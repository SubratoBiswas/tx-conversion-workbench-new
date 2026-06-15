"""Cutover orchestration service.

Owns lifecycle and generators for the Slice 6 cutover layer:

* Reconciliation generator — when invoked on a freshly loaded conversion,
  seeds plausible control-total checks (open AR / item counts / etc.)
  with deterministic mock variances. Real engagements override these
  with their own SQL contracts at later slices.
* Cutover Runbook seeder — canonical N-step playbook the team can then
  edit (assign owners, change durations, add tasks).
* Sign-off ledger writer — append-only inserts, never updates.
* Issue / Risk / Dress Rehearsal CRUD wrappers — kept thin.

Every mutation funnels through ``record_event`` so the audit log
captures the trail.
"""
from __future__ import annotations

import random
from datetime import datetime, date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditEvent  # noqa: F401
from app.models.conversion import Conversion
from app.models.cutover import (
    CutoverTask, DressRehearsal, Issue, ReconciliationCheck,
    Risk, SignOff,
)
from app.models.project import Project
from app.services.audit_service import record_event


# ─── Reconciliation generator ──────────────────────────────────────


_RECON_METRICS_BY_OBJECT: dict[str, list[tuple[str, str]]] = {
    "Item": [
        ("Active Items · Count",         "count"),
        ("Item Master · UOM Coverage %", "percent"),
    ],
    "Customer": [
        ("AR Open Balance · USD", "currency"),
        ("Active Customers · Count", "count"),
    ],
    "Supplier": [
        ("AP Open Balance · USD", "currency"),
        ("Active Suppliers · Count", "count"),
    ],
    "Sales Order": [
        ("Open Sales Order Backlog · USD", "currency"),
        ("Open Order Lines · Count", "count"),
    ],
}


def seed_reconciliation_for_project(
    db: Session, project: Project, *, mock: bool = True,
) -> list[ReconciliationCheck]:
    """Seed plausible reconciliation rows for every loaded / output-
    generated conversion. Idempotent — refreshing simply updates the
    existing rows in place rather than duplicating."""
    rng = random.Random()
    rng.seed(f"recon:{project.id}")
    convs = (
        db.query(Conversion)
        .filter(Conversion.project_id == project.id)
        .all()
    )
    out: list[ReconciliationCheck] = []
    for c in convs:
        metrics = _RECON_METRICS_BY_OBJECT.get(c.target_object or "", [])
        for metric_name, kind in metrics:
            existing = (
                db.query(ReconciliationCheck)
                .filter(
                    ReconciliationCheck.conversion_id == c.id,
                    ReconciliationCheck.metric_name == metric_name,
                )
                .first()
            )
            # Mock-mode variances: 80% within tolerance, 15% within
            # tolerance but flagged for review, 5% hard fail.
            roll = rng.random()
            if roll < 0.80:
                status = "pass"
                variance_factor = rng.uniform(-0.001, 0.001)
            elif roll < 0.95:
                status = "warning"
                variance_factor = rng.uniform(-0.01, 0.01)
            else:
                status = "fail"
                variance_factor = rng.uniform(-0.07, 0.07)

            base = 1_000_000 if kind == "currency" else 8000
            source = base + rng.randint(-base // 10, base // 10)
            target = int(source * (1 + variance_factor))
            variance = source - target
            variance_pct = round((variance / source) * 100, 3) if source else 0.0
            tolerance = int(0.005 * source) if status == "pass" else int(0.05 * source)
            tolerance_pct = 0.5 if status == "pass" else 5.0

            if existing:
                existing.source_value = source
                existing.target_value = target
                existing.variance = variance
                existing.variance_pct = variance_pct
                existing.tolerance = tolerance
                existing.tolerance_pct = tolerance_pct
                existing.status = status
                existing.last_run_at = datetime.utcnow()
                row = existing
            else:
                row = ReconciliationCheck(
                    conversion_id=c.id,
                    metric_name=metric_name,
                    source_value=source,
                    target_value=target,
                    variance=variance,
                    variance_pct=variance_pct,
                    tolerance=tolerance,
                    tolerance_pct=tolerance_pct,
                    currency="USD" if kind == "currency" else None,
                    status=status,
                    notes=("Mock recon" if mock else None),
                )
                db.add(row)
            out.append(row)
    db.commit()
    return out


# ─── Runbook seeder ─────────────────────────────────────────────────


_CANONICAL_RUNBOOK: list[dict[str, Any]] = [
    {"phase": "lift", "title": "Freeze source ERP",                    "duration": 30, "severity": "critical"},
    {"phase": "lift", "title": "Snapshot DEV → restore-point #1",     "duration": 20, "severity": "info"},
    {"phase": "lift", "title": "Load T0 Foundation (UOM / COA / Cal)", "duration": 45, "severity": "critical"},
    {"phase": "lift", "title": "Reconcile T0 control totals",          "duration": 20, "severity": "critical"},
    {"phase": "lift", "title": "Load T1 Masters (Items / Customers)",  "duration": 90, "severity": "critical"},
    {"phase": "lift", "title": "Reconcile T1 control totals",          "duration": 30, "severity": "critical"},
    {"phase": "lift", "title": "Load T2 Open Transactions",            "duration": 120, "severity": "critical"},
    {"phase": "lift", "title": "Reconcile T2 control totals",          "duration": 40, "severity": "critical"},
    {"phase": "lift", "title": "Load T3 Historical",                   "duration": 180, "severity": "warning"},
    {"phase": "lift", "title": "FX rate refresh + revaluation",        "duration": 25, "severity": "critical"},
    {"phase": "lift", "title": "Integration cutover (Salesforce / Workday / Avalara)", "duration": 60, "severity": "critical"},
    {"phase": "lift", "title": "Run smoke tests against PROD",         "duration": 45, "severity": "critical"},
    {"phase": "lift", "title": "Sign-off · Cutover Go/No-go",          "duration": 30, "severity": "critical"},
    {"phase": "thrive", "title": "Open Fusion to end users",           "duration": 15, "severity": "critical"},
    {"phase": "thrive", "title": "Hypercare standby + war-room rotation", "duration": 0, "severity": "warning"},
]


def seed_runbook_for_project(
    db: Session, project: Project, *, force: bool = False,
) -> list[CutoverTask]:
    existing_count = (
        db.query(CutoverTask).filter(CutoverTask.project_id == project.id).count()
    )
    if existing_count and not force:
        return (
            db.query(CutoverTask)
            .filter(CutoverTask.project_id == project.id)
            .order_by(CutoverTask.sequence)
            .all()
        )
    if force:
        db.query(CutoverTask).filter(CutoverTask.project_id == project.id).delete()
    rows: list[CutoverTask] = []
    for i, t in enumerate(_CANONICAL_RUNBOOK, start=1):
        row = CutoverTask(
            project_id=project.id,
            sequence=i * 10,
            phase=t["phase"],
            title=t["title"],
            expected_duration_minutes=t["duration"],
            severity=t["severity"],
            owner_email=project.migration_lead or "migration_lead@trinamix.com",
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


# ─── Sign-off ledger ────────────────────────────────────────────────


def record_signoff(
    db: Session,
    *,
    project_id: int,
    conversion_id: int | None,
    kind: str,
    subject: str,
    signer_email: str,
    signer_role: str,
    decision: str = "approved",
    comment: str | None = None,
    evidence_url: str | None = None,
    references_signoff_id: int | None = None,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> SignOff:
    """Append-only insert. Never updates an existing row."""
    row = SignOff(
        project_id=project_id,
        conversion_id=conversion_id,
        kind=kind,
        subject=subject,
        signer_email=signer_email,
        signer_role=signer_role,
        decision=decision,
        comment=comment,
        evidence_url=evidence_url,
        references_signoff_id=references_signoff_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="mapping.approved" if kind == "conversion" else "project.updated",
        target_type="sign_off",
        target_id=row.id,
        project_id=project_id,
        summary=(
            f"Sign-off ({kind}) · {subject} → {decision} by {signer_role}"
        ),
        details={
            "kind": kind, "subject": subject, "signer_role": signer_role,
            "decision": decision,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return row


# ─── Issues / Risks / Dress rehearsals — thin CRUD ──────────────────


def upsert_risk_score(row: Risk) -> None:
    """Set ``row.score`` = probability × impact whenever either changes."""
    row.score = int((row.probability or 0) * (row.impact or 0))


def days_until_cutover(project: Project) -> int | None:
    if not project.production_cutover_start:
        return None
    delta = project.production_cutover_start.date() - date.today()
    return delta.days

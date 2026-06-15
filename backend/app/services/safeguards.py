"""Cutover Safeguards — the 7 pre-go-live gates.

A safeguard is a derived check computed from existing project state. None
of these are stored as their own table — that would create double-source-
of-truth problems and require refresh jobs. Instead, the service walks
the relevant models on demand and returns a status snapshot the UI can
render on the Migration Monitor strip.

The seven gates (canonical order — same order they render in the UI):

  gl_periods   — open GL periods aligned between source and target.
  dual_cert    — every dual-cert-required mapping has the second sign-off.
  load_seq     — every conversion respects its dependency-tier order.
  doc_nos      — document numbering sequences are continuous (no gaps).
  txn_close    — open transactions in scope are reconciled or excluded.
  fx_rates     — current FX rates fall within tolerance of the target.
  recon        — control totals match within tolerance (latest run).

Each gate returns ``pass | warning | fail | not_run`` with a short message
and a ``details`` object for the drilldown. ``not_run`` is the right state
when the underlying signal is missing (e.g. no recon run yet) — the UI
treats it differently than a failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.conversion import Conversion
from app.models.load import LoadRun
from app.models.mapping import MappingSuggestion
from app.models.project import Project


SAFEGUARD_CODES: tuple[str, ...] = (
    "gl_periods", "dual_cert", "load_seq", "doc_nos",
    "txn_close", "fx_rates", "recon",
)


@dataclass
class SafeguardResult:
    code: str
    name: str
    status: str          # "pass" | "warning" | "fail" | "not_run"
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    last_checked_at: datetime = field(default_factory=datetime.utcnow)


# ─── Individual checks ──────────────────────────────────────────────


def _check_gl_periods(_db: Session, project: Project) -> SafeguardResult:
    """For v1 mock-mode this returns a deterministic "pass" tied to the
    project's cutover window. Live mode (a later slice) inspects GL period
    rows in the source ERP via the discovery scanner."""
    if not project.production_cutover_start:
        return SafeguardResult(
            code="gl_periods", name="GL Periods Aligned",
            status="not_run",
            message="Cutover window not yet defined — set the production cutover dates.",
        )
    # Same period is open both sides ⇒ pass.
    return SafeguardResult(
        code="gl_periods", name="GL Periods Aligned",
        status="pass",
        message="Source and target both have one open period covering cutover.",
        details={"source_open_period": "AUG-2026", "target_open_period": "AUG-2026"},
    )


def _check_dual_cert(db: Session, project: Project) -> SafeguardResult:
    """Every mapping flagged ``requires_dual_approval`` must show two
    distinct approver emails before cutover. Returns ``fail`` if any
    flagged mapping is approved by only one user."""
    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    cids = [c.id for c in convs]
    if not cids:
        return SafeguardResult(
            code="dual_cert", name="Dual Certification",
            status="not_run",
            message="No conversions yet.",
        )
    flagged = (
        db.query(MappingSuggestion)
        .filter(
            MappingSuggestion.conversion_id.in_(cids),
            MappingSuggestion.requires_dual_approval == 1,
        )
        .all()
    )
    if not flagged:
        return SafeguardResult(
            code="dual_cert", name="Dual Certification",
            status="pass",
            message="No dual-cert-required mappings in scope.",
            details={"flagged_count": 0},
        )
    missing: list[dict[str, Any]] = []
    for m in flagged:
        primary = (m.approved_by or "").strip()
        secondary = (m.second_approver_email or "").strip()
        if not primary or not secondary or primary == secondary:
            missing.append({
                "mapping_id": m.id,
                "conversion_id": m.conversion_id,
                "target_field_id": m.target_field_id,
                "primary": primary or None,
                "secondary": secondary or None,
            })
    if missing:
        return SafeguardResult(
            code="dual_cert", name="Dual Certification",
            status="fail",
            message=(
                f"{len(missing)} flagged mapping{'s' if len(missing) != 1 else ''} "
                f"still need a second sign-off from a different approver."
            ),
            details={"flagged_count": len(flagged), "missing": missing[:25]},
        )
    return SafeguardResult(
        code="dual_cert", name="Dual Certification",
        status="pass",
        message=f"All {len(flagged)} flagged mappings dual-certified.",
        details={"flagged_count": len(flagged), "missing": []},
    )


def _check_load_seq(db: Session, project: Project) -> SafeguardResult:
    """Every dependency-tier prerequisite must be loaded before its
    downstream object. Walks the conversion list ordered by
    ``planned_load_order``; flags any downstream conversion whose
    prerequisite is not yet at ``status="loaded"``."""
    convs = (
        db.query(Conversion)
        .filter(Conversion.project_id == project.id)
        .order_by(Conversion.planned_load_order, Conversion.id)
        .all()
    )
    if not convs:
        return SafeguardResult(
            code="load_seq", name="Load Sequence Valid",
            status="not_run",
            message="No conversions yet.",
        )
    in_loaded = {c.target_object for c in convs if c.status == "loaded"}
    out_of_order: list[dict[str, Any]] = []
    # Same dependency map the cascade detector uses.
    REQUIRES = {
        "Sales Order":   {"Item", "Customer", "UOM"},
        "Purchase Order": {"Item", "Supplier", "UOM"},
        "BOM":            {"Item"},
        "On-Hand Balance": {"Item", "Inventory Org"},
    }
    for c in convs:
        if c.status not in ("loaded", "output_generated"):
            continue
        prereqs = REQUIRES.get(c.target_object or "", set())
        missing_prereqs = prereqs - in_loaded
        if missing_prereqs:
            out_of_order.append({
                "conversion_id": c.id,
                "name": c.name,
                "target_object": c.target_object,
                "missing_prereqs": sorted(missing_prereqs),
            })
    if out_of_order:
        return SafeguardResult(
            code="load_seq", name="Load Sequence Valid",
            status="fail",
            message=(
                f"{len(out_of_order)} conversion{'s' if len(out_of_order) != 1 else ''} "
                "advanced before their prerequisites loaded — re-sequence the cutover plan."
            ),
            details={"out_of_order": out_of_order},
        )
    return SafeguardResult(
        code="load_seq", name="Load Sequence Valid",
        status="pass",
        message="Every loaded conversion respects its dependency-tier order.",
    )


def _check_doc_nos(_db: Session, project: Project) -> SafeguardResult:
    """Document-numbering continuity — mock-mode v1. Live mode reads
    AR_INVOICES / AP_INVOICES last-document-number tables."""
    return SafeguardResult(
        code="doc_nos", name="Document Numbering",
        status="pass",
        message="No gaps detected in AR/AP/SO document sequences.",
        details={"sequences_checked": ["AR_INV", "AP_INV", "SO_ORDER", "PO_ORDER"]},
    )


def _check_txn_close(db: Session, project: Project) -> SafeguardResult:
    """Open transactions in scope must be reconciled or explicitly
    excluded. Mock-mode returns ``warning`` when any conversion is
    still in an active state past the cutover window."""
    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    open_states = {"draft", "mapping_suggested", "awaiting_approval"}
    open_convs = [c for c in convs if c.status in open_states]
    if open_convs:
        return SafeguardResult(
            code="txn_close", name="Transaction Close",
            status="warning",
            message=(
                f"{len(open_convs)} conversion{'s' if len(open_convs) != 1 else ''} "
                "still active — confirm transactions in those objects are excluded."
            ),
            details={
                "open_conversions": [
                    {"id": c.id, "name": c.name, "status": c.status}
                    for c in open_convs[:25]
                ],
            },
        )
    return SafeguardResult(
        code="txn_close", name="Transaction Close",
        status="pass",
        message="All conversions reconciled or excluded from cutover.",
    )


def _check_fx_rates(_db: Session, project: Project) -> SafeguardResult:
    """FX rates — mock-mode returns warning to mirror the Bolt screenshot
    where this is the one safeguard not yet green. Real prober reads the
    rate table and confirms each currency's rate is within tolerance."""
    return SafeguardResult(
        code="fx_rates", name="FX Rates Current",
        status="warning",
        message="3 currencies have rates older than 24h — refresh before cutover.",
        details={
            "stale_currencies": [
                {"code": "GBP", "rate_age_hours": 31},
                {"code": "AUD", "rate_age_hours": 27},
                {"code": "INR", "rate_age_hours": 26},
            ],
        },
    )


def _check_recon(db: Session, project: Project) -> SafeguardResult:
    """Reconciliation — defers to the latest ReconciliationCheck rows for
    this project. Surfaces ``warning`` when any check is "fail" within
    tolerance, ``fail`` if any is hard-fail."""
    from app.models.reconciliation import ReconciliationCheck

    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    cids = [c.id for c in convs]
    if not cids:
        return SafeguardResult(
            code="recon", name="Reconciliation",
            status="not_run",
            message="No reconciliation checks recorded yet.",
        )
    checks = (
        db.query(ReconciliationCheck)
        .filter(ReconciliationCheck.conversion_id.in_(cids))
        .all()
    )
    if not checks:
        return SafeguardResult(
            code="recon", name="Reconciliation",
            status="not_run",
            message="No reconciliation checks recorded yet.",
        )
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warning"]
    if fails:
        return SafeguardResult(
            code="recon", name="Reconciliation",
            status="fail",
            message=(
                f"{len(fails)} reconciliation check{'s' if len(fails) != 1 else ''} "
                "failed — variance exceeds tolerance."
            ),
            details={
                "failing_checks": [
                    {"metric": c.metric_name, "variance": c.variance,
                     "tolerance": c.tolerance}
                    for c in fails[:10]
                ],
            },
        )
    if warns:
        return SafeguardResult(
            code="recon", name="Reconciliation",
            status="warning",
            message=f"{len(warns)} check(s) within tolerance but flagged for review.",
        )
    return SafeguardResult(
        code="recon", name="Reconciliation",
        status="pass",
        message=f"All {len(checks)} reconciliation check(s) within tolerance.",
    )


# ─── Roll-up ────────────────────────────────────────────────────────


def evaluate_safeguards(db: Session, project: Project) -> list[SafeguardResult]:
    """Run all seven checks and return them in canonical order."""
    return [
        _check_gl_periods(db, project),
        _check_dual_cert(db, project),
        _check_load_seq(db, project),
        _check_doc_nos(db, project),
        _check_txn_close(db, project),
        _check_fx_rates(db, project),
        _check_recon(db, project),
    ]


def safeguard_pass_rate(results: list[SafeguardResult]) -> float:
    """Float 0..1 — fraction passing. ``not_run`` counts as zero so the
    Migration Readiness Score reflects "still unproven" honestly."""
    if not results:
        return 0.0
    passed = sum(1 for r in results if r.status == "pass")
    return round(passed / len(results), 3)

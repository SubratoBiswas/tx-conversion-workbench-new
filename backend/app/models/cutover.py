"""Cutover orchestration models — the production-grade go-live layer.

This module collects everything an enterprise customer needs to run a
real Oracle Fusion cutover with audit-ready evidence:

* **ReconciliationCheck** — control-total comparisons between source and
  target with explicit tolerances. Drives the Recon safeguard and the
  CFO dashboard's variance figure.
* **CutoverTask** — ordered, time-boxed runbook tasks for the actual
  go-live day. Owner + expected_duration_minutes + actual + status.
* **SignOff** — append-only sign-off ledger. Every phase / conversion
  acceptance lands here as immutable evidence — name + role + timestamp,
  never updated. Compliance gold.
* **Issue** — open blockers per project with owner + due_date + severity.
* **Risk** — risk register entry: probability × impact + mitigation owner.
* **DressRehearsal** — record of each cutover dry run with pass/fail.

None of these are "nice-to-have" — they are what differentiates a
demo-quality tool from one a Fortune 500 program manager will trust.
"""
from datetime import datetime

from sqlalchemy import (
    Column, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ─── ReconciliationCheck ───────────────────────────────────────────


RECON_STATUSES = ("pass", "warning", "fail", "not_run")


class ReconciliationCheck(Base):
    __tablename__ = "reconciliation_checks"

    id = Column(Integer, primary_key=True, index=True)
    conversion_id = Column(
        Integer, ForeignKey("conversions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    metric_name = Column(String(200), nullable=False, index=True)
    # Free-form: "AR Open Balance · USD" / "Item Count · Active Items"
    source_value = Column(Float, default=0.0)
    target_value = Column(Float, default=0.0)
    variance = Column(Float, default=0.0)
    variance_pct = Column(Float, default=0.0)
    tolerance = Column(Float, default=0.0)   # absolute (currency / count)
    tolerance_pct = Column(Float, default=0.0)
    currency = Column(String(8), nullable=True)
    status = Column(String(16), default="not_run")
    notes = Column(Text, nullable=True)
    last_run_at = Column(DateTime, default=datetime.utcnow)


# ─── Cutover Runbook ───────────────────────────────────────────────


RUNBOOK_TASK_STATUSES = ("pending", "in_progress", "blocked", "complete", "skipped")
RUNBOOK_TASK_SEVERITY = ("info", "warning", "critical")


class CutoverTask(Base):
    __tablename__ = "cutover_tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence = Column(Integer, default=0)
    phase = Column(String(20), default="lift")  # blueprint | own | lift | thrive
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    owner_email = Column(String(150), nullable=True)
    expected_duration_minutes = Column(Integer, default=15)
    actual_duration_minutes = Column(Integer, nullable=True)
    status = Column(String(20), default="pending")
    severity = Column(String(20), default="info")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    blocker_note = Column(Text, nullable=True)
    # Optional anchor to a specific conversion the task gates on.
    conversion_id = Column(
        Integer, ForeignKey("conversions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Sign-Off Ledger ───────────────────────────────────────────────


SIGNOFF_KINDS = (
    "phase",          # phase-level (Blueprint complete, Own complete, ...)
    "conversion",     # per-conversion (Item Master signed off)
    "coa",            # Chart-of-Accounts segment composition signed off
    "cutover_go",     # final go/no-go vote
    "uat",            # UAT acceptance
)


class SignOff(Base):
    """Append-only — never UPDATE, never DELETE. If a sign-off is revoked,
    insert a new row with ``kind=*_revoked`` referencing the prior."""

    __tablename__ = "sign_offs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    conversion_id = Column(
        Integer, ForeignKey("conversions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    kind = Column(String(32), nullable=False, index=True)
    # The thing being signed off — phase code, conversion target_object, …
    subject = Column(String(200), nullable=False)
    signer_email = Column(String(200), nullable=False)
    signer_role = Column(String(120), nullable=False)
    decision = Column(String(20), default="approved")   # approved | rejected
    comment = Column(Text, nullable=True)
    evidence_url = Column(String(500), nullable=True)
    # Polymorphic link back to a prior signoff (revocations).
    references_signoff_id = Column(Integer, ForeignKey("sign_offs.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── Issue / Blocker tracker ───────────────────────────────────────


ISSUE_STATUSES = ("open", "in_progress", "blocked", "resolved", "wont_fix")
ISSUE_SEVERITIES = ("low", "medium", "high", "critical")


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    conversion_id = Column(
        Integer, ForeignKey("conversions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    owner_email = Column(String(150), nullable=True)
    raised_by = Column(String(150), nullable=True)
    severity = Column(String(20), default="medium")
    status = Column(String(20), default="open")
    due_date = Column(Date, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)
    external_ticket = Column(String(120), nullable=True)
    tags_json = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Risk register ─────────────────────────────────────────────────


RISK_STATUSES = ("identified", "mitigating", "accepted", "closed")


class Risk(Base):
    __tablename__ = "risks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # 1..5 each
    probability = Column(Integer, default=3)
    impact = Column(Integer, default=3)
    # Computed at write time so the dashboard query is cheap.
    score = Column(Integer, default=9)
    mitigation = Column(Text, nullable=True)
    owner_email = Column(String(150), nullable=True)
    status = Column(String(20), default="identified")
    raised_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


# ─── Dress Rehearsal ───────────────────────────────────────────────


DRESS_REHEARSAL_RESULTS = ("pass", "warning", "fail", "in_progress")


class DressRehearsal(Base):
    __tablename__ = "dress_rehearsals"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence = Column(Integer, default=1)
    scheduled_for = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    result = Column(String(20), default="in_progress")
    # Free-form report — what passed, what failed, what was logged.
    summary = Column(Text, nullable=True)
    findings_json = Column(JSON, default=list)
    led_by = Column(String(150), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

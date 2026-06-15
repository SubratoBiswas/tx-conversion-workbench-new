"""Cutover & Exec layer endpoints (Slice 6).

Owned surfaces:

  GET    /projects/{id}/safeguards                — 7-gate snapshot
  POST   /projects/{id}/safeguards/refresh        — re-evaluate
  GET    /projects/{id}/readiness                 — composite score + lenses
  POST   /projects/{id}/reconciliation/seed       — generator (mock-mode)
  GET    /projects/{id}/reconciliation            — list checks
  POST   /projects/{id}/runbook/seed              — canonical 15-step template
  GET    /projects/{id}/runbook                   — list tasks
  PATCH  /runbook-tasks/{id}                      — update status / actuals
  GET    /projects/{id}/issues                    — list open
  POST   /projects/{id}/issues                    — open one
  PATCH  /issues/{id}                             — update
  GET    /projects/{id}/risks                     — risk register
  POST   /projects/{id}/risks
  PATCH  /risks/{id}
  POST   /projects/{id}/dress-rehearsals          — log a rehearsal
  GET    /projects/{id}/dress-rehearsals
  POST   /projects/{id}/sign-offs                 — append-only ledger
  GET    /projects/{id}/sign-offs
  POST   /projects/{id}/promote-environment       — gated promotion
  GET    /projects/{id}/exec-summary              — CFO rollup
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.cutover import (
    CutoverTask, DressRehearsal, Issue, ReconciliationCheck,
    Risk, SignOff,
)
from app.models.project import Project
from app.models.load import LoadRun
from app.models.user import User
from app.services.audit_service import record_event
from app.services.auth_service import get_current_user
from app.services.cutover_service import (
    days_until_cutover, record_signoff, seed_reconciliation_for_project,
    seed_runbook_for_project, upsert_risk_score,
)
from app.services.coa_readiness import (
    compute_project_coa_readiness, require_coa_ready_for_cutover,
    serialize_readiness as serialize_coa_readiness,
)
from app.services.readiness_score import compute_readiness
from app.services.safeguards import evaluate_safeguards, safeguard_pass_rate


router = APIRouter(prefix="/api", tags=["cutover-slice6"])


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _require_project(db: Session, project_id: int) -> Project:
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    return p


# ─── Schemas (kept in this file to keep the surface review-able as a unit) ──


class SafeguardOut(BaseModel):
    code: str
    name: str
    status: str
    message: str
    details: dict[str, Any] = {}


class SafeguardsResponse(BaseModel):
    pass_rate: float
    safeguards: list[SafeguardOut]


class ReadinessLensOut(BaseModel):
    label: str
    value: float
    value_pct: int
    weight: float
    details: dict[str, Any] = {}


class ReadinessResponse(BaseModel):
    total: float
    total_pct: int
    delta_2w: float
    lenses: dict[str, ReadinessLensOut]


class ReconciliationCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversion_id: int
    metric_name: str
    source_value: float
    target_value: float
    variance: float
    variance_pct: float
    tolerance: float
    tolerance_pct: float
    currency: str | None = None
    status: str
    notes: str | None = None
    last_run_at: datetime | None = None


class RunbookTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sequence: int
    phase: str
    title: str
    description: str | None = None
    owner_email: str | None = None
    expected_duration_minutes: int
    actual_duration_minutes: int | None = None
    status: str
    severity: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    blocker_note: str | None = None
    conversion_id: int | None = None


class RunbookTaskUpdate(BaseModel):
    status: str | None = None
    owner_email: str | None = None
    actual_duration_minutes: int | None = None
    blocker_note: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    conversion_id: int | None = None
    title: str
    description: str | None = None
    owner_email: str | None = None
    raised_by: str | None = None
    severity: str
    status: str
    due_date: date | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    external_ticket: str | None = None
    tags_json: list[str] | None = []
    created_at: datetime


class IssueCreate(BaseModel):
    title: str
    description: str | None = None
    owner_email: str | None = None
    severity: str = "medium"
    due_date: date | None = None
    conversion_id: int | None = None
    external_ticket: str | None = None
    tags: list[str] = Field(default_factory=list)


class IssueUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    owner_email: str | None = None
    severity: str | None = None
    status: str | None = None
    due_date: date | None = None
    resolution_note: str | None = None
    external_ticket: str | None = None


class RiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    title: str
    description: str | None = None
    probability: int
    impact: int
    score: int
    mitigation: str | None = None
    owner_email: str | None = None
    status: str
    raised_at: datetime
    closed_at: datetime | None = None


class RiskCreate(BaseModel):
    title: str
    description: str | None = None
    probability: int = 3
    impact: int = 3
    mitigation: str | None = None
    owner_email: str | None = None
    status: str = "identified"


class RiskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    probability: int | None = None
    impact: int | None = None
    mitigation: str | None = None
    owner_email: str | None = None
    status: str | None = None


class DressRehearsalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    sequence: int
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_minutes: int | None = None
    result: str
    summary: str | None = None
    findings_json: list[dict[str, Any]] | None = []
    led_by: str | None = None
    created_at: datetime


class DressRehearsalCreate(BaseModel):
    scheduled_for: datetime | None = None
    result: str = "in_progress"
    summary: str | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    led_by: str | None = None
    duration_minutes: int | None = None


class SignOffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    conversion_id: int | None = None
    kind: str
    subject: str
    signer_email: str
    signer_role: str
    decision: str
    comment: str | None = None
    evidence_url: str | None = None
    references_signoff_id: int | None = None
    created_at: datetime


class SignOffCreate(BaseModel):
    kind: str
    subject: str
    signer_email: str
    signer_role: str
    conversion_id: int | None = None
    decision: str = "approved"
    comment: str | None = None
    evidence_url: str | None = None
    references_signoff_id: int | None = None


class PromoteEnvironmentPayload(BaseModel):
    target_environment: str   # "QA" | "UAT" | "PROD"


class ExecSummaryResponse(BaseModel):
    score_pct: int
    score_5: float
    safeguard_pass_rate: float
    days_to_cutover: int | None = None
    open_critical_issues: int
    top_risks: list[RiskOut]
    top_blockers: list[IssueOut]
    total_recon_variance_usd: float
    pillar_complexity: float | None = None
    integrations_degraded: int = 0


# ─── Safeguards ─────────────────────────────────────────────────────


@router.get("/projects/{project_id}/safeguards", response_model=SafeguardsResponse)
def list_safeguards(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    results = evaluate_safeguards(db, p)
    return SafeguardsResponse(
        pass_rate=safeguard_pass_rate(results),
        safeguards=[
            SafeguardOut(
                code=r.code, name=r.name, status=r.status,
                message=r.message, details=r.details,
            )
            for r in results
        ],
    )


# ─── Readiness Score ────────────────────────────────────────────────


@router.get("/projects/{project_id}/readiness", response_model=ReadinessResponse)
def get_readiness(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    score = compute_readiness(db, p)
    return ReadinessResponse(
        total=score.total,
        total_pct=score.total_pct,
        delta_2w=score.delta_2w,
        lenses={k: ReadinessLensOut(**v) for k, v in score.lenses.items()},
    )


# ─── Reconciliation ─────────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/reconciliation/seed",
    response_model=list[ReconciliationCheckOut],
)
def post_reconciliation_seed(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    rows = seed_reconciliation_for_project(db, p)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="discovery.scan_completed",   # closest match in vocabulary
        target_type="project",
        target_id=p.id,
        project_id=p.id,
        summary=f"Seeded {len(rows)} reconciliation checks (mock)",
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return rows


@router.get(
    "/projects/{project_id}/reconciliation",
    response_model=list[ReconciliationCheckOut],
)
def list_reconciliation(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    from app.models.conversion import Conversion
    cids = [
        c.id for c in db.query(Conversion).filter(Conversion.project_id == project_id).all()
    ]
    if not cids:
        return []
    return (
        db.query(ReconciliationCheck)
        .filter(ReconciliationCheck.conversion_id.in_(cids))
        .order_by(ReconciliationCheck.metric_name)
        .all()
    )


# ─── Runbook ────────────────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/runbook/seed",
    response_model=list[RunbookTaskOut],
)
def post_runbook_seed(
    project_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    rows = seed_runbook_for_project(db, p, force=force)
    return rows


@router.get(
    "/projects/{project_id}/runbook",
    response_model=list[RunbookTaskOut],
)
def list_runbook(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    return (
        db.query(CutoverTask)
        .filter(CutoverTask.project_id == project_id)
        .order_by(CutoverTask.sequence)
        .all()
    )


@router.patch("/runbook-tasks/{task_id}", response_model=RunbookTaskOut)
def update_runbook_task(
    task_id: int,
    payload: RunbookTaskUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.query(CutoverTask).filter(CutoverTask.id == task_id).first()
    if not row:
        raise HTTPException(404, "Task not found")
    data = payload.model_dump(exclude_unset=True)
    changed = list(data.keys())
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="cutover_task",
        target_id=row.id,
        project_id=row.project_id,
        summary=f"Runbook task '{row.title}' updated",
        details={"fields": changed},
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return row


# ─── Issues ─────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/issues", response_model=list[IssueOut])
def list_issues(
    project_id: int,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    q = db.query(Issue).filter(Issue.project_id == project_id)
    if status:
        q = q.filter(Issue.status == status)
    return q.order_by(Issue.severity.desc(), Issue.id.desc()).all()


@router.post("/projects/{project_id}/issues", response_model=IssueOut)
def create_issue(
    project_id: int,
    payload: IssueCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    row = Issue(
        project_id=project_id,
        title=payload.title,
        description=payload.description,
        owner_email=payload.owner_email,
        severity=payload.severity,
        due_date=payload.due_date,
        conversion_id=payload.conversion_id,
        external_ticket=payload.external_ticket,
        tags_json=payload.tags,
        raised_by=user.email,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="issue",
        target_id=row.id,
        project_id=project_id,
        summary=f"Issue raised · {row.severity.upper()} · {row.title}",
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return row


@router.patch("/issues/{issue_id}", response_model=IssueOut)
def update_issue(
    issue_id: int,
    payload: IssueUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.query(Issue).filter(Issue.id == issue_id).first()
    if not row:
        raise HTTPException(404, "Issue not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("status") in ("resolved", "wont_fix") and not row.resolved_at:
        row.resolved_at = datetime.utcnow()
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="issue",
        target_id=row.id,
        project_id=row.project_id,
        summary=f"Issue updated · {row.title}",
        details={"fields": list(data.keys())},
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return row


# ─── Risks ──────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/risks", response_model=list[RiskOut])
def list_risks(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    return (
        db.query(Risk)
        .filter(Risk.project_id == project_id)
        .order_by(Risk.score.desc(), Risk.id.desc())
        .all()
    )


@router.post("/projects/{project_id}/risks", response_model=RiskOut)
def create_risk(
    project_id: int,
    payload: RiskCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    row = Risk(project_id=project_id, **payload.model_dump())
    upsert_risk_score(row)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/risks/{risk_id}", response_model=RiskOut)
def update_risk(
    risk_id: int,
    payload: RiskUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = db.query(Risk).filter(Risk.id == risk_id).first()
    if not row:
        raise HTTPException(404, "Risk not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    upsert_risk_score(row)
    if row.status == "closed" and not row.closed_at:
        row.closed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


# ─── Dress Rehearsals ───────────────────────────────────────────────


@router.get(
    "/projects/{project_id}/dress-rehearsals",
    response_model=list[DressRehearsalOut],
)
def list_dress_rehearsals(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    return (
        db.query(DressRehearsal)
        .filter(DressRehearsal.project_id == project_id)
        .order_by(DressRehearsal.sequence.desc())
        .all()
    )


@router.post(
    "/projects/{project_id}/dress-rehearsals",
    response_model=DressRehearsalOut,
)
def create_dress_rehearsal(
    project_id: int,
    payload: DressRehearsalCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    next_seq = (
        db.query(DressRehearsal)
        .filter(DressRehearsal.project_id == project_id)
        .count() + 1
    )
    row = DressRehearsal(
        project_id=project_id,
        sequence=next_seq,
        scheduled_for=payload.scheduled_for,
        started_at=payload.scheduled_for,
        completed_at=datetime.utcnow() if payload.result != "in_progress" else None,
        duration_minutes=payload.duration_minutes,
        result=payload.result,
        summary=payload.summary,
        findings_json=payload.findings,
        led_by=payload.led_by or user.email,
    )
    db.add(row)
    p.dress_rehearsal_count = next_seq
    db.commit()
    db.refresh(row)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="dress_rehearsal",
        target_id=row.id,
        project_id=project_id,
        summary=f"Dress rehearsal #{next_seq} · {payload.result.upper()}",
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return row


# ─── Sign-Off Ledger ────────────────────────────────────────────────


@router.get("/projects/{project_id}/sign-offs", response_model=list[SignOffOut])
def list_sign_offs(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    return (
        db.query(SignOff)
        .filter(SignOff.project_id == project_id)
        .order_by(SignOff.created_at.desc())
        .all()
    )


@router.post("/projects/{project_id}/sign-offs", response_model=SignOffOut)
def create_sign_off(
    project_id: int,
    payload: SignOffCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    # P6 — COA coverage gate. The cutover-go sign-off is the last
    # control before production load; if the COA scope cannot compose
    # cleanly, we MUST block the sign-off (with a structured payload the
    # UI can render) rather than let an incomplete COA bleed into prod.
    if payload.kind == "cutover_go" and payload.decision == "approved":
        try:
            require_coa_ready_for_cutover(db, project_id)
        except HTTPException as block:
            # Auditors require an immutable trail of every blocked
            # production gate attempt — a successful sign-off is logged
            # implicitly via the SignOff row, but a *blocked* attempt
            # leaves no row at all unless we record one here.
            details = block.detail if isinstance(block.detail, dict) else {"message": str(block.detail)}
            record_event(
                db,
                actor_email=user.email,
                actor_user_id=user.id,
                action="project.updated",
                target_type="sign_off_blocked",
                target_id=None,
                project_id=project_id,
                summary=(
                    f"Cutover-Go sign-off BLOCKED by COA gate · "
                    f"requested by {payload.signer_email} ({payload.signer_role})"
                ),
                details={
                    "kind":   payload.kind,
                    "subject": payload.subject,
                    "signer_email": payload.signer_email,
                    "signer_role":  payload.signer_role,
                    "coa_block":    details,
                },
                source_ip=_client_ip(request),
                user_agent=_ua(request),
            )
            raise
    row = record_signoff(
        db,
        project_id=project_id,
        conversion_id=payload.conversion_id,
        kind=payload.kind,
        subject=payload.subject,
        signer_email=payload.signer_email,
        signer_role=payload.signer_role,
        decision=payload.decision,
        comment=payload.comment,
        evidence_url=payload.evidence_url,
        references_signoff_id=payload.references_signoff_id,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return row


# ─── COA Readiness (drives the cutover-go gate banner) ──────────────


@router.get("/projects/{project_id}/coa-readiness")
def get_coa_readiness(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Used by the Sign-off Capture modal to know whether to allow a
    cutover-go sign-off. Returns ``{is_ready, threshold_pct, conversions,
    blocker_reason}``. When a project has no COA scope at all the
    response is ``is_ready=True`` with ``conversions=[]`` so the UI
    renders "COA gate: N/A"."""
    _require_project(db, project_id)
    state = compute_project_coa_readiness(db, project_id)
    return serialize_coa_readiness(state)


# ─── Environment promotion gate ─────────────────────────────────────


_PROMOTION_ORDER = ["DEV", "QA", "UAT", "PROD"]


@router.post(
    "/projects/{project_id}/promote-environment",
    response_model=dict,
)
def promote_environment(
    project_id: int,
    payload: PromoteEnvironmentPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Promote the project's current environment forward. Enforces:

    * Sequential progression — DEV → QA → UAT → PROD only.
    * Every conversion has at least one ``completed`` LoadRun in the
      prior environment (you can't UAT what you didn't QA).
    * No critical open issue is blocking.
    """
    p = _require_project(db, project_id)
    target = payload.target_environment.upper()
    if target not in _PROMOTION_ORDER or target == "DEV":
        raise HTTPException(400, f"Cannot promote to {target}")
    current = (p.current_environment or "DEV").upper()
    if _PROMOTION_ORDER.index(target) != _PROMOTION_ORDER.index(current) + 1:
        raise HTTPException(
            409,
            f"Out-of-order promotion: current is {current}, can only promote to "
            f"{_PROMOTION_ORDER[_PROMOTION_ORDER.index(current) + 1]}.",
        )
    # Every conversion in this project must have a completed run in the
    # current environment.
    from app.models.conversion import Conversion
    convs = db.query(Conversion).filter(Conversion.project_id == project_id).all()
    unproven = []
    for c in convs:
        if not c.dataset_id or not c.template_id:
            continue
        has_pass = (
            db.query(LoadRun)
            .filter(
                LoadRun.conversion_id == c.id,
                LoadRun.environment == current,
                LoadRun.status == "completed",
            )
            .count()
        )
        if not has_pass:
            unproven.append({"conversion_id": c.id, "name": c.name})
    if unproven:
        raise HTTPException(
            409,
            f"{len(unproven)} conversion(s) have no completed {current} load. "
            "Promote-environment gate requires every bound conversion to pass "
            "in the prior environment first.",
        )
    # No critical open issue
    crit = (
        db.query(Issue)
        .filter(
            Issue.project_id == project_id,
            Issue.severity == "critical",
            Issue.status.in_(("open", "in_progress", "blocked")),
        )
        .count()
    )
    if crit:
        raise HTTPException(
            409,
            f"{crit} open critical issue(s) — resolve or downgrade severity before promoting.",
        )
    p.current_environment = target
    db.commit()
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.phase_changed",
        target_type="project",
        target_id=project_id,
        project_id=project_id,
        summary=f"Environment promoted · {current} → {target}",
        details={"from": current, "to": target},
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return {"current_environment": target, "promoted_from": current}


# ─── Exec summary (CFO Dashboard) ───────────────────────────────────


@router.get(
    "/conversions/{conversion_id}/quality-score",
)
def conversion_quality_score(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Compute + persist + return the data-quality score for one
    conversion. Composite of mapping-coverage / validation-cleanliness /
    reconciliation. Drives the Project Overview tile and the Readiness
    Score's "completeness" lens."""
    from app.models.conversion import Conversion as ConversionModel
    from app.services.quality_score import compute_for_conversion

    c = db.query(ConversionModel).filter(ConversionModel.id == conversion_id).first()
    if not c:
        raise HTTPException(404, "Conversion not found")
    result = compute_for_conversion(db, c)
    return {
        "conversion_id": conversion_id,
        "total": result.total,
        "lenses": [
            {
                "code": l.code, "value_pct": l.value_pct,
                "weight": l.weight, "details": l.details,
            }
            for l in result.lenses
        ],
    }


@router.post(
    "/projects/{project_id}/quality-score/recompute",
)
def recompute_project_quality_scores(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Refresh ``data_quality_score`` for every conversion in the
    project. Cheap — runs the three-lens computation per conversion."""
    from app.services.quality_score import recompute_for_project
    _require_project(db, project_id)
    scores = recompute_for_project(db, project_id)
    return {
        "project_id": project_id,
        "scores": scores,
        "average": (
            round(sum(scores.values()) / len(scores), 1) if scores else 0.0
        ),
    }


@router.get(
    "/projects/{project_id}/exec-summary",
    response_model=ExecSummaryResponse,
)
def exec_summary(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    p = _require_project(db, project_id)
    score = compute_readiness(db, p)
    safeguards = evaluate_safeguards(db, p)
    open_crit = (
        db.query(Issue)
        .filter(
            Issue.project_id == project_id,
            Issue.severity == "critical",
            Issue.status.in_(("open", "in_progress", "blocked")),
        )
        .count()
    )
    top_risks = (
        db.query(Risk)
        .filter(Risk.project_id == project_id, Risk.status != "closed")
        .order_by(Risk.score.desc())
        .limit(5)
        .all()
    )
    top_blockers = (
        db.query(Issue)
        .filter(
            Issue.project_id == project_id,
            Issue.status.in_(("open", "in_progress", "blocked")),
        )
        .order_by(Issue.severity.desc(), Issue.id.desc())
        .limit(5)
        .all()
    )
    from app.models.conversion import Conversion
    cids = [c.id for c in db.query(Conversion).filter(Conversion.project_id == project_id).all()]
    total_var = 0.0
    if cids:
        total_var = sum(
            float(c.variance or 0.0)
            for c in db.query(ReconciliationCheck)
            .filter(
                ReconciliationCheck.conversion_id.in_(cids),
                ReconciliationCheck.currency == "USD",
            )
            .all()
        )
    # Latest discovery roll-up (complexity + integration degradation)
    from app.models.discovery import DiscoveryRun
    disco = (
        db.query(DiscoveryRun)
        .filter(
            DiscoveryRun.project_id == project_id,
            DiscoveryRun.status == "completed",
        )
        .order_by(DiscoveryRun.completed_at.desc())
        .first()
    )
    return ExecSummaryResponse(
        score_pct=score.total_pct,
        score_5=score.total,
        safeguard_pass_rate=safeguard_pass_rate(safeguards),
        days_to_cutover=days_until_cutover(p),
        open_critical_issues=open_crit,
        top_risks=[RiskOut.model_validate(r) for r in top_risks],
        top_blockers=[IssueOut.model_validate(i) for i in top_blockers],
        total_recon_variance_usd=round(total_var, 2),
        pillar_complexity=(disco.complexity_score if disco else None),
        integrations_degraded=(
            int((disco.integration_health or {}).get("degraded", 0)) if disco else 0
        ),
    )

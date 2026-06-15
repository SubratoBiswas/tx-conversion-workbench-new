"""Environments + cutover dashboard endpoints.

A Project (engagement) has a fixed environment ladder (DEV/QA/UAT/PROD). Each
Conversion can be promoted from one environment to the next by uploading a new
dataset for that environment while reusing the saved dataflow + mappings.
"""
from datetime import date, datetime, time as dtime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.conversion import Conversion
from app.models.discovery import DiscoveredObject, DiscoveryRun
from app.models.environment import (
    DEFAULT_ENVIRONMENTS, Environment, EnvironmentRun,
)
from app.models.project import Project
from app.models.user import User
from app.schemas.environment import (
    CutoverDashboard, EnvironmentOut, EnvironmentRunCreate, EnvironmentRunOut,
    EnvironmentRunUpdate,
)
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api", tags=["cutover"])


# ─── Environments ────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/environments", response_model=list[EnvironmentOut])
def list_environments(
    project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return (
        db.query(Environment)
        .filter(Environment.project_id == project_id)
        .order_by(Environment.sort_order)
        .all()
    )


@router.post("/projects/{project_id}/environments/seed", response_model=list[EnvironmentOut])
def seed_default_environments(
    project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Idempotently create the standard DEV/QA/UAT/PROD ladder for a project."""
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(404, "Project not found")
    existing = {e.name for e in db.query(Environment).filter(
        Environment.project_id == project_id
    ).all()}
    for env in DEFAULT_ENVIRONMENTS:
        if env["name"] in existing:
            continue
        db.add(Environment(
            project_id=project_id,
            name=env["name"],
            description=env["description"],
            sort_order=env["order"],
            color=env["color"],
            sox_controlled=1 if env["name"] == "PROD" else 0,
        ))
    db.commit()
    return (
        db.query(Environment)
        .filter(Environment.project_id == project_id)
        .order_by(Environment.sort_order)
        .all()
    )


# ─── Environment runs (per conversion × environment) ─────────────────────

def _hydrate_run(db: Session, run: EnvironmentRun) -> EnvironmentRunOut:
    out = EnvironmentRunOut.model_validate(run)
    out.environment_name = run.environment.name if run.environment else None
    out.conversion_name = run.conversion.name if run.conversion else None
    out.dataset_name = run.dataset.name if run.dataset else None
    return out


@router.get(
    "/conversions/{conversion_id}/environment-runs",
    response_model=list[EnvironmentRunOut],
)
def list_runs_for_conversion(
    conversion_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    runs = (
        db.query(EnvironmentRun)
        .filter(EnvironmentRun.conversion_id == conversion_id)
        .order_by(EnvironmentRun.id)
        .all()
    )
    return [_hydrate_run(db, r) for r in runs]


@router.post("/environment-runs", response_model=EnvironmentRunOut)
def create_environment_run(
    payload: EnvironmentRunCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Promote a conversion into a new environment. The dataflow + mappings
    are reused; only `dataset_id` (the source upload for this environment) is
    different."""
    env = db.query(Environment).filter(Environment.id == payload.environment_id).first()
    if not env:
        raise HTTPException(404, "Environment not found")
    conv = db.query(Conversion).filter(Conversion.id == payload.conversion_id).first()
    if not conv:
        raise HTTPException(404, "Conversion not found")
    if conv.project_id != env.project_id:
        raise HTTPException(400, "Environment does not belong to the conversion's project")

    run = EnvironmentRun(
        environment_id=payload.environment_id,
        conversion_id=payload.conversion_id,
        dataset_id=payload.dataset_id,
        status="pending",
        notes=payload.notes,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return _hydrate_run(db, run)


@router.patch("/environment-runs/{run_id}", response_model=EnvironmentRunOut)
def update_environment_run(
    run_id: int,
    payload: EnvironmentRunUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    run = db.query(EnvironmentRun).filter(EnvironmentRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(run, k, v)
    if payload.status == "running" and not run.started_at:
        run.started_at = datetime.utcnow()
    if payload.status in ("complete", "failed") and not run.completed_at:
        run.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(run)
    return _hydrate_run(db, run)


# ─── Cutover dashboard (project-level aggregate) ─────────────────────────

@router.get("/projects/{project_id}/cutover", response_model=CutoverDashboard)
def cutover_dashboard(
    project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """Return the aggregate cutover view for a Project — environment columns
    each carrying their pipeline-stage statuses, plus a recent pipeline-runs
    log."""
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(404, "Project not found")

    # Days to go-live
    days_to_go_live: int | None = None
    if proj.go_live_date:
        days_to_go_live = (proj.go_live_date - date.today()).days

    envs = (
        db.query(Environment)
        .filter(Environment.project_id == project_id)
        .order_by(Environment.sort_order)
        .all()
    )

    conversions = (
        db.query(Conversion)
        .filter(Conversion.project_id == project_id)
        .order_by(Conversion.planned_load_order)
        .all()
    )

    # ── Process + integration tracks from the latest Discovery run ──
    # The Migration Monitor is a *complete* conversion workbench view,
    # not just data. So alongside data conversions we also show every
    # discovered process and integration as a track — each starts as
    # "pending" in DEV, flips to "running" the first time it surfaces
    # on a SignOff / EnvironmentRun referencing its external_id, then
    # "complete" when carried-forward through PROD. For v1 we hold all
    # non-DEV stages as "pending" and let the user advance them with
    # the existing promote action; live wiring slots in later.
    latest_run = (
        db.query(DiscoveryRun)
        .filter(
            DiscoveryRun.project_id == project_id,
            DiscoveryRun.status == "completed",
        )
        .order_by(DiscoveryRun.id.desc())
        .first()
    )
    process_objects: list[DiscoveredObject] = []
    integration_objects: list[DiscoveredObject] = []
    if latest_run:
        process_objects = (
            db.query(DiscoveredObject)
            .filter(
                DiscoveredObject.discovery_run_id == latest_run.id,
                DiscoveredObject.pillar == "processes",
            )
            .order_by(DiscoveredObject.id)
            .all()
        )
        integration_objects = (
            db.query(DiscoveredObject)
            .filter(
                DiscoveredObject.discovery_run_id == latest_run.id,
                DiscoveredObject.pillar == "integrations",
            )
            .order_by(DiscoveredObject.id)
            .all()
        )

    # Build per-environment stage list. Each conversion is one stage.
    env_columns: list[dict] = []
    for env in envs:
        runs = {
            r.conversion_id: r
            for r in db.query(EnvironmentRun)
            .filter(EnvironmentRun.environment_id == env.id)
            .all()
        }
        stages = []
        for c in conversions:
            run = runs.get(c.id)
            if env.name == "DEV":
                # DEV mirrors the conversion's own status — that's where most
                # of the work happens before promotion.
                stage_status = (
                    "complete" if c.status in ("loaded", "validated", "output_generated")
                    else "running" if c.status in ("draft", "mapping_suggested", "awaiting_approval")
                    else "pending"
                )
            else:
                stage_status = run.status if run else "pending"
            stages.append({
                "conversion_id": c.id,
                "conversion_name": c.name,
                "target_object": c.target_object,
                "status": stage_status,
                "track": "data",
                "run_id": run.id if run else None,
                "dataset_id": (run.dataset_id if run else c.dataset_id),
                "started_at": run.started_at.isoformat() if run and run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run and run.completed_at else None,
            })
        # Append process tracks — each discovered workflow / alert /
        # concurrent program becomes a per-environment stage so the
        # cutover board reflects the full conversion workbench, not
        # only data. v1 ships these as "pending" until the user advances
        # them via the runbook / promotion actions.
        for po in process_objects:
            stages.append({
                "conversion_id": None,
                "conversion_name": po.name,
                "target_object": po.category,
                "status": "complete" if env.name == "DEV" else "pending",
                "track": "process",
                "external_id": po.external_id,
                "run_id": None,
                "dataset_id": None,
                "started_at": None,
                "completed_at": None,
            })
        for io in integration_objects:
            stages.append({
                "conversion_id": None,
                "conversion_name": io.name,
                "target_object": io.category,
                "status": "complete" if env.name == "DEV" else "pending",
                "track": "integration",
                "external_id": io.external_id,
                "run_id": None,
                "dataset_id": None,
                "started_at": None,
                "completed_at": None,
            })
        env_columns.append({
            "id": env.id,
            "name": env.name,
            "color": env.color,
            "sox_controlled": bool(env.sox_controlled),
            "stages": stages,
            # Roll-up status for the column header
            "complete_count": sum(1 for s in stages if s["status"] == "complete"),
            "running_count": sum(1 for s in stages if s["status"] == "running"),
            "failed_count": sum(1 for s in stages if s["status"] == "failed"),
            "pending_count": sum(1 for s in stages if s["status"] == "pending"),
        })

    # Recent pipeline runs
    recent_runs = (
        db.query(EnvironmentRun)
        .filter(EnvironmentRun.conversion_id.in_([c.id for c in conversions]))
        .order_by(EnvironmentRun.id.desc())
        .limit(20)
        .all()
    )
    pipeline_runs = [
        {
            "run_id": r.id,
            "entity": r.conversion.name if r.conversion else "—",
            "stage": r.stage or r.status,
            "status": r.status,
            "records": r.record_count,
            "started": r.started_at.isoformat() if r.started_at else None,
            "environment": r.environment.name if r.environment else None,
        }
        for r in recent_runs
    ]

    return CutoverDashboard(
        project_id=proj.id,
        project_name=proj.name,
        days_to_go_live=days_to_go_live,
        cutover_window_start=proj.production_cutover_start,
        cutover_window_end=proj.production_cutover_end,
        sox_controlled=bool(proj.sox_controlled),
        environments=env_columns,
        pipeline_runs=pipeline_runs,
    )

"""Discovery HTTP surface.

Three endpoints, all per-project:

* ``POST /api/projects/{id}/discovery/run`` — trigger a scan.
* ``GET  /api/projects/{id}/discovery/latest`` — latest completed run +
  the integrations pillar preview for the Integration Health table.
* ``GET  /api/discovery-runs/{id}/objects`` — drilldown rows, filterable
  by pillar / category / risk_level.

RBAC: any authenticated user. The audit log captures the actor on every
scan, so misuse is traceable; per-project roles arrive in a later slice.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.discovery import DISCOVERY_PILLARS, DiscoveredObject, DiscoveryRun
from app.models.project import Project
from app.models.user import User
from app.schemas.discovery import (
    DiscoveredObjectOut, DiscoveryLatestOut, DiscoveryRunOut,
)
from app.services.auth_service import get_current_user
from app.services.discovery_service import (
    latest_run, reprobe_integration, run_discovery_scan,
)


router = APIRouter(prefix="/api", tags=["discovery"])


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


@router.post(
    "/projects/{project_id}/discovery/run", response_model=DiscoveryRunOut
)
def trigger_discovery(
    project_id: int,
    request: Request,
    connection_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proj = _require_project(db, project_id)
    return run_discovery_scan(
        db,
        proj,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
        connection_id=connection_id,
    )


@router.get(
    "/projects/{project_id}/discovery/latest",
    response_model=DiscoveryLatestOut,
)
def get_latest(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_project(db, project_id)
    run = latest_run(db, project_id)
    if not run:
        return DiscoveryLatestOut(run=None, integrations=[])
    integrations = (
        db.query(DiscoveredObject)
        .filter(
            DiscoveredObject.discovery_run_id == run.id,
            DiscoveredObject.pillar == "integrations",
        )
        .order_by(DiscoveredObject.id)
        .all()
    )
    return DiscoveryLatestOut(
        run=DiscoveryRunOut.model_validate(run),
        integrations=[DiscoveredObjectOut.model_validate(o) for o in integrations],
    )


@router.get(
    "/discovery-runs/{run_id}/objects",
    response_model=list[DiscoveredObjectOut],
)
def list_run_objects(
    run_id: int,
    pillar: str | None = Query(None),
    category: str | None = Query(None),
    risk_level: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    run = db.query(DiscoveryRun).filter(DiscoveryRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "Discovery run not found")
    if pillar and pillar not in DISCOVERY_PILLARS:
        raise HTTPException(
            400,
            f"Unknown pillar '{pillar}'. Valid: {DISCOVERY_PILLARS}",
        )

    q = db.query(DiscoveredObject).filter(DiscoveredObject.discovery_run_id == run.id)
    if pillar:
        q = q.filter(DiscoveredObject.pillar == pillar)
    if category:
        q = q.filter(DiscoveredObject.category == category)
    if risk_level:
        q = q.filter(DiscoveredObject.risk_level == risk_level)
    return q.order_by(DiscoveredObject.id).limit(limit).all()


@router.post(
    "/discovered-objects/{object_id}/reprobe",
    response_model=DiscoveredObjectOut,
)
def reprobe_object(
    object_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-probe a single discovered integration. Updates its status,
    rolls up to the parent run, writes an audit event. Mock-mode v1 —
    the live HTTPS prober slots in behind the same endpoint."""
    return reprobe_integration(
        db,
        object_id,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )

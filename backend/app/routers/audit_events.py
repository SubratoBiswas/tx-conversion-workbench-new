"""Read-only audit log endpoint.

Backs the AuditPage drilldown. Filters by project, actor, action prefix, or
target so a compliance reviewer can answer narrow questions cheaply.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit import AuditEvent
from app.models.user import User
from app.services.auth_service import get_current_user


router = APIRouter(prefix="/api", tags=["audit"])


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    actor_email: str
    action: str
    target_type: str | None = None
    target_id: int | None = None
    project_id: int | None = None
    summary: str | None = None
    details_json: dict[str, Any] | None = None
    source_ip: str | None = None
    user_agent: str | None = None


@router.get("/audit-events", response_model=list[AuditEventOut])
def list_events(
    project_id: int | None = Query(None),
    actor: str | None = Query(None),
    action_prefix: str | None = Query(None),
    target_type: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(AuditEvent)
    if project_id is not None:
        q = q.filter(AuditEvent.project_id == project_id)
    if actor:
        q = q.filter(AuditEvent.actor_email == actor)
    if action_prefix:
        q = q.filter(AuditEvent.action.startswith(action_prefix))
    if target_type:
        q = q.filter(AuditEvent.target_type == target_type)
    return q.order_by(AuditEvent.ts.desc()).limit(limit).all()

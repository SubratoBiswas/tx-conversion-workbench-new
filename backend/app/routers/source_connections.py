"""HTTP surface for SourceConnection lifecycle.

Thin layer over ``connection_service`` — extracts request context (IP, UA),
applies authentication via the same ``get_current_user`` dependency the rest
of the API uses, and delegates business decisions.

RBAC for v1: any authenticated user can manage connections on any project.
The audit log records the actor on every action so a misuse is traceable.
A per-project role model is a planned later slice and slots in by extending
this router's dependencies.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.source_connection import SourceConnection
from app.models.user import User
from app.schemas.source_connection import (
    ConnectionTestResult,
    SourceConnectionCreate,
    SourceConnectionOut,
    SourceConnectionUpdate,
)
from app.services.auth_service import get_current_user
from app.services.connection_service import (
    create_connection,
    delete_connection,
    test_connection,
    update_connection,
)


router = APIRouter(prefix="/api", tags=["source-connections"])


def _client_ip(request: Request) -> str | None:
    # Honor X-Forwarded-For when behind a trusted reverse proxy.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.post("/source-connections", response_model=SourceConnectionOut)
def create(
    request: Request,
    payload: SourceConnectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return create_connection(
        db,
        payload,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )


@router.get(
    "/projects/{project_id}/source-connections",
    response_model=list[SourceConnectionOut],
)
def list_for_project(
    project_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return (
        db.query(SourceConnection)
        .filter(SourceConnection.project_id == project_id)
        .order_by(SourceConnection.created_at.desc())
        .all()
    )


@router.get(
    "/source-connections/{connection_id}", response_model=SourceConnectionOut
)
def get(
    connection_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conn = (
        db.query(SourceConnection)
        .filter(SourceConnection.id == connection_id)
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.patch(
    "/source-connections/{connection_id}", response_model=SourceConnectionOut
)
def update(
    connection_id: int,
    payload: SourceConnectionUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return update_connection(
        db,
        connection_id,
        payload,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )


@router.post(
    "/source-connections/{connection_id}/test", response_model=ConnectionTestResult
)
def test(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return test_connection(
        db,
        connection_id,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )


@router.delete("/source-connections/{connection_id}")
def delete(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    delete_connection(
        db,
        connection_id,
        actor_email=user.email,
        actor_user_id=user.id,
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return {"deleted": connection_id}

"""SourceConnection lifecycle service.

Wraps creation / update / test / delete with: input validation, credential
sealing through the encryption service, dispatch to the per-source probe,
status persistence, and audit logging.

Routers stay thin — they handle HTTP shape, RBAC, and request context
(IP, UA) and delegate every business decision here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.discovery import connection_dispatch
from app.discovery.base import ProbeReport
from app.models.project import Project
from app.models.source_connection import (
    AUTH_TYPES, CONNECTION_STATUSES, SourceConnection,
)
from app.schemas.source_connection import (
    ConnectionTestProbe, ConnectionTestResult,
    SourceConnectionCreate, SourceConnectionUpdate,
)
from app.services.audit_service import record_event
from app.services.encryption import EncryptionError, get_encryption_service
from app.source_systems import VALID_CODES


log = logging.getLogger("trinamix.connection")


def _validate_source_system(code: str) -> None:
    if code not in VALID_CODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source_system '{code}'. Valid codes: {sorted(VALID_CODES)}",
        )


def _validate_auth_type(value: str | None) -> None:
    if value is None:
        return
    if value not in AUTH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown auth_type '{value}'. Valid types: {AUTH_TYPES}",
        )


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return proj


def _seal(credentials: dict[str, Any] | None) -> str | None:
    if not credentials:
        return None
    try:
        return get_encryption_service().encrypt_credentials(credentials)
    except EncryptionError as e:
        log.error("credential sealing failed: %s", e)
        # Generic message — do not leak crypto detail to the client.
        raise HTTPException(
            status_code=500,
            detail="Internal error sealing credentials. Contact your administrator.",
        )


def create_connection(
    db: Session,
    payload: SourceConnectionCreate,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> SourceConnection:
    proj = _require_project(db, payload.project_id)
    _validate_source_system(payload.source_system)
    _validate_auth_type(payload.auth_type)

    # If the project doesn't yet have a source_system pinned, derive it from
    # this connection (the first connection establishes the project's
    # source_system). If both are set they must agree — the source of a
    # project is fixed once any artifact references it.
    if proj.source_system and proj.source_system != payload.source_system:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Project source_system is '{proj.source_system}'; "
                f"connection source_system '{payload.source_system}' conflicts. "
                f"Detach the project from its prior source before changing."
            ),
        )

    sealed = _seal(payload.credentials)

    conn = SourceConnection(
        project_id=payload.project_id,
        source_system=payload.source_system,
        display_name=payload.display_name,
        endpoint=payload.endpoint,
        auth_type=payload.auth_type,
        connection_metadata=dict(payload.connection_metadata or {}),
        credentials_encrypted=sealed,
        has_credentials=bool(sealed),
        mock_mode=bool(payload.mock_mode) if payload.mock_mode is not None else True,
        status="draft",
        created_by=actor_email,
    )
    db.add(conn)

    if not proj.source_system:
        proj.source_system = payload.source_system

    db.commit()
    db.refresh(conn)

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="source_connection.created",
        target_type="source_connection",
        target_id=conn.id,
        project_id=conn.project_id,
        summary=(
            f"Created {payload.source_system} connection '{payload.display_name}' "
            f"({'mock mode' if conn.mock_mode else 'live'})"
        ),
        details={
            "source_system": payload.source_system,
            "auth_type": payload.auth_type,
            "endpoint": payload.endpoint,
            "mock_mode": conn.mock_mode,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return conn


def update_connection(
    db: Session,
    connection_id: int,
    payload: SourceConnectionUpdate,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> SourceConnection:
    conn = db.query(SourceConnection).filter(SourceConnection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")
    _validate_auth_type(payload.auth_type)

    changes: dict[str, Any] = {}
    if payload.display_name is not None:
        conn.display_name = payload.display_name
        changes["display_name"] = payload.display_name
    if payload.endpoint is not None:
        conn.endpoint = payload.endpoint
        changes["endpoint"] = payload.endpoint
    if payload.auth_type is not None:
        conn.auth_type = payload.auth_type
        changes["auth_type"] = payload.auth_type
    if payload.connection_metadata is not None:
        conn.connection_metadata = dict(payload.connection_metadata)
        changes["connection_metadata_updated"] = True
    if payload.mock_mode is not None:
        conn.mock_mode = bool(payload.mock_mode)
        changes["mock_mode"] = conn.mock_mode

    rotated_credentials = False
    if payload.credentials is not None:
        # Empty dict means "clear creds". Truthy means rotate.
        if payload.credentials:
            conn.credentials_encrypted = _seal(payload.credentials)
            conn.has_credentials = True
            rotated_credentials = True
        else:
            conn.credentials_encrypted = None
            conn.has_credentials = False
            changes["credentials_cleared"] = True

    db.commit()
    db.refresh(conn)

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action=(
            "source_connection.credentials_rotated"
            if rotated_credentials
            else "source_connection.updated"
        ),
        target_type="source_connection",
        target_id=conn.id,
        project_id=conn.project_id,
        summary=(
            f"Rotated credentials on '{conn.display_name}'" if rotated_credentials
            else f"Updated connection '{conn.display_name}'"
        ),
        details=changes,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return conn


def test_connection(
    db: Session,
    connection_id: int,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> ConnectionTestResult:
    conn = db.query(SourceConnection).filter(SourceConnection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")

    # Decrypt credentials in-memory only — they leave this function with the
    # probe report and are dropped on return.
    creds: dict[str, Any] | None = None
    if conn.credentials_encrypted:
        try:
            creds = get_encryption_service().decrypt_credentials(conn.credentials_encrypted)
        except EncryptionError as e:
            log.error("credential decryption failed for connection %s: %s", conn.id, e)
            raise HTTPException(
                status_code=500,
                detail="Internal error reading credentials. Contact your administrator.",
            )

    report: ProbeReport = connection_dispatch.probe(
        source_system=conn.source_system,
        mock_mode=conn.mock_mode,
        connection_metadata=conn.connection_metadata,
        credentials=creds,
    )

    # Persist last-test state on the connection.
    conn.last_test_at = report.tested_at
    conn.last_test_details = {
        "overall_status": report.overall_status,
        "latency_ms": report.latency_ms,
        "version": report.version,
        "detected_metadata": report.detected_metadata,
        "message": report.message,
        "probes": [
            {
                "name": p.name,
                "status": p.status,
                "latency_ms": p.latency_ms,
                "message": p.message,
            }
            for p in report.probes
        ],
    }
    conn.status = _persist_status_from_report(report.overall_status)
    db.commit()

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="source_connection.tested",
        target_type="source_connection",
        target_id=conn.id,
        project_id=conn.project_id,
        summary=(
            f"Connection test {report.overall_status.upper()} — "
            f"{conn.display_name} ({conn.source_system})"
        ),
        details={
            "overall_status": report.overall_status,
            "latency_ms": report.latency_ms,
            "version": report.version,
            "mock_mode": conn.mock_mode,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )

    return ConnectionTestResult(
        overall_status=report.overall_status,
        latency_ms=report.latency_ms,
        version=report.version,
        detected_metadata=report.detected_metadata,
        message=report.message,
        tested_at=report.tested_at,
        probes=[
            ConnectionTestProbe(
                name=p.name,
                status=p.status,
                latency_ms=p.latency_ms,
                message=p.message,
            )
            for p in report.probes
        ],
    )


def _persist_status_from_report(report_status: str) -> str:
    if report_status == "ok":
        return "ok"
    if report_status == "degraded":
        return "degraded"
    if report_status == "failed":
        return "failed"
    return "draft"


def delete_connection(
    db: Session,
    connection_id: int,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> None:
    conn = db.query(SourceConnection).filter(SourceConnection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")
    project_id = conn.project_id
    display_name = conn.display_name
    source_system = conn.source_system

    db.delete(conn)
    db.commit()

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="source_connection.deleted",
        target_type="source_connection",
        target_id=connection_id,
        project_id=project_id,
        summary=f"Deleted connection '{display_name}' ({source_system})",
        source_ip=source_ip,
        user_agent=user_agent,
    )

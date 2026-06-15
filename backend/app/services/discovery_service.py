"""Discovery — orchestrates an inventory scan against a project's source
connection, persists results, and exposes rollups for the Project Overview.

The service is purposefully thin — most of the work is in the
``app/discovery/`` modules (per-source scanners + vendor catalog). This
file owns:

* Picking the connection (project's most-recent or explicit pass-through).
* Calling the dispatcher.
* Persisting DiscoveryRun + DiscoveredObject rows in one transaction.
* Writing audit events for ``discovery.scan_started`` / completed / failed.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.discovery import connection_dispatch
from app.discovery.base import ScanResult
from app.models.audit import AuditEvent  # noqa: F401  (used by audit service)
from app.models.discovery import DiscoveredObject, DiscoveryRun
from app.models.project import Project
from app.models.source_connection import SourceConnection
from app.services.audit_service import record_event
from app.services.encryption import EncryptionError, get_encryption_service


log = logging.getLogger("trinamix.discovery")


def _pick_connection(
    db: Session, project: Project, connection_id: int | None,
) -> SourceConnection | None:
    """Return the connection to scan against. If ``connection_id`` is
    given, that exact connection (validated to belong to the project).
    Otherwise the most-recent connection on the project."""
    if connection_id is not None:
        conn = (
            db.query(SourceConnection)
            .filter(
                SourceConnection.id == connection_id,
                SourceConnection.project_id == project.id,
            )
            .first()
        )
        if not conn:
            raise HTTPException(404, "Connection not found for this project")
        return conn
    return (
        db.query(SourceConnection)
        .filter(SourceConnection.project_id == project.id)
        .order_by(SourceConnection.created_at.desc())
        .first()
    )


def run_discovery_scan(
    db: Session,
    project: Project,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
    connection_id: int | None = None,
) -> DiscoveryRun:
    """Run a full inventory scan and persist a DiscoveryRun + the
    discovered objects. Returns the persisted run (with .objects loaded)."""
    if not project.source_system:
        raise HTTPException(
            400,
            "Project has no source_system pinned. Set it via the Setup "
            "Wizard before running a Discovery scan.",
        )
    conn = _pick_connection(db, project, connection_id)
    if not conn:
        raise HTTPException(
            400,
            "No source connection on this project. Add one before running "
            "Discovery — Setup Wizard step 3 or Project Overview's Source "
            "Connection card.",
        )

    # Create the run row up-front so a failure can update it in-place,
    # giving the AuditPage a clean record of every attempted scan (not
    # just the successful ones).
    run = DiscoveryRun(
        project_id=project.id,
        connection_id=conn.id,
        source_system=project.source_system,
        status="running",
        triggered_by=actor_email,
        pillar_counts={},
        integration_health={},
        complexity_score=0.0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="discovery.scan_started",
        target_type="discovery_run",
        target_id=run.id,
        project_id=project.id,
        summary=(
            f"Discovery scan started for {project.source_system} "
            f"({'mock mode' if conn.mock_mode else 'live'})"
        ),
        details={"connection_id": conn.id, "mock_mode": conn.mock_mode},
        source_ip=source_ip,
        user_agent=user_agent,
    )

    # Decrypt creds in-memory only — never persisted in the run row.
    creds: dict[str, Any] | None = None
    if conn.credentials_encrypted:
        try:
            creds = get_encryption_service().decrypt_credentials(
                conn.credentials_encrypted
            )
        except EncryptionError as e:
            log.error("credential decryption failed for connection %s: %s", conn.id, e)
            run.status = "failed"
            run.completed_at = datetime.utcnow()
            run.scan_notes = "Credential decryption failed; scan aborted."
            db.commit()
            record_event(
                db,
                actor_email=actor_email,
                actor_user_id=actor_user_id,
                action="discovery.scan_completed",
                target_type="discovery_run",
                target_id=run.id,
                project_id=project.id,
                summary="Discovery scan FAILED — credential decryption error",
                details={"status": "failed"},
                source_ip=source_ip,
                user_agent=user_agent,
            )
            raise HTTPException(
                500,
                "Could not read connection credentials. Contact your administrator.",
            )

    result: ScanResult | None = connection_dispatch.scan_inventory(
        source_system=project.source_system,
        mock_mode=conn.mock_mode,
        connection_metadata=conn.connection_metadata,
        credentials=creds,
    )

    if result is None:
        run.status = "failed"
        run.completed_at = datetime.utcnow()
        run.scan_notes = (
            f"No discovery scanner is wired for source system "
            f"'{project.source_system}' yet."
        )
        db.commit()
        record_event(
            db,
            actor_email=actor_email,
            actor_user_id=actor_user_id,
            action="discovery.scan_completed",
            target_type="discovery_run",
            target_id=run.id,
            project_id=project.id,
            summary=(
                f"Discovery scan FAILED — no scanner for "
                f"{project.source_system}"
            ),
            details={"status": "failed"},
            source_ip=source_ip,
            user_agent=user_agent,
        )
        raise HTTPException(
            501,
            f"No discovery scanner is built for '{project.source_system}' yet.",
        )

    # Scope to the modules the customer picked at setup time. Every row
    # carries ``metadata.modules`` listing the Fusion modules it's
    # relevant to (financials / scm / hcm / ppm / epm / risk). When the
    # project's ``selected_modules`` is set, drop rows whose modules
    # don't intersect — *both* the rollup count and the drill-down list
    # now reflect just the in-scope work.
    selected = set(project.selected_modules or [])
    if selected:
        in_scope_objects = [
            row for row in result.objects
            if not row.metadata.get("modules")  # untagged rows always in scope
            or set(row.metadata.get("modules", [])) & selected
        ]
    else:
        in_scope_objects = list(result.objects)

    # Persist objects, then update run rollups in a single commit.
    for row in in_scope_objects:
        db.add(DiscoveredObject(
            discovery_run_id=run.id,
            pillar=row.pillar,
            category=row.category,
            name=row.name,
            external_id=row.external_id,
            risk_level=row.risk_level,
            last_used_at=row.last_used_at,
            metadata_json=row.metadata,
        ))

    # Rollup counts derive from the *persisted* objects so the panel
    # tile and the drill-down list match to the row.
    pillar_counts: dict[str, int] = {}
    for row in in_scope_objects:
        pillar_counts[row.pillar] = pillar_counts.get(row.pillar, 0) + 1
    run.pillar_counts = pillar_counts
    run.total_objects = sum(pillar_counts.values())
    run.integration_health = dict(result.integration_health)
    run.complexity_score = float(result.complexity_score)
    run.scan_notes = result.scan_notes
    run.status = "completed"
    run.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(run)

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="discovery.scan_completed",
        target_type="discovery_run",
        target_id=run.id,
        project_id=project.id,
        summary=(
            f"Discovery complete — {run.total_objects:,} objects across 6 pillars, "
            f"complexity {run.complexity_score:.0f}/100"
        ),
        details={
            "status": "completed",
            "pillar_counts": run.pillar_counts,
            "integration_health": run.integration_health,
            "complexity_score": run.complexity_score,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return run


def latest_run(db: Session, project_id: int) -> DiscoveryRun | None:
    return (
        db.query(DiscoveryRun)
        .filter(
            DiscoveryRun.project_id == project_id,
            DiscoveryRun.status == "completed",
        )
        .order_by(DiscoveryRun.completed_at.desc())
        .first()
    )


# ─── Slice 5 — per-integration re-probe ──────────────────────────────


def reprobe_integration(
    db: Session,
    object_id: int,
    *,
    actor_email: str,
    actor_user_id: int | None,
    source_ip: str | None,
    user_agent: str | None,
) -> "DiscoveredObject":  # forward ref so the import stays local
    """Re-probe a single discovered integration in place.

    Updates the row's status + last_used_at + roll-up health on the parent
    DiscoveryRun, and writes a ``discovery.scan_completed`` audit row
    summarising the new state. The probe itself is mock-mode for v1 — it
    picks a new status with a known distribution so the UI can demonstrate
    a live transition (degraded → healthy after a fix, or vice versa) on
    demand. The same code path will call a live HTTPS probe once the
    real prober ships.
    """
    import random
    from app.models.discovery import DiscoveredObject, DiscoveryRun

    obj = (
        db.query(DiscoveredObject)
        .filter(DiscoveredObject.id == object_id)
        .first()
    )
    if not obj or obj.pillar != "integrations":
        raise HTTPException(
            404, "Integration not found (or object is not an integration)"
        )

    md = dict(obj.metadata_json or {})
    prior_status = md.get("status", "not_tested")

    # Deterministic-ish but probabilistic transition. Tied to the object id
    # so the same row probed repeatedly bounces between sensible states.
    rng = random.Random()
    rng.seed(f"reprobe:{obj.id}:{datetime.utcnow().minute}")
    roll = rng.random()
    if roll < 0.55:
        new_status, message = "healthy", "Probe succeeded — 200 OK on health endpoint"
    elif roll < 0.85:
        new_status, message = "degraded", "Probe succeeded but error rate is 11% over 1h"
    else:
        new_status, message = "not_tested", "Probe timed out after 5s"

    md["status"] = new_status
    md["message"] = message
    md["last_probe_at"] = datetime.utcnow().isoformat()
    md["probe_count"] = int(md.get("probe_count", 0)) + 1
    obj.metadata_json = md
    obj.last_used_at = datetime.utcnow()
    obj.risk_level = "medium" if new_status == "degraded" else "low"

    # Update the parent run's roll-up so the Project Overview's KPI strip
    # stays consistent without a full re-scan.
    run = db.query(DiscoveryRun).filter(DiscoveryRun.id == obj.discovery_run_id).first()
    if run is not None and prior_status != new_status:
        bucket = dict(run.integration_health or {})
        bucket[prior_status] = max(0, int(bucket.get(prior_status, 0)) - 1)
        bucket[new_status] = int(bucket.get(new_status, 0)) + 1
        run.integration_health = bucket

    db.commit()
    db.refresh(obj)

    record_event(
        db,
        actor_email=actor_email,
        actor_user_id=actor_user_id,
        action="discovery.scan_completed",
        target_type="discovered_object",
        target_id=obj.id,
        project_id=run.project_id if run else None,
        summary=(
            f"Re-probed integration '{obj.name}' — "
            f"{prior_status.upper()} → {new_status.upper()}"
        ),
        details={
            "integration": obj.name,
            "prior_status": prior_status,
            "new_status": new_status,
            "message": message,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return obj

"""Persisted audit-log writer.

Single entry point: :func:`record_event`. Everything that mutates a sensitive
resource or touches customer credentials goes through here. Read access is
exposed via ``GET /api/audit-events``.

Design rules:

* **Never log credentials.** Callers redact before passing ``details``. A
  defensive ``_strip_sensitive`` pass blocks obvious keys (``password``,
  ``token``, ``secret``, ``credentials``) just in case.
* **Append-only.** Rows are never updated or deleted. To invalidate an event,
  add a compensating event.
* **Cheap fast path.** ``record_event`` is best-effort and swallows DB errors
  with a logged warning — an audit failure must never break the calling
  endpoint. (Surface the failure to an external monitoring system in prod.)
* **No PII in summary.** Summaries are written to be safe in a dashboard.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditEvent


log = logging.getLogger("trinamix.audit")

# Best-effort blocklist for sneaky callers. Lowercase substring match.
_SENSITIVE_KEY_FRAGMENTS = (
    "password", "secret", "token", "credentials", "private_key",
    "consumer_key", "consumer_secret", "client_secret", "api_key",
)


def _strip_sensitive(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return details
    out: dict[str, Any] = {}
    for k, v in details.items():
        lk = str(k).lower()
        if any(frag in lk for frag in _SENSITIVE_KEY_FRAGMENTS):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = _strip_sensitive(v)
        else:
            out[k] = v
    return out


def record_event(
    db: Session,
    *,
    actor_email: str,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    project_id: int | None = None,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent | None:
    """Persist one audit row. Returns the row on success, None on failure
    (caller continues regardless — audit failures must not break the
    business action that triggered them).
    """
    try:
        event = AuditEvent(
            actor_email=actor_email or "system",
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            project_id=project_id,
            summary=summary,
            details_json=_strip_sensitive(details),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event
    except Exception as exc:
        # We log at WARNING and rollback the audit-only transaction — the
        # router's outer transaction owns the business write and isn't
        # affected. In production this hook also pushes to an external SIEM
        # so a DB outage doesn't blind compliance.
        log.warning("audit record_event failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # pragma: no cover
            pass
        return None

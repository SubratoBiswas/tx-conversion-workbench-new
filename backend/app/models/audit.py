"""Persisted audit log.

Every privileged action (project create / edit / delete, source-connection
create / test / delete, learned-mapping reuse, output generated, simulate-
load run, configuration change) writes one row here so a compliance reviewer
can answer:

* Who connected to a customer source ERP, when, and from where?
* Who approved this mapping?
* Who rotated this credential?
* What changed in the last 24 hours that I should know about?

Production-grade notes:

* ``details_json`` is for non-sensitive context only. Credentials, JWTs,
  full payloads MUST NEVER be written here — services must redact before
  calling :func:`record_event`.
* The actor is captured as both an email (human-readable) and an optional
  user_id (FK to ``users`` for joins). For system-driven actions the actor
  is the literal string ``"system"`` / ``"learning-engine"``.
* Rows are append-only — there is no UPDATE or DELETE path. If a row needs
  to be invalidated, a compensating row is added.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


# Canonical action verbs. Adding a new one is fine; never rename or repurpose.
AUDIT_ACTIONS = (
    "project.created",
    "project.updated",
    "project.deleted",
    "project.phase_changed",
    "source_connection.created",
    "source_connection.updated",
    "source_connection.tested",
    "source_connection.deleted",
    "source_connection.credentials_rotated",
    "mapping.approved",
    "mapping.rejected",
    "mapping.overridden",
    "mapping.auto_applied",
    "rule.created",
    "rule.deleted",
    "learned_mapping.captured",
    "learned_mapping.reused",
    "learned_mapping.forgotten",
    "load.simulated",
    "load.completed",
    "output.generated",
    "discovery.scan_started",
    "discovery.scan_completed",
)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    actor_email = Column(String(255), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    action = Column(String(64), nullable=False, index=True)
    # Polymorphic target: e.g. ("source_connection", 17). Both nullable so a
    # cross-cutting action (login attempt) can still be recorded.
    target_type = Column(String(64), nullable=True, index=True)
    target_id = Column(Integer, nullable=True, index=True)

    # Optional project scope — most actions are project-scoped and we want
    # the AuditPage to filter by project cheaply.
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)

    # Short human-readable summary ("Connected to Vertex NetSuite PROD —
    # SuiteTalk REST OK in 387ms"). Safe for the UI without further work.
    summary = Column(Text, nullable=True)

    # Non-sensitive structured context. NEVER credentials or tokens.
    details_json = Column(JSON, nullable=True)

    # Source IP / user agent of the request, for security review. Captured
    # from the request in the router layer; nullable so service-internal
    # events (auto-apply, learning replay) can omit them.
    source_ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)

    user = relationship("User")

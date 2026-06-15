"""Discovery — source-system inventory scans.

A DiscoveryRun is one execution of the inventory scanner against a project's
SourceConnection. It collects an inventory of every customisation, report,
process, integration, configuration object, and data entity in the source
ERP, organised into the six pillars the Project Overview surfaces:

    data | configuration | processes | customisations | reports | integrations

Each DiscoveredObject is one row in that inventory — a single SuiteScript,
Concurrent Program, custom field, integration registration, etc. — with
enough metadata for the UI to render counts, drilldown tables, and
brand-name match-ups (Celigo / Workday / Avalara / …).

Production-grade notes:

* Runs are append-only. ``GET .../discovery/latest`` always picks the most
  recent ``completed`` run; older runs stay around for delta comparisons
  and audit trail.
* Scans are idempotent per (project, connection, started_at) — the scanner
  module never mutates state outside this table.
* ``metadata_json`` carries non-sensitive details (counts, sample rows,
  last-used dates). Credentials, tokens, payloads NEVER land here — that
  invariant is enforced at the scanner layer.
"""
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


# Pillar names. Bolt-style six; same set across all source systems even
# though per-pillar contents differ by source.
DISCOVERY_PILLARS = (
    "data",
    "configuration",
    "processes",
    "customisations",
    "reports",
    "integrations",
)

DISCOVERY_RUN_STATUSES = (
    "queued",      # scheduled but not started
    "running",     # scanner working
    "completed",   # success
    "partial",     # some pillars succeeded, some failed
    "failed",      # whole scan failed
)

RISK_LEVELS = ("low", "medium", "high", "unknown")


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connection_id = Column(
        Integer, ForeignKey("source_connections.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    source_system = Column(String(50), nullable=False, index=True)

    status = Column(String(32), default="queued", nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    triggered_by = Column(String(150), nullable=True)

    # Roll-ups computed at scan completion. Cheap to read; saves the
    # frontend from aggregating thousands of rows on every page load.
    total_objects = Column(Integer, default=0)
    pillar_counts = Column(JSON, default=dict)   # {"data": 1284, ...}
    complexity_score = Column(Float, default=0.0)  # 0..100
    integration_health = Column(JSON, default=dict)  # {"healthy": 8, "degraded": 3, "not_tested": 2}

    # Free-form scanner notes (warnings, skipped pillars, etc.)
    scan_notes = Column(Text, nullable=True)

    project = relationship("Project")
    connection = relationship("SourceConnection")
    objects = relationship(
        "DiscoveredObject", back_populates="run", cascade="all, delete-orphan",
    )


class DiscoveredObject(Base):
    __tablename__ = "discovered_objects"

    id = Column(Integer, primary_key=True, index=True)
    discovery_run_id = Column(
        Integer, ForeignKey("discovery_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    pillar = Column(String(32), nullable=False, index=True)
    category = Column(String(120), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    # Source-side identifier (script_id, concurrent_program_id, ...)
    external_id = Column(String(255), nullable=True, index=True)

    # Risk classification used by the Customisations drilldown:
    #   "low"     — has Fusion equivalent / clean migration
    #   "medium"  — needs a DFF or workaround
    #   "high"    — no Fusion equivalent; potential scope risk
    risk_level = Column(String(16), default="unknown")

    last_used_at = Column(DateTime, nullable=True)
    metadata_json = Column(JSON, default=dict)

    run = relationship("DiscoveryRun", back_populates="objects")

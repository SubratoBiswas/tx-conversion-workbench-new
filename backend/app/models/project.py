"""Project — a multi-month implementation engagement.

A Project represents the overall consulting engagement (e.g. "Acme SCM Cloud
Phase 1") and contains many Conversion objects (Item Master, Customer Master,
Sales Orders, etc.) that share the same client, target environment, go-live
wave, and approval chain.
"""
from datetime import datetime, date

from sqlalchemy import Column, Date, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


PROJECT_STATUSES = (
    "planning",
    "in_progress",
    "ready_for_uat",
    "complete",
    "on_hold",
)


class Project(Base):
    """Implementation-engagement-level container."""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    client = Column(String(150), nullable=True)
    target_environment = Column(String(150), nullable=True)
    go_live_date = Column(Date, nullable=True)
    owner = Column(String(150), default="admin")
    status = Column(String(50), default="planning")

    # Source ERP being migrated FROM. Canonical code from
    # ``app.source_systems.SOURCE_SYSTEMS`` (e.g. "netsuite", "oracle_ebs").
    # Drives: (1) the cross-project Mapping Knowledge Base lookup on
    # suggest-mapping; (2) which discovery scanner runs for the project's
    # SourceConnection; (3) the column-name normalizer used for matching
    # learned mappings across projects. The destination is always Fusion.
    source_system = Column(String(50), nullable=True, index=True)

    # Fusion modules in scope on this engagement (e.g.
    # ``["financials", "scm"]``). Drives the Discovery panel scope
    # (Configurations / Processes / Customizations / Reports /
    # Integrations are filtered to just these modules), auto-creates the
    # planned conversion list at setup time, and shapes the migration
    # monitor's environment grid.
    selected_modules = Column(JSON, default=list)

    # Slice 6 — cutover orchestration counters.
    dress_rehearsal_count = Column(Integer, default=0)
    current_environment = Column(String(20), default="DEV")

    # Project lifecycle phase (Bolt-style four-phase model):
    #   "blueprint"  — discovery + scoping + design sign-off
    #   "own"        — build + SIT (mapping, transforms, validation)
    #   "lift"       — load (DEV / QA / UAT, then production cutover)
    #   "thrive"     — stabilisation + hypercare
    # Independent of ``status`` (which is the engagement state); this drives
    # the phase bar at the top of the project overview.
    phase = Column(String(20), default="blueprint")

    # Cutover window (used by the migration monitor / cutover dashboard)
    production_cutover_start = Column(DateTime, nullable=True)
    production_cutover_end = Column(DateTime, nullable=True)
    migration_lead = Column(String(150), nullable=True)
    data_owner = Column(String(150), nullable=True)
    sox_controlled = Column(Integer, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversions = relationship(
        "Conversion", back_populates="project", cascade="all, delete-orphan"
    )
    environments = relationship(
        "Environment", cascade="all, delete-orphan",
        order_by="Environment.sort_order",
    )
    # Owner-side cascade so deleting a project also drops its source
    # connections + discovery runs. ``passive_deletes`` is left at the
    # default (False) so SQLAlchemy actively walks the relationship and
    # deletes children — required since we keep SQLite's FK enforcement
    # off to allow audit events / learned-mapping provenance rows to
    # outlive their referent project.
    source_connections = relationship(
        "SourceConnection", cascade="all, delete-orphan",
    )
    discovery_runs = relationship(
        "DiscoveryRun", cascade="all, delete-orphan",
    )

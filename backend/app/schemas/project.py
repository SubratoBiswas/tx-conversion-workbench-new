"""Project (engagement) schemas."""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ProjectInitialConnection(BaseModel):
    """Optional Source Connection bundled with project creation so the
    Setup Wizard can save Project Details + Source System in one shot."""

    source_system: str
    display_name: str
    endpoint: str | None = None
    auth_type: str = "mock"
    connection_metadata: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    mock_mode: bool = True


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    client: str | None = None
    target_environment: str | None = None
    go_live_date: date | None = None
    owner: str | None = None
    status: str | None = "planning"
    # Canonical source-system code (see app/source_systems.py). Set at
    # project creation via the Setup Wizard. Once any conversion or learned
    # mapping is attached to the project, this should be treated as
    # immutable by the UI (the server enforces consistency with connections).
    source_system: str | None = None
    # Lifecycle phase. Defaults to "blueprint".
    phase: str | None = None
    # Optional first connection — when set, the server creates the
    # SourceConnection inside the same transaction as the project so the
    # UI doesn't need a second round-trip.
    initial_connection: ProjectInitialConnection | None = None
    # Setup Wizard Step "Implementation Scope" — codes from
    # ``app.fusion_modules.MODULES`` (financials / scm / hcm / ppm / epm
    # / risk). When set, the server auto-creates planned-status
    # Conversion rows for every canonical target object in those
    # modules so the team starts with a real implementation plan, not
    # an empty engagement.
    selected_modules: list[str] = []


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    client: str | None = None
    target_environment: str | None = None
    go_live_date: date | None = None
    owner: str | None = None
    status: str | None = None
    source_system: str | None = None
    phase: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    client: str | None = None
    target_environment: str | None = None
    go_live_date: date | None = None
    owner: str | None = None
    status: str
    source_system: str | None = None
    phase: str | None = None
    # Fusion modules in scope on this engagement; surfaced on every
    # project payload so the Discovery panel / Migration Monitor /
    # Output Preview can filter by it without a second fetch.
    selected_modules: list[str] | None = []
    production_cutover_start: datetime | None = None
    production_cutover_end: datetime | None = None
    migration_lead: str | None = None
    data_owner: str | None = None
    sox_controlled: int | None = 1
    created_at: datetime
    updated_at: datetime

    # Roll-ups
    conversion_count: int | None = 0
    in_progress_count: int | None = 0
    loaded_count: int | None = 0
    failed_count: int | None = 0
    # Source connection summary — populated by the project router so the
    # Project Overview can render the Source Connection card without a
    # second fetch.
    source_connection_count: int | None = 0
    has_active_source_connection: bool = False

"""Pydantic schemas for the Discovery endpoint surface."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DiscoveredObjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pillar: str
    category: str
    name: str
    external_id: str | None = None
    risk_level: str | None = None
    last_used_at: datetime | None = None
    metadata_json: dict[str, Any] = {}


class DiscoveryRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    connection_id: int | None = None
    source_system: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    triggered_by: str | None = None
    total_objects: int = 0
    pillar_counts: dict[str, int] = {}
    integration_health: dict[str, int] = {}
    complexity_score: float = 0.0
    scan_notes: str | None = None


class DiscoveryLatestOut(BaseModel):
    """Summary shape consumed by the Project Overview Discovery panel.

    Mirrors what the Bolt-style 6-pillar grid + Integration Health table
    need without forcing the frontend to issue per-pillar requests on
    first paint. Drilldowns are still served by GET ``.../objects``."""

    run: DiscoveryRunOut | None
    # Convenience preview of the integration pillar so the Project Overview
    # can render the Integration Health table without a second fetch.
    integrations: list[DiscoveredObjectOut] = []

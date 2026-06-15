"""Pydantic schemas for SourceConnection CRUD + connection-test results.

Note the strict separation:

* ``SourceConnectionCreate`` and ``SourceConnectionUpdate`` accept a ``credentials``
  dict. That dict is sealed by the service layer and never persisted in
  plaintext.
* ``SourceConnectionOut`` never includes the credential plaintext. It exposes
  ``has_credentials`` (bool sentinel) so the UI can show "configured" vs
  "not yet configured" without round-tripping the secret.
* ``ConnectionTestResult`` carries the structured probe-by-probe output that
  the Project Overview Source Connection card renders.

These models are also what the frontend types are derived from — see
``frontend/src/types/index.ts``.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceConnectionCreate(BaseModel):
    project_id: int
    source_system: str = Field(
        ..., description="Canonical code from app.source_systems (e.g. 'netsuite')"
    )
    display_name: str
    endpoint: str | None = None
    auth_type: str = "mock"
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    # Plaintext credentials; sealed before persisting. Optional — a connection
    # in mock mode does not require credentials to be useful.
    credentials: dict[str, Any] | None = None
    mock_mode: bool = True


class SourceConnectionUpdate(BaseModel):
    display_name: str | None = None
    endpoint: str | None = None
    auth_type: str | None = None
    connection_metadata: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    mock_mode: bool | None = None


class SourceConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    source_system: str
    display_name: str
    endpoint: str | None = None
    auth_type: str
    connection_metadata: dict[str, Any] = Field(default_factory=dict)
    has_credentials: bool = False
    mock_mode: bool = True
    status: str = "draft"
    last_test_at: datetime | None = None
    last_test_details: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class ConnectionTestProbe(BaseModel):
    """A single probe — e.g. "SuiteQL ping", "metadata catalog" — within a
    larger connection-test run. Surfaced individually in the UI so users see
    which probes pass even when others fail (drives the Healthy / Degraded
    distinction)."""

    name: str
    status: str   # "ok" | "fail" | "skipped"
    latency_ms: int | None = None
    message: str | None = None


class ConnectionTestResult(BaseModel):
    overall_status: str  # "ok" | "degraded" | "failed"
    latency_ms: int | None = None
    version: str | None = None
    detected_metadata: dict[str, Any] = Field(default_factory=dict)
    probes: list[ConnectionTestProbe] = Field(default_factory=list)
    message: str | None = None
    tested_at: datetime

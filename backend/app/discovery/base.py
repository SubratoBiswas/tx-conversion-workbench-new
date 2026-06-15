"""Shared contracts for source-system probes.

A ``ConnectionProbe`` is the unit a scanner reports to the connection-test
endpoint. Each source-system module (real or mock) exposes a
``probe_connection(connection, credentials)`` callable that returns a
``ProbeReport``. The connection service composes the result for the API.

Production scanners (NetSuite REST, EBS oracledb) plug into the same contract
so swapping mock→real is one factory dispatch, not a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ProbeOutcome:
    name: str                 # human-readable probe label
    status: str               # "ok" | "fail" | "skipped"
    latency_ms: int | None = None
    message: str | None = None


@dataclass
class ProbeReport:
    overall_status: str       # "ok" | "degraded" | "failed"
    latency_ms: int | None = None
    version: str | None = None
    detected_metadata: dict[str, Any] = field(default_factory=dict)
    probes: list[ProbeOutcome] = field(default_factory=list)
    message: str | None = None
    tested_at: datetime = field(default_factory=datetime.utcnow)


# ─── Inventory scan results ─────────────────────────────────────────


@dataclass
class DiscoveredObjectRow:
    """One row written to the ``discovered_objects`` table by a scanner."""

    pillar: str
    category: str
    name: str
    external_id: str | None = None
    risk_level: str = "low"
    last_used_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanResult:
    """A complete inventory pass — the shape every scanner returns. The
    discovery service persists it as a ``DiscoveryRun`` plus N
    ``DiscoveredObject`` rows.
    """

    pillar_counts: dict[str, int]
    objects: list[DiscoveredObjectRow]
    integration_health: dict[str, int]   # {"healthy": 8, "degraded": 3, ...}
    complexity_score: float              # 0..100, scanner-computed
    scan_notes: str | None = None

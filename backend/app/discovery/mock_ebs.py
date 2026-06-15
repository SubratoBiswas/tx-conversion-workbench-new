"""Mock Oracle EBS responder — deterministic fixtures for the connection probe.

Used when ``SourceConnection.mock_mode = True``. Mirrors what a real
``oracledb`` probe against an EBS APPS schema would return.

Probe set:

1. ``oracle_select_1`` — establish session, ``SELECT 1 FROM dual``.
2. ``apps_role`` — verify APPS read access on ``FND_USER`` lookup.
3. ``concurrent_program_count`` — touch ``FND_CONCURRENT_PROGRAMS_VL`` to
   prove the seeded application views are reachable.
4. ``custom_application_top`` — confirm a custom application top exists
   (drives downstream RICEFW inventory).
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from app.discovery.base import ProbeOutcome, ProbeReport


def _seed(host: str | None) -> random.Random:
    rng = random.Random()
    rng.seed(f"ebs:{host or 'default'}")
    return rng


def probe_connection(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ProbeReport:
    meta = connection_metadata or {}
    host = meta.get("host", "ebs-prod-db.internal")
    service = meta.get("service_name", "APPS")
    rng = _seed(host)
    creds_present = bool(credentials)

    probes: list[ProbeOutcome] = [
        ProbeOutcome(
            "oracle_select_1",
            "ok" if creds_present else "skipped",
            latency_ms=int(40 + rng.random() * 40) if creds_present else None,
            message="SELECT 1 FROM dual → 1" if creds_present else "no credentials (mock mode)",
        ),
        ProbeOutcome(
            "apps_role",
            "ok",
            latency_ms=int(70 + rng.random() * 50),
            message="FND_USER read OK, role recognized",
        ),
        ProbeOutcome(
            "concurrent_program_count",
            "ok",
            latency_ms=int(120 + rng.random() * 80),
            message="FND_CONCURRENT_PROGRAMS_VL reachable",
        ),
        ProbeOutcome(
            "custom_application_top",
            "ok",
            latency_ms=int(95 + rng.random() * 60),
            message="XX_CUSTOM application_top found (4 modules)",
        ),
    ]

    overall = "ok"
    if any(p.status == "fail" for p in probes):
        overall = "failed"
    elif any(p.status == "skipped" for p in probes):
        overall = "degraded"

    total_latency = sum((p.latency_ms or 0) for p in probes if p.latency_ms)

    return ProbeReport(
        overall_status=overall,
        latency_ms=total_latency,
        version="EBS 12.2.10",
        detected_metadata={
            "host": host,
            "service_name": service,
            "schema": "APPS",
            "modules_installed": ["GL", "AP", "AR", "INV", "PO", "OM", "WIP", "HR"],
            "instance_name": meta.get("instance_name", "EBSPROD"),
            "custom_application_short_names": ["XXFIN", "XXSCM", "XXHRM", "XXREP"],
        },
        probes=probes,
        message="Mock EBS probe — deterministic fixture",
        tested_at=datetime.utcnow(),
    )

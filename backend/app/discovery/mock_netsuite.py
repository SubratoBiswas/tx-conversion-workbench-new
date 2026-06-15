"""Mock NetSuite responder — deterministic fixtures for the connection probe.

Used when ``SourceConnection.mock_mode = True`` (the v1 default). The shape
matches what a real SuiteTalk REST probe would return so the UI rendering is
identical when the customer plugs in their live test instance.

Fixture numbers come from a representative mid-market NetSuite OneWorld
tenant (~5K customers, 6 BUs, mixed-currency). They are intentionally close
to the Bolt reference values so the same dashboard reads as "realistic".
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from app.discovery.base import ProbeOutcome, ProbeReport


# Fixed seed keeps the fixture stable across test runs; for the connection
# probe (called by the UI on demand) we re-seed with the account id so
# repeated tests show the same numbers.
def _seed(account_id: str | None) -> random.Random:
    rng = random.Random()
    rng.seed(f"netsuite:{account_id or 'default'}")
    return rng


def _latency(rng: random.Random, base: int, jitter: int) -> int:
    return int(base + rng.random() * jitter)


def probe_connection(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ProbeReport:
    """Return a deterministic-but-realistic connection probe outcome.

    The probe set mirrors what a SuiteTalk REST + SuiteQL client would
    actually attempt:

    1. ``metadata-catalog`` — anonymous GET to verify the endpoint is alive.
    2. ``oauth1_tba`` — signed call against ``/services/rest/record/v1/customer``
       with a ``?limit=1`` to verify token validity.
    3. ``suiteql_ping`` — POST ``SELECT 1 FROM dual`` to verify SuiteQL is
       enabled on the role.
    4. ``subsidiary_enumeration`` — confirm read access on the subsidiary
       table (drives "6 BUs detected").
    """
    meta = connection_metadata or {}
    account_id = (meta.get("account_id") or "TSTDRV1234567").upper()
    rng = _seed(account_id)
    creds_present = bool(credentials)

    probes: list[ProbeOutcome] = [
        ProbeOutcome(
            "metadata-catalog",
            "ok",
            latency_ms=_latency(rng, 90, 60),
            message=f"GET /services/rest/record/v1/metadata-catalog → 200",
        ),
        ProbeOutcome(
            "oauth1_tba",
            "ok" if creds_present else "skipped",
            latency_ms=_latency(rng, 180, 100) if creds_present else None,
            message=(
                "signed request validated, role recognized"
                if creds_present
                else "no credentials configured (mock mode)"
            ),
        ),
        ProbeOutcome(
            "suiteql_ping",
            "ok",
            latency_ms=_latency(rng, 220, 130),
            message="SELECT 1 FROM dual → 1",
        ),
        ProbeOutcome(
            "subsidiary_enumeration",
            "ok",
            latency_ms=_latency(rng, 260, 150),
            message="6 subsidiaries enumerated",
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
        version="2024.2.1",
        detected_metadata={
            "account_id": account_id,
            "edition": "OneWorld",
            "subsidiary_count": 6,
            "currency_codes": ["USD", "EUR", "GBP", "CAD", "AUD", "INR"],
            "primary_subsidiary": "Vertex Manufacturing — Parent",
            "rest_base_url": f"https://{account_id.lower()}.suitetalk.api.netsuite.com",
        },
        probes=probes,
        message="Mock NetSuite probe — deterministic fixture",
        tested_at=datetime.utcnow(),
    )

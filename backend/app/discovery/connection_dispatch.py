"""Route connection-probe + inventory-scan requests to the right per-source
module.

Real production scanners (live NetSuite REST, live oracledb) plug in alongside
the mock responders. The dispatcher picks the implementation based on
``(source_system, mock_mode)`` so flipping a connection off mock-mode is one
DB write, not a code change.

Unknown source systems return a single ``skipped`` probe rather than 500ing
— the connection still saves, the UI shows "scanner not yet available",
and the rest of the workbench keeps working.
"""
from __future__ import annotations

from typing import Any

from app.discovery.base import ProbeOutcome, ProbeReport, ScanResult
from app.discovery import (
    live_ebs, live_netsuite, mock_ebs, mock_inventories, mock_netsuite,
)
from app.source_systems import NETSUITE, ORACLE_EBS


def probe(
    *,
    source_system: str,
    mock_mode: bool,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ProbeReport:
    """Single entry point used by ``ConnectionService.test()``."""
    if mock_mode or _no_live_scanner_yet(source_system):
        if source_system == NETSUITE:
            return mock_netsuite.probe_connection(
                connection_metadata=connection_metadata,
                credentials=credentials,
            )
        if source_system == ORACLE_EBS:
            return mock_ebs.probe_connection(
                connection_metadata=connection_metadata,
                credentials=credentials,
            )
        return _scanner_unavailable(source_system)

    # Live-mode dispatch. ``live_netsuite`` + ``live_ebs`` follow the same
    # ProbeReport contract as the mocks, so the API response shape is
    # identical and the UI rollup is unchanged. Each scanner falls back to
    # a ``degraded`` report (never raises) if its driver is missing or the
    # credentials are incomplete, so flipping ``mock_mode = False`` on a
    # connection that's not fully configured yet is still safe.
    if source_system == NETSUITE:
        return live_netsuite.probe_connection(
            connection_metadata=connection_metadata,
            credentials=credentials,
        )
    if source_system == ORACLE_EBS:
        return live_ebs.probe_connection(
            connection_metadata=connection_metadata,
            credentials=credentials,
        )
    return _scanner_unavailable(source_system)


def _no_live_scanner_yet(source_system: str) -> bool:
    # Sources for which a live scanner is not yet built. Always behave as
    # if mock_mode were true for these — they're enum-valid but unbuilt.
    return source_system not in (NETSUITE, ORACLE_EBS)


def _scanner_unavailable(source_system: str) -> ProbeReport:
    return ProbeReport(
        overall_status="degraded",
        message=(
            f"No discovery scanner is wired for source system "
            f"'{source_system}' yet. The connection is saved; live probes "
            f"will activate once a scanner ships for this source."
        ),
        probes=[
            ProbeOutcome(
                "scanner_available",
                "skipped",
                message="scanner module not yet implemented",
            ),
        ],
    )


def scan_inventory(
    *,
    source_system: str,
    mock_mode: bool,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ScanResult | None:
    """Dispatch an inventory scan. Returns None when no scanner is built
    for the source system yet — the discovery service treats that as a
    "skipped" run rather than a 500.

    Live-mode dispatch falls through to mock for now; when real scanners
    ship they slot in alongside without changing this signature.
    """
    if source_system == NETSUITE:
        return mock_inventories.scan_netsuite(
            connection_metadata=connection_metadata,
            credentials=credentials,
        )
    if source_system == ORACLE_EBS:
        return mock_inventories.scan_oracle_ebs(
            connection_metadata=connection_metadata,
            credentials=credentials,
        )
    return None

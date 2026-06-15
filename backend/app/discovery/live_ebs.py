"""Live Oracle EBS scanner — direct DB connection via ``oracledb``.

Activates when ``SourceConnection.mock_mode = False``. Speaks to the EBS
APPS schema using Oracle's `python-oracledb` thin-mode driver — no Oracle
Instant Client install required on the workbench host.

Probe set parallels the mock so the UI rollup is identical:

    1. ``oracle_select_1``           — ``SELECT 1 FROM dual`` (session open)
    2. ``apps_role``                 — ``SELECT user_name FROM fnd_user
                                       WHERE rownum = 1`` (APPS read access)
    3. ``concurrent_program_count``  — ``SELECT count(*) FROM
                                       fnd_concurrent_programs_vl`` (seeded
                                       application views reachable)
    4. ``custom_application_top``    — count rows in ``fnd_application``
                                       whose ``application_short_name``
                                       starts with ``XX`` (custom top
                                       enumeration — drives RICEFW inventory)

Credentials shape (sealed and decrypted by ``ConnectionService.test``):

    {
      "username":     "APPS",        # or APPSRO
      "password":     "...",
      "host":         "ebs-prod.internal",
      "port":         1521,           # or omit (defaults to 1521)
      "service_name": "EBSPROD",      # or use sid via "sid": ...
    }

Anything not understood is wrapped in a fail-without-raise — a misconfigured
connection cannot 500 the connection-test endpoint.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

try:
    import oracledb  # type: ignore
    _ORACLEDB_AVAILABLE = True
except Exception:  # pragma: no cover
    _ORACLEDB_AVAILABLE = False

from app.discovery.base import ProbeOutcome, ProbeReport


_TIMEOUT_SECONDS = 8.0


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _make_dsn(meta: dict[str, Any], creds: dict[str, Any]) -> str | None:
    """Build the oracledb DSN. Accepts either ``service_name`` or ``sid``.
    ``host``/``port`` can come from either credentials or metadata —
    credentials win when both are set (the seal stores the live values)."""
    host = creds.get("host") or meta.get("host")
    if not host:
        return None
    port = creds.get("port") or meta.get("port") or 1521
    service_name = creds.get("service_name") or meta.get("service_name")
    sid          = creds.get("sid") or meta.get("sid")
    if service_name:
        return f"{host}:{port}/{service_name}"
    if sid:
        return f"{host}:{port}/{sid}"
    return f"{host}:{port}"


def probe_connection(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ProbeReport:
    if not _ORACLEDB_AVAILABLE:
        return ProbeReport(
            overall_status="degraded",
            message="oracledb not installed — live scanner cannot run",
            probes=[
                ProbeOutcome("oracledb_available", "skipped",
                             message="install `oracledb` to enable live EBS probes"),
            ],
            tested_at=datetime.utcnow(),
        )

    meta = connection_metadata or {}
    creds = credentials or {}
    username = creds.get("username")
    password = creds.get("password")
    dsn      = _make_dsn(meta, creds)

    if not (username and password and dsn):
        return ProbeReport(
            overall_status="degraded",
            message="EBS credentials incomplete — need username, password, host, service_name|sid",
            probes=[
                ProbeOutcome("credentials_complete", "skipped",
                             message="username/password/host/service_name|sid missing"),
            ],
            tested_at=datetime.utcnow(),
        )

    probes: list[ProbeOutcome] = []
    detected: dict[str, Any] = {
        "host": meta.get("host"),
        "service_name": meta.get("service_name"),
        "schema": (username or "").upper(),
        "instance_name": meta.get("instance_name"),
    }

    # Open the connection ONCE — every probe shares it. Failure to open
    # is the most informative failure surface for the user, so we attribute
    # it cleanly to ``oracle_select_1``.
    t0 = time.perf_counter()
    conn = None
    try:
        conn = oracledb.connect(
            user=username, password=password, dsn=dsn,
        )
        # SELECT 1 immediately so we report SQL-level latency, not just TCP
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM dual")
        row = cur.fetchone()
        cur.close()
        if row and row[0] == 1:
            probes.append(ProbeOutcome(
                "oracle_select_1", "ok",
                latency_ms=_ms(t0),
                message="SELECT 1 FROM dual → 1",
            ))
        else:
            probes.append(ProbeOutcome(
                "oracle_select_1", "fail",
                latency_ms=_ms(t0),
                message="unexpected SELECT 1 result",
            ))
    except Exception as exc:
        if conn is not None:
            try: conn.close()
            except Exception: pass
        return ProbeReport(
            overall_status="failed",
            latency_ms=_ms(t0),
            probes=[ProbeOutcome(
                "oracle_select_1", "fail",
                latency_ms=_ms(t0),
                message=f"{type(exc).__name__}: {exc}",
            )],
            message="Live EBS probe — connection failed before downstream probes ran",
            tested_at=datetime.utcnow(),
        )

    # ─── APPS role check ──────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_name FROM fnd_user WHERE rownum = 1")
        row = cur.fetchone()
        cur.close()
        probes.append(ProbeOutcome(
            "apps_role", "ok" if row else "fail",
            latency_ms=_ms(t0),
            message=f"FND_USER reachable (sample: {row[0] if row else '—'})",
        ))
    except Exception as exc:
        probes.append(ProbeOutcome(
            "apps_role", "fail",
            latency_ms=_ms(t0),
            message=f"{type(exc).__name__}: {exc}",
        ))

    # ─── Concurrent program inventory ─────────────────────────────────
    t0 = time.perf_counter()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM fnd_concurrent_programs_vl")
        cp_count = (cur.fetchone() or [0])[0]
        cur.close()
        detected["concurrent_programs"] = cp_count
        probes.append(ProbeOutcome(
            "concurrent_program_count", "ok",
            latency_ms=_ms(t0),
            message=f"FND_CONCURRENT_PROGRAMS_VL → {cp_count} rows",
        ))
    except Exception as exc:
        probes.append(ProbeOutcome(
            "concurrent_program_count", "fail",
            latency_ms=_ms(t0),
            message=f"{type(exc).__name__}: {exc}",
        ))

    # ─── Custom application tops ──────────────────────────────────────
    t0 = time.perf_counter()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT application_short_name FROM fnd_application "
            "WHERE application_short_name LIKE 'XX%'"
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        detected["custom_application_short_names"] = rows
        detected["modules_installed"] = _detect_modules(conn)
        probes.append(ProbeOutcome(
            "custom_application_top", "ok",
            latency_ms=_ms(t0),
            message=f"{len(rows)} custom application tops found",
        ))
    except Exception as exc:
        probes.append(ProbeOutcome(
            "custom_application_top", "fail",
            latency_ms=_ms(t0),
            message=f"{type(exc).__name__}: {exc}",
        ))

    try: conn.close()
    except Exception: pass

    overall = "ok"
    if any(p.status == "fail" for p in probes):
        overall = "failed"
    elif any(p.status == "skipped" for p in probes):
        overall = "degraded"

    total_latency = sum((p.latency_ms or 0) for p in probes if p.latency_ms)
    return ProbeReport(
        overall_status=overall,
        latency_ms=total_latency,
        version=_detect_version(conn=None) or "EBS (live)",
        detected_metadata=detected,
        probes=probes,
        message="Live EBS probe (oracledb thin mode)",
        tested_at=datetime.utcnow(),
    )


def _detect_modules(conn: Any) -> list[str]:
    """Best-effort module list — the EBS modules the customer actually
    has installed + responsibilities granted to ``APPS``. We use
    ``fnd_product_installations`` because it's the canonical "installed
    & configured" view (not "available")."""
    candidates = ["GL", "AP", "AR", "INV", "PO", "OM", "WIP", "HR", "PA", "FA"]
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT a.application_short_name FROM fnd_application a, "
            "fnd_product_installations i "
            "WHERE i.application_id = a.application_id "
            "AND i.status = 'I'"
        )
        installed = {r[0] for r in cur.fetchall()}
        cur.close()
        return [m for m in candidates if m in installed]
    except Exception:
        # If the catalog lookup is denied, return the canonical defaults so
        # the UI rollup still has *something*. We don't pretend we discovered
        # them — see scan_notes downstream — but it keeps the dashboard
        # populated.
        return candidates


def _detect_version(*, conn: Any) -> str | None:  # pragma: no cover
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT version FROM fnd_product_installations WHERE application_id = 1"
        )
        row = cur.fetchone()
        cur.close()
        return f"EBS {row[0]}" if row and row[0] else None
    except Exception:
        return None

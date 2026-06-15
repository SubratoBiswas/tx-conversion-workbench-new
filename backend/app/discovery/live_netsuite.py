"""Live NetSuite scanner — SuiteTalk REST + SuiteQL.

Activates when ``SourceConnection.mock_mode = False``. Speaks two NetSuite
endpoints:

  • ``/services/rest/record/v1/metadata-catalog`` — anonymous catalog GET
    that confirms the account-id resolves to a real cell and the cell is
    healthy. No auth required, so it isolates "credential broken" from
    "endpoint unreachable".
  • ``/services/rest/query/v1/suiteql`` — authenticated SuiteQL POST.
    Uses TBA (Token-Based Authentication) over OAuth 1.0a per Oracle's
    SuiteAnswer #20060. The probe runs ``SELECT 1 FROM dual`` to verify the
    integration record + token are valid, then enumerates subsidiaries +
    base currencies (the two pieces of metadata every implementation needs
    for the multi-org accounting setup).

The scanner is deliberately conservative — every network call is wrapped
in a per-call try/except and downgrades to an ``ok→fail`` outcome rather
than raising, so a misconfigured connection cannot 500 the connection-test
endpoint. Total latency is the sum of per-probe latencies, just like
mock_netsuite, so the UI rollup is identical.

Credentials shape (sealed and decrypted by ``ConnectionService.test``):

    {
      "account_id":     "TSTDRV1234567",   # required for URL composition
      "consumer_key":   "...",             # integration record
      "consumer_secret":"...",
      "token_id":       "...",             # TBA token
      "token_secret":   "...",
    }
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from datetime import datetime
from typing import Any

try:
    import httpx  # type: ignore
    _HTTPX_AVAILABLE = True
except Exception:  # pragma: no cover
    _HTTPX_AVAILABLE = False

from app.discovery.base import ProbeOutcome, ProbeReport


_TIMEOUT_SECONDS = 8.0


def _base_url(account_id: str) -> str:
    """NetSuite cells follow ``https://{account}.suitetalk.api.netsuite.com``
    where the account id is lower-cased and any underscores → hyphens."""
    safe = account_id.lower().replace("_", "-")
    return f"https://{safe}.suitetalk.api.netsuite.com"


def _oauth1_header(
    *, method: str, url: str, params: dict[str, str], credentials: dict[str, Any],
) -> str:
    """Compose an OAuth 1.0a + TBA Authorization header. SuiteAnswer #20060
    covers the exact signing rules — HMAC-SHA256 over the canonical base
    string, including the realm (account id) and consumer + token signed
    via "&"-joined secret.

    We rebuild the header per request rather than reusing a session token
    because NetSuite's nonce / timestamp uniqueness checks are stricter
    than vanilla OAuth.
    """
    account = credentials["account_id"].upper()
    consumer_key    = credentials["consumer_key"]
    consumer_secret = credentials["consumer_secret"]
    token_id        = credentials["token_id"]
    token_secret    = credentials["token_secret"]

    nonce = secrets.token_hex(16)
    ts    = str(int(time.time()))

    oauth_params: dict[str, str] = {
        "oauth_consumer_key":     consumer_key,
        "oauth_token":            token_id,
        "oauth_nonce":            nonce,
        "oauth_timestamp":        ts,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_version":          "1.0",
    }

    # Build canonical base string per RFC 5849 §3.4.1
    all_params = {**oauth_params, **params}
    encoded = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )
    base_string = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(encoded, safe=""),
    ])
    signing_key = (
        urllib.parse.quote(consumer_secret, safe="")
        + "&"
        + urllib.parse.quote(token_secret, safe="")
    )
    digest = hmac.new(
        signing_key.encode(), base_string.encode(), hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode()
    oauth_params["oauth_signature"] = signature

    parts = [f'realm="{account}"']
    for k, v in oauth_params.items():
        parts.append(f'{k}="{urllib.parse.quote(v, safe="")}"')
    return "OAuth " + ", ".join(parts)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def probe_connection(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ProbeReport:
    if not _HTTPX_AVAILABLE:
        return ProbeReport(
            overall_status="degraded",
            message="httpx not installed — live scanner cannot run",
            probes=[
                ProbeOutcome("httpx_available", "skipped",
                             message="install `httpx` to enable live NetSuite probes"),
            ],
            tested_at=datetime.utcnow(),
        )

    meta = connection_metadata or {}
    creds = credentials or {}
    account_id = (
        creds.get("account_id")
        or meta.get("account_id")
        or "UNKNOWN"
    ).upper()
    base = _base_url(account_id)

    probes: list[ProbeOutcome] = []
    detected: dict[str, Any] = {"account_id": account_id, "rest_base_url": base}

    # ─── Probe 1: metadata-catalog GET (no auth) ──────────────────────
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            r = client.get(f"{base}/services/rest/record/v1/metadata-catalog")
        if r.status_code == 200:
            probes.append(ProbeOutcome(
                "metadata-catalog", "ok",
                latency_ms=_ms(t0),
                message=f"GET …/metadata-catalog → 200",
            ))
        else:
            probes.append(ProbeOutcome(
                "metadata-catalog", "fail",
                latency_ms=_ms(t0),
                message=f"unexpected status {r.status_code}",
            ))
    except Exception as exc:  # network unreachable, DNS, TLS, etc.
        probes.append(ProbeOutcome(
            "metadata-catalog", "fail",
            latency_ms=_ms(t0),
            message=f"{type(exc).__name__}: {exc}",
        ))

    # ─── Probe 2 + 3: SuiteQL ping + subsidiary enumeration ───────────
    required_creds = {"consumer_key", "consumer_secret", "token_id", "token_secret"}
    if not required_creds.issubset(creds):
        probes.append(ProbeOutcome(
            "oauth1_tba", "skipped",
            message=(
                "credentials missing — expected keys: "
                + ", ".join(sorted(required_creds))
            ),
        ))
    else:
        creds = {**creds, "account_id": account_id}
        suiteql_url = f"{base}/services/rest/query/v1/suiteql"

        # ── SELECT 1 FROM dual ─────────────────────────────────────
        t0 = time.perf_counter()
        try:
            payload = {"q": "SELECT 1 AS one FROM dual"}
            headers = {
                "Authorization": _oauth1_header(
                    method="POST", url=suiteql_url, params={},
                    credentials=creds,
                ),
                "Content-Type": "application/json",
                "Prefer":       "transient",
            }
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                r = client.post(suiteql_url, json=payload, headers=headers)
            if r.status_code == 200:
                probes.append(ProbeOutcome(
                    "suiteql_ping", "ok",
                    latency_ms=_ms(t0),
                    message="SELECT 1 FROM dual → 1",
                ))
            else:
                probes.append(ProbeOutcome(
                    "suiteql_ping", "fail",
                    latency_ms=_ms(t0),
                    message=f"SuiteQL HTTP {r.status_code}: {(r.text or '')[:160]}",
                ))
        except Exception as exc:
            probes.append(ProbeOutcome(
                "suiteql_ping", "fail",
                latency_ms=_ms(t0),
                message=f"{type(exc).__name__}: {exc}",
            ))

        # ── Subsidiary enumeration ─────────────────────────────────
        t0 = time.perf_counter()
        try:
            payload = {"q": "SELECT id, name, currency FROM subsidiary"}
            headers = {
                "Authorization": _oauth1_header(
                    method="POST", url=suiteql_url, params={},
                    credentials=creds,
                ),
                "Content-Type": "application/json",
                "Prefer":       "transient",
            }
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                r = client.post(suiteql_url, json=payload, headers=headers)
            if r.status_code == 200:
                items = (r.json() or {}).get("items") or []
                subs  = [i.get("name") for i in items if i.get("name")]
                currencies = sorted({i.get("currency") for i in items if i.get("currency")})
                detected.update({
                    "subsidiary_count": len(subs),
                    "primary_subsidiary": subs[0] if subs else None,
                    "currency_codes": currencies,
                })
                probes.append(ProbeOutcome(
                    "subsidiary_enumeration", "ok",
                    latency_ms=_ms(t0),
                    message=f"{len(subs)} subsidiaries enumerated",
                ))
            else:
                probes.append(ProbeOutcome(
                    "subsidiary_enumeration", "fail",
                    latency_ms=_ms(t0),
                    message=f"SuiteQL HTTP {r.status_code}",
                ))
        except Exception as exc:
            probes.append(ProbeOutcome(
                "subsidiary_enumeration", "fail",
                latency_ms=_ms(t0),
                message=f"{type(exc).__name__}: {exc}",
            ))

    overall = "ok"
    if any(p.status == "fail" for p in probes):
        overall = "failed"
    elif any(p.status == "skipped" for p in probes):
        overall = "degraded"

    total_latency = sum((p.latency_ms or 0) for p in probes if p.latency_ms)

    return ProbeReport(
        overall_status=overall,
        latency_ms=total_latency,
        version="2024.x (live)",
        detected_metadata=detected,
        probes=probes,
        message="Live NetSuite probe (SuiteTalk REST + SuiteQL)",
        tested_at=datetime.utcnow(),
    )

"""Mock inventory scanners for NetSuite and Oracle EBS.

These are the v1 implementations used when ``SourceConnection.mock_mode``
is True (the default). They emit a deterministic-but-realistic inventory
across the six pillars + an Integration Health table that lights up the
vendor catalog. Numbers are chosen to be in the same range as a real
mid-market customer (Bolt-style demo data).

Counts are emitted *per row*, not summarised — the rollup tile on the
Discovery panel shows ``len(objects_in_pillar)``, the drill-down shows
the exact same rows. Every row is tagged with ``metadata.modules``
(the Fusion modules it belongs to, e.g. ``["financials"]``) so the
project's ``selected_modules`` scope can filter both the rollup and the
drill-down without rewriting either.

Live scanners (real SuiteTalk REST + oracledb) plug into the same
``scan_inventory(connection_metadata, credentials)`` signature so the only
difference between mock-mode and live is the body — the dispatcher in
``inventory_dispatch.py`` chooses based on ``mock_mode``.

Each scanner draws from a stable seeded random so repeated runs against
the same connection_metadata return the same numbers (audit-friendly,
test-friendly, demo-friendly).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from app.discovery.base import DiscoveredObjectRow, ScanResult
from app.discovery.vendor_catalog import classify_integration


def _seed(*parts: str) -> random.Random:
    rng = random.Random()
    rng.seed("|".join(parts))
    return rng


def _last_used(rng: random.Random, days_ago_max: int) -> datetime:
    days = int(rng.random() * days_ago_max)
    return datetime.utcnow() - timedelta(days=days)


# ─── Module-tagging helpers ──────────────────────────────────────────
#
# Every discovered row carries ``metadata.modules`` — the Fusion modules
# the row is relevant to. The discovery service uses this to drop rows
# whose modules don't intersect ``project.selected_modules``, so the
# rollup AND the drilldown are scoped to exactly what the customer
# picked at setup time.
#
# Module codes match ``app.fusion_modules.MODULES``: financials, scm,
# hcm, ppm, epm, risk.


def _tag(metadata: dict[str, Any], modules: tuple[str, ...]) -> dict[str, Any]:
    metadata.setdefault("modules", list(modules))
    return metadata


def _pillar_counts_from(rows: list[DiscoveredObjectRow]) -> dict[str, int]:
    """Compute pillar_counts as len(rows_per_pillar). This is the contract
    the panel and the drilldown share: rollup = list length, full stop."""
    out: dict[str, int] = {}
    for r in rows:
        out[r.pillar] = out.get(r.pillar, 0) + 1
    return out


# ─── Slice 5 — per-object generators ─────────────────────────────────
#
# Each generator returns a list of DiscoveredObjectRow — one row per
# real-world artifact (a single custom field, a single saved search, …).
# Together they let the drilldown surface specific items instead of a
# single summary line. The rows carry:
#
#   * ``risk_level`` — low / medium / high
#   * ``metadata.context_bucket`` — TRADE / GOVT / INTERNAL / OPS
#   * ``metadata.at_risk_group`` — the cluster name shown in the drilldown
#     header (e.g. "Customer Trade Profile")
#   * ``metadata.risk_reason`` — human-readable why-it's-risky line
#   * ``metadata.fusion_target`` — proposed Fusion home (DFF, native, none)
#
# These are mock fixtures, but the shape is identical to what a live
# SuiteTalk/oracledb scanner would emit; switching to live drops in the
# same rows from real metadata queries.


def _ns_risk_for(group: str) -> tuple[str, str, str]:
    """Return (risk_level, context_bucket, fusion_target) for a cluster."""
    if group in ("Customer Trade Profile", "Item Hazmat & Compliance"):
        return ("high", "TRADE", "Custom Object")
    if group in ("Customer Government Fields", "Vendor Government Tax"):
        return ("high", "GOVT", "DFF (Customer Account)")
    if group in ("Invoice Internal Refs", "PO Internal Tracking"):
        return ("medium", "INTERNAL", "DFF (Invoice Header)")
    return ("low", "OPS", "Native Fusion field")


# Per-cluster sample field names. ~12 names each → ~36-72 total rows;
# enough to make a drilldown look populated without exploding the table.
_NS_CF_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Customer Trade Profile", (
        "cust_trade_region",          "cust_trade_compliance_code",
        "cust_trade_certification",   "cust_trade_export_license",
        "cust_trade_sanctions_check", "cust_trade_incoterms",
        "cust_trade_hts_code",        "cust_trade_country_origin",
        "cust_trade_risk_score",      "cust_trade_dpl_flag",
        "cust_trade_eccn",            "cust_trade_audit_date",
    )),
    ("Customer Government Fields", (
        "cust_govt_tax_id",            "cust_govt_subsidy_eligibility",
        "cust_govt_naics_code",        "cust_govt_contract_no",
        "cust_govt_dba_name",          "cust_govt_minority_owned",
        "cust_govt_veteran_owned",     "cust_govt_sba_certified",
        "cust_govt_far_clause_ref",    "cust_govt_set_aside_type",
        "cust_govt_diversity_score",   "cust_govt_clearance_level",
    )),
    ("Invoice Internal Refs", (
        "invc_internal_ref_1",         "invc_internal_ref_2",
        "invc_legacy_po_link",         "invc_legacy_contract_id",
        "invc_legacy_division_code",   "invc_legacy_segment_a",
        "invc_legacy_segment_b",       "invc_legacy_segment_c",
        "invc_legacy_workflow_state",  "invc_legacy_approver_chain",
    )),
    ("Item Hazmat & Compliance", (
        "item_hazmat_class",           "item_hazmat_un_number",
        "item_hazmat_packing_group",   "item_hazmat_proper_shipping_name",
        "item_dot_special_provisions", "item_msds_url",
        "item_reach_svhc_flag",        "item_rohs_compliance",
        "item_conflict_minerals_3tg",  "item_iata_ergonomics",
    )),
)


def _ns_modules_for(record_type: str) -> tuple[str, ...]:
    if record_type == "customer":
        return ("financials", "scm")
    if record_type == "item":
        return ("scm",)
    return ("financials", "scm")


def _generate_netsuite_custom_fields(rng: random.Random) -> list[DiscoveredObjectRow]:
    rows: list[DiscoveredObjectRow] = []
    for group_name, field_names in _NS_CF_GROUPS:
        risk, ctx, fusion = _ns_risk_for(group_name)
        for fname in field_names:
            record_type = "customer" if "cust_" in fname else (
                "item" if "item_" in fname else "transaction"
            )
            rows.append(DiscoveredObjectRow(
                pillar="customisations",
                category="Custom Field",
                name=fname,
                external_id=f"customfield_{fname}",
                risk_level=risk,
                last_used_at=_last_used(rng, 30),
                metadata=_tag({
                    "context_bucket": ctx,
                    "at_risk_group": group_name,
                    "fusion_target": fusion,
                    "risk_reason": (
                        f"No native Fusion field for {ctx} data — "
                        f"target: {fusion}"
                    ) if risk != "low" else "Maps cleanly to a native Fusion field",
                    "record_type": record_type,
                }, _ns_modules_for(record_type)),
            ))
    return rows


def _generate_netsuite_scripts(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        # (name, script_type, subject_area, modules)
        ("script_auto_close_so",         "User Event",  "Sales Order", ("scm",)),
        ("script_gl_subsidiary_post",    "Scheduled",   "GL",          ("financials",)),
        ("script_inv_reorder_alert",     "Workflow",    "Inventory",   ("scm",)),
        ("script_ap_3way_match",         "User Event",  "AP",          ("financials",)),
        ("script_revenue_recognition",   "Scheduled",   "Revenue",     ("financials",)),
        ("script_credit_hold_check",     "User Event",  "Customer",    ("financials",)),
        ("script_drop_ship_eta",         "Map/Reduce",  "Sales Order", ("scm",)),
        ("script_warranty_claim_route",  "Workflow",    "Service",     ("scm",)),
        ("script_3pl_inbound_consume",   "RESTlet",     "Inventory",   ("scm",)),
        ("script_bom_explosion_audit",   "Scheduled",   "BOM",         ("scm",)),
        ("script_lot_traceability",      "User Event",  "Item",        ("scm",)),
        ("script_oss_pricing_override",  "User Event",  "Pricing",     ("scm",)),
    ]
    return [
        DiscoveredObjectRow(
            pillar="customisations", category="SuiteScript",
            name=name, external_id=name,
            risk_level="medium",
            last_used_at=_last_used(rng, 14),
            metadata=_tag({
                "script_type": stype, "subject_area": area,
                "context_bucket": "OPS",
                "fusion_target": "Groovy expression / BPM",
                "risk_reason": (
                    "SuiteScript logic must be re-implemented as Groovy / BPM"
                ),
            }, mods),
        )
        for name, stype, area, mods in samples
    ]


def _generate_netsuite_custom_records(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        # (scriptid, label, risk, modules)
        ("customrecord_warranty_claim",     "Warranty Claim",       "high",   ("scm",)),
        ("customrecord_dealer_program",     "Dealer Program",       "medium", ("scm", "financials")),
        ("customrecord_rebate_accrual",     "Rebate Accrual",       "high",   ("financials",)),
        ("customrecord_field_service_call", "Field Service Call",   "medium", ("scm",)),
        ("customrecord_marketing_campaign", "Marketing Campaign",   "low",    ("financials", "scm")),
        ("customrecord_sox_test",           "SOX Test",             "high",   ("financials",)),
    ]
    return [
        DiscoveredObjectRow(
            pillar="customisations", category="Custom Record Type",
            name=label, external_id=scriptid, risk_level=risk,
            metadata=_tag({
                "scriptid": scriptid, "context_bucket": "OPS",
                "fusion_target": (
                    "Custom Object" if risk == "high" else "DFF / Lookup"
                ),
                "risk_reason": (
                    "No Fusion equivalent — needs a custom object"
                    if risk == "high"
                    else "Maps to a Fusion DFF or lookup table"
                ),
            }, mods),
        )
        for scriptid, label, risk, mods in samples
    ]


def _generate_netsuite_reports(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        # Saved Searches — kept-in-prod, owned by finance/ops
        ("Open AR by Aging Bucket",        "Saved Search", "OTBI",         312, "low",    ("financials",)),
        ("Open AP by Aging Bucket",        "Saved Search", "OTBI",         284, "low",    ("financials",)),
        ("Inventory Below Reorder",        "Saved Search", "OTBI",         168, "low",    ("scm",)),
        ("Sales Pipeline by Rep",          "Saved Search", "OTBI",         92,  "medium", ("scm",)),
        ("GL Activity by Subsidiary",      "Saved Search", "OTBI",         154, "low",    ("financials",)),
        ("Customer Credit Hold List",      "Saved Search", "OTBI",         47,  "low",    ("financials",)),
        # BI Publisher templates
        ("AR Invoice Statement",           "BI Publisher", "BIP",          22,  "low",    ("financials",)),
        ("AP Check Stub",                  "BI Publisher", "BIP",          18,  "low",    ("financials",)),
        ("Sales Order Confirmation",       "BI Publisher", "BIP",          31,  "low",    ("scm",)),
        ("Purchase Order PDF",             "BI Publisher", "BIP",          14,  "low",    ("scm",)),
        ("Statement of Account",           "BI Publisher", "BIP",          22,  "medium", ("financials",)),
        # Legacy Reports (deprecated platform)
        ("Discoverer Cost Roll-up",        "Discoverer",   "Disco",        8,   "high",   ("scm", "financials")),
        ("Discoverer Margin by Region",    "Discoverer",   "Disco",        5,   "high",   ("financials",)),
        # HCM reports
        ("Headcount by Cost Center",       "Saved Search", "OTBI",         24,  "low",    ("hcm",)),
        ("Time-Off Liability",             "Saved Search", "OTBI",         9,   "medium", ("hcm",)),
        ("New-Hire Pipeline",              "BI Publisher", "BIP",          6,   "low",    ("hcm",)),
    ]
    rows: list[DiscoveredObjectRow] = []
    for name, kind, platform, instances, risk, mods in samples:
        rows.append(DiscoveredObjectRow(
            pillar="reports", category=kind,
            name=name, external_id=name.replace(" ", "_").lower(),
            risk_level=risk,
            last_used_at=_last_used(rng, 30),
            metadata=_tag({
                "platform": platform,
                "instance_count": instances,   # how many copies / dashboards
                "context_bucket": "OPS",
                "fusion_target": (
                    "OTBI subject area" if platform == "OTBI"
                    else "BI Publisher" if platform == "BIP"
                    else "Re-platform to OTBI / BI Publisher"
                ),
                "risk_reason": (
                    "Discoverer is deprecated — must re-platform before cutover"
                    if platform == "Disco" else
                    "Standard re-platform; OTBI subject area exists"
                ),
            }, mods),
        ))
    return rows


# ─── NetSuite ────────────────────────────────────────────────────────


# Names + transports modeled on a typical NetSuite OneWorld customer.
# Each ("raw_name", optional_consumer_key) tuple gets classified by the
# vendor catalog, which fills in the brand / transport / direction.
_NETSUITE_INTEGRATIONS: tuple[tuple[str, str], ...] = (
    ("Salesforce CRM Sync",        "sfdc-bridge-9871"),
    ("Workday HCM Inbound",        "workday-hcm-prod"),
    ("Celigo integrator.io",       "celigo-app-04421"),
    ("Avalara AvaTax",             "avalara-prod"),
    ("Bill.com AP Sync",           "billcom-app-22"),
    ("Boomi (reporting)",          ""),
    ("Bank Feed · Wells Fargo",    ""),
    ("Shopify Storefront",         ""),
    ("Expensify Reports",          ""),
    ("ShipStation API",            ""),
    ("ADP Payroll",                ""),
    ("Custom RESTlet · GL Feed",   ""),
    ("Warehouse WMS Bridge",       ""),
)


def scan_netsuite(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ScanResult:
    meta = connection_metadata or {}
    account_id = (meta.get("account_id") or "TSTDRV1234567").upper()
    rng = _seed("netsuite", account_id)

    objects: list[DiscoveredObjectRow] = []

    # ── Data pillar ──────────────────────────────────────────────────
    # One row per master object. Realistic mid-market counts emit on
    # ``metadata.row_count`` so the panel can show "Customer Master ·
    # 4,512 rows" while the rollup still aligns ("Data · 6 entities").
    customers = 4500 + rng.randint(0, 800)
    suppliers = 1100 + rng.randint(0, 400)
    items     = 8200 + rng.randint(0, 2000)
    employees = 320 + rng.randint(0, 180)
    open_sos  = 1850 + rng.randint(0, 500)
    open_pos  = 920 + rng.randint(0, 400)
    objects += [
        DiscoveredObjectRow(
            pillar="data", category="Customer Master",
            name=f"customer × {customers:,}",
            external_id="customer", risk_level="low",
            metadata=_tag(
                {"row_count": customers, "table": "customer"},
                ("financials", "scm"),
            ),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Vendor Master",
            name=f"vendor × {suppliers:,}", external_id="vendor",
            metadata=_tag(
                {"row_count": suppliers, "table": "vendor"},
                ("financials", "scm"),
            ),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Item Master",
            name=f"item × {items:,}", external_id="item",
            metadata=_tag(
                {"row_count": items, "table": "item"},
                ("scm",),
            ),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Employee Master",
            name=f"employee × {employees:,}", external_id="employee",
            metadata=_tag(
                {"row_count": employees, "table": "employee"},
                ("hcm",),
            ),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Open Sales Orders",
            name=f"open SO × {open_sos:,}", external_id="salesorder",
            metadata=_tag(
                {"row_count": open_sos, "table": "transaction"},
                ("scm",),
            ),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Open Purchase Orders",
            name=f"open PO × {open_pos:,}", external_id="purchaseorder",
            metadata=_tag(
                {"row_count": open_pos, "table": "transaction"},
                ("scm", "financials"),
            ),
        ),
    ]

    # ── Configuration pillar ─────────────────────────────────────────
    # One row per actual setup object. The list reads like an
    # implementer's spreadsheet: subsidiary names, accounting books,
    # operating units, COA segments, payment terms, customer categories,
    # tax engine setups, item types — exactly what a Discovery analyst
    # would see in a real cell. Names are deterministic per account so
    # the same demo replays identically.
    subsidiary_names = [
        "Vertex Manufacturing — Parent", "Vertex Manufacturing — UK Ltd",
        "Vertex Manufacturing — DE GmbH", "Vertex Manufacturing — CA Inc",
        "Vertex Manufacturing — AU Pty",  "Vertex Manufacturing — IN Pvt",
    ]
    for name in subsidiary_names:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Subsidiary",
            name=name, external_id=name.lower().replace(" ", "_"),
            metadata=_tag(
                {"type": "subsidiary"}, ("financials",),
            ),
        ))
    book_specs = [
        ("Primary Book — Local GAAP",   "USD"),
        ("Primary Book — UK GAAP",      "GBP"),
        ("Primary Book — DE GAAP",      "EUR"),
        ("Secondary Book — IFRS",       "USD"),
        ("Secondary Book — US GAAP",    "USD"),
        ("Adjustment Book — Audit",     "USD"),
        ("Adjustment Book — Tax",       "USD"),
        ("Consolidation Book — Mgmt",   "USD"),
    ]
    for name, ccy in book_specs:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Accounting Book",
            name=name, external_id=name.lower().replace(" ", "_"),
            metadata=_tag(
                {"functional_currency": ccy}, ("financials",),
            ),
        ))
    coa_segments = [
        ("Company",          2, "financials"),
        ("Cost Center",      4, "financials"),
        ("Natural Account",  6, "financials"),
        ("Sub Account",      4, "financials"),
        ("Product",          4, "scm"),
    ]
    for seg_name, length, mod in coa_segments:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="COA Segment",
            name=f"{seg_name} ({length} char)",
            external_id=f"coa_seg_{seg_name.lower().replace(' ', '_')}",
            metadata=_tag(
                {"segment": seg_name, "length": length},
                (mod,),
            ),
        ))
    bu_names = [
        "North America Operations", "EMEA Operations", "APAC Operations",
        "Service & Support",        "Direct-to-Consumer",
        "Strategic Accounts",
    ]
    for name in bu_names:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Business Unit",
            name=name, external_id=name.lower().replace(" ", "_"),
            metadata=_tag(
                {"type": "business_unit"},
                ("financials", "scm"),
            ),
        ))
    # Payment / receivable terms ──
    for code in (
        "Net 15", "Net 30", "Net 45", "Net 60", "Net 90",
        "2/10 Net 30", "1/15 Net 30", "Due on Receipt",
        "EOM Net 30", "Net 30 (Mfg)", "Net 60 (Govt)",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Payment Term",
            name=code, external_id=f"paymentterm_{code.lower().replace(' ', '_').replace('/', '_')}",
            metadata=_tag({"discount_pct": 2 if "2/10" in code else 1 if "1/15" in code else 0},
                          ("financials",)),
        ))
    # Customer / vendor categories ──
    for code in (
        "Trade — Retail", "Trade — Wholesale", "Trade — Distributor",
        "Trade — Direct OEM", "Service — Recurring", "Service — One-Time",
        "Government — Federal", "Government — State", "Internal Transfer",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Customer Category",
            name=code, external_id=code.lower().replace(" — ", "_").replace(" ", "_"),
            metadata=_tag({"context_bucket": "TRADE" if "Trade" in code else "GOVT" if "Government" in code else "INTERNAL"},
                          ("financials", "scm")),
        ))
    # Item types / classifications ──
    for code in (
        "Inventory Part", "Inventory Assembly", "Non-Inventory Part",
        "Service · Recurring", "Service · One-Time",
        "Drop-Ship Part", "Kit / Package", "Lot Numbered Inventory",
        "Serial Numbered Inventory", "Other Charge",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Item Type",
            name=code, external_id=code.lower().replace(" · ", "_").replace(" ", "_").replace("/", "_"),
            metadata=_tag({"requires_subinventory": "Lot" in code or "Serial" in code},
                          ("scm",)),
        ))
    # Tax / withholding profiles ──
    for code in (
        "Avalara — US Sales Tax", "Avalara — VAT (UK/EU)",
        "Vertex — US Sales Tax", "Manual — Statutory GST (IN)",
        "Manual — Statutory GST (AU)", "Manual — Withholding (1099-MISC)",
        "Manual — Withholding (1099-NEC)",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Tax / Withholding Profile",
            name=code, external_id=code.lower().split(" — ")[0].strip().replace(" ", "_"),
            metadata=_tag({"engine": code.split(" — ")[0]}, ("financials",)),
        ))
    # HCM responsibility / job profiles ──
    for code in (
        "Job · Plant Manager",   "Job · Production Planner",
        "Job · Buyer",           "Job · Procurement Analyst",
        "Job · Accountant — AR", "Job · Accountant — AP",
        "Job · Financial Analyst", "Job · Controller",
        "Job · HR Business Partner",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Job / Position",
            name=code, external_id=code.lower().replace(" · ", "_").replace(" — ", "_").replace(" ", "_"),
            metadata=_tag({"hcm_template": True}, ("hcm",)),
        ))

    # ── Processes pillar ─────────────────────────────────────────────
    # Per-workflow, per-alert rows. The catalogue covers the typical
    # implementations across financials / scm / hcm so the rollup count
    # responds to the customer's selected_modules.
    workflows: tuple[tuple[str, str, str], ...] = (
        # (name, primary module, area)
        ("Sales Order Approval Workflow",        "scm",        "SO"),
        ("Customer Credit Hold Workflow",        "financials", "AR"),
        ("PO Approval (3-tier)",                 "scm",        "PO"),
        ("Vendor Bill Approval",                 "financials", "AP"),
        ("Journal Entry Approval",               "financials", "GL"),
        ("Expense Report Approval",              "financials", "EXP"),
        ("Item Activation Workflow",             "scm",        "Item"),
        ("Return Material Authorization",        "scm",        "SO"),
        ("Inventory Adjustment Approval",        "scm",        "INV"),
        ("Drop-Ship PO Auto-Creation",           "scm",        "PO"),
        ("Revenue Recognition Trigger",          "financials", "REV"),
        ("AR Invoice Print/Email",               "financials", "AR"),
        ("Credit Memo Approval",                 "financials", "AR"),
        ("New Hire Onboarding",                  "hcm",        "HR"),
        ("Termination Offboarding",              "hcm",        "HR"),
        ("Time-Off Request Approval",            "hcm",        "HR"),
        ("Performance Review Cycle",             "hcm",        "HR"),
        ("Vendor Onboarding & W-9",              "financials", "AP"),
        ("Customer Contract Renewal",            "financials", "AR"),
        ("Lockbox Receipt Auto-Match",           "financials", "AR"),
        ("Multi-Subsidiary Inter-Co JE",         "financials", "GL"),
        ("Asset Capitalisation Trigger",         "financials", "FA"),
    )
    for name, mod, area in workflows:
        objects.append(DiscoveredObjectRow(
            pillar="processes", category="SuiteFlow Workflow",
            name=name, external_id=name.lower().replace(" ", "_"),
            metadata=_tag({"area": area, "active": True},
                          (mod,)),
        ))
    alerts: tuple[tuple[str, str], ...] = (
        ("Inventory Below Reorder Point",       "scm"),
        ("AR Customer Past Due > 30d",          "financials"),
        ("AR Customer Past Due > 60d",          "financials"),
        ("AP Vendor Bill Discount Expiring",    "financials"),
        ("Open PO Past Promise Date",           "scm"),
        ("GL Unposted Journal > 5 days",        "financials"),
        ("Bank Reconciliation Variance",        "financials"),
        ("Lot Expiration < 30 days",            "scm"),
        ("Employee Birthday / Anniversary",     "hcm"),
        ("Performance Review Due",              "hcm"),
        ("Subsidiary Intercompany Imbalance",   "financials"),
    )
    for name, mod in alerts:
        objects.append(DiscoveredObjectRow(
            pillar="processes", category="Saved-Search Alert",
            name=name, external_id=name.lower().replace(" ", "_").replace(">", "gt"),
            metadata=_tag({"alert_engine": "saved_search"}, (mod,)),
        ))

    # ── Customisations pillar ────────────────────────────────────────
    # Slice 5: emit individual custom-field rows clustered into at-risk
    # groups (Bolt-style "Customer Trade Profile · 78 fields"), each row
    # carrying its own risk_level + context_bucket + risk_reason so the
    # drilldown table shows per-field detail, not a single summary line.
    netsuite_custom_fields = _generate_netsuite_custom_fields(rng)
    objects += netsuite_custom_fields
    custom_fields = len(netsuite_custom_fields)

    netsuite_scripts = _generate_netsuite_scripts(rng)
    objects += netsuite_scripts
    suite_scripts = len(netsuite_scripts)

    netsuite_records = _generate_netsuite_custom_records(rng)
    objects += netsuite_records
    cust_records = len(netsuite_records)

    customisations_total = custom_fields + suite_scripts + cust_records

    # ── Reports pillar ───────────────────────────────────────────────
    # Slice 5: per-report rows with owner + last_run + Fusion-target hint.
    netsuite_reports = _generate_netsuite_reports(rng)
    objects += netsuite_reports
    reports_total = sum(
        int(r.metadata.get("instance_count", 1)) for r in netsuite_reports
    )

    # ── Integrations pillar (vendor-catalog classified) ──────────────
    integration_rows: list[DiscoveredObjectRow] = []
    health_counts = {"healthy": 0, "degraded": 0, "not_tested": 0}
    # Brand-to-module mapping. Most integrations span at least one
    # module; CRM/storefront sit on scm, payroll/HRIS on hcm, financial
    # sync apps on financials.
    _NS_INT_MODULES: dict[str, tuple[str, ...]] = {
        "Salesforce CRM Sync":      ("scm", "financials"),
        "Workday HCM Inbound":      ("hcm",),
        "Celigo integrator.io":     ("scm", "financials"),
        "Avalara AvaTax":           ("financials",),
        "Bill.com AP Sync":         ("financials",),
        "Boomi (reporting)":        ("financials", "scm"),
        "Bank Feed · Wells Fargo":  ("financials",),
        "Shopify Storefront":       ("scm",),
        "Expensify Reports":        ("financials",),
        "ShipStation API":          ("scm",),
        "ADP Payroll":              ("hcm",),
        "Custom RESTlet · GL Feed": ("financials",),
        "Warehouse WMS Bridge":     ("scm",),
    }
    for raw_name, ck in _NETSUITE_INTEGRATIONS:
        cls = classify_integration(raw_name, consumer_key=ck)
        # Health distribution: ~62% healthy, ~23% degraded, ~15% not_tested.
        roll = rng.random()
        if roll < 0.62:
            status, message = "healthy", "last 24h: 100% success"
        elif roll < 0.85:
            status, message = "degraded", "success rate 84% over 24h"
        else:
            status, message = "not_tested", "no probe within SLA window"
        health_counts[status] += 1
        integration_rows.append(DiscoveredObjectRow(
            pillar="integrations",
            category=cls.category,
            name=cls.brand,
            external_id=raw_name,
            risk_level="medium" if status == "degraded" else "low",
            last_used_at=_last_used(rng, days_ago_max=5),
            metadata=_tag({
                "raw_name": raw_name,
                "transport": cls.transport,
                "direction": cls.direction,
                "status": status,
                "message": message,
                "matched_rule": cls.matched_rule,
            }, _NS_INT_MODULES.get(raw_name, ("financials", "scm"))),
        ))
    objects += integration_rows

    # pillar_counts MUST equal len(rows_per_pillar). That's the contract
    # the rollup tile and the drill-down list share — anything else is a
    # lie the user catches the first time they click into Configuration.
    pillar_counts = _pillar_counts_from(objects)

    # Complexity score — weighted blend that emphasizes customisations
    # and integrations (the things that drive scope risk).
    complexity = min(100.0, round(
        (pillar_counts.get("customisations", 0) * 0.45)
        + (pillar_counts.get("integrations", 0) * 1.2)
        + (pillar_counts.get("processes", 0) * 0.4)
        + (pillar_counts.get("configuration", 0) * 0.3)
        + 28,
        1,
    ))

    return ScanResult(
        pillar_counts=pillar_counts,
        objects=objects,
        integration_health=health_counts,
        complexity_score=complexity,
        scan_notes=(
            f"Mock NetSuite inventory · account={account_id} · "
            f"{pillar_counts.get('integrations', 0)} integrations classified by vendor catalog"
        ),
    )


# ─── Oracle EBS — Slice 5 per-object generators ──────────────────────


_EBS_DFF_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Vendor Tax Profile", "GOVT", (
        "XX_VENDOR_TAX_PROFILE_ATTR1",   # 1099 classification
        "XX_VENDOR_TAX_PROFILE_ATTR2",   # state-of-incorporation
        "XX_VENDOR_TAX_PROFILE_ATTR3",   # withholding-applicable
        "XX_VENDOR_TAX_PROFILE_ATTR4",   # tax-treaty-flag
        "XX_VENDOR_TAX_PROFILE_ATTR5",   # foreign-tax-id
    )),
    ("Customer Trade Compliance", "TRADE", (
        "XX_CUST_TRADE_DPL_CHECK",
        "XX_CUST_TRADE_ECCN_CODE",
        "XX_CUST_TRADE_END_USE",
        "XX_CUST_TRADE_LICENSE_TYPE",
        "XX_CUST_TRADE_RESTRICTED_CTRY",
    )),
    ("Invoice Internal Audit Refs", "INTERNAL", (
        "XX_INV_AUDIT_REF_1",
        "XX_INV_AUDIT_REF_2",
        "XX_INV_LEGACY_PO_LINK",
        "XX_INV_LEGACY_CONTRACT_ID",
    )),
    ("Item Hazmat / EH&S", "TRADE", (
        "XX_ITEM_HAZ_CLASS_CODE",
        "XX_ITEM_HAZ_UN_NUMBER",
        "XX_ITEM_HAZ_PACKING_GROUP",
        "XX_ITEM_HAZ_SDS_URL",
    )),
)


def _ebs_risk_for(context_bucket: str) -> tuple[str, str]:
    if context_bucket == "TRADE":
        return ("high", "Custom Object")
    if context_bucket == "GOVT":
        return ("high", "DFF (Supplier)")
    if context_bucket == "INTERNAL":
        return ("medium", "DFF (Invoice Header)")
    return ("low", "Native Fusion field")


def _generate_ebs_dff_segments(rng: random.Random) -> list[DiscoveredObjectRow]:
    rows: list[DiscoveredObjectRow] = []
    for group, ctx, names in _EBS_DFF_GROUPS:
        risk, fusion = _ebs_risk_for(ctx)
        for n in names:
            app = (
                "AP" if "VENDOR" in n else
                "AR" if "CUST" in n or "INV" in n else
                "INV"
            )
            mods: tuple[str, ...] = (
                ("scm",) if app == "INV" else ("financials",)
            )
            rows.append(DiscoveredObjectRow(
                pillar="customisations",
                category="Descriptive Flexfield (DFF)",
                name=n, external_id=n,
                risk_level=risk,
                last_used_at=_last_used(rng, 60),
                metadata=_tag({
                    "context_bucket": ctx,
                    "at_risk_group": group,
                    "fusion_target": fusion,
                    "risk_reason": (
                        f"DFF segment carrying {ctx} data — target: {fusion}"
                    ),
                    "flexfield_application": app,
                }, mods),
            ))
    return rows


def _ebs_modules_for_app(app: str) -> tuple[str, ...]:
    """EBS application short-name → Fusion module mapping."""
    if app in ("GL", "AR", "AP", "FA"):
        return ("financials",)
    if app in ("INV", "PO", "OM", "WIP"):
        return ("scm",)
    if app == "HR":
        return ("hcm",)
    return ("financials",)


def _generate_ebs_concurrent_programs(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        ("XX_AR_INTEREST_INVOICE",       "AR",  "high",   "Re-implement as ESS job"),
        ("XX_AP_PAYMENT_AUTOMATION",     "AP",  "medium", "Replace with Payment Batch flow"),
        ("XX_INV_CYCLE_COUNT_ALERTS",    "INV", "medium", "Configure as standard alert"),
        ("XX_GL_TOPSIDE_ADJ_REPORT",     "GL",  "low",    "Native Fusion FSG"),
        ("XX_OM_BACKORDER_ROUTING",      "OM",  "high",   "Sales Order workflow redesign"),
        ("XX_PO_VENDOR_PERF_SCORE",      "PO",  "medium", "Supplier 360 KPI"),
        ("XX_HR_HEADCOUNT_RECONCILE",    "HR",  "high",   "Fusion HCM analytics"),
        ("XX_WIP_VARIANCE_REVAL",        "WIP", "high",   "Cost-management ESS job"),
        ("XX_AR_RECEIPT_LOCKBOX_PARSE",  "AR",  "medium", "Native lockbox + AutoMatch"),
        ("XX_AP_INV_HOLD_RELEASE",       "AP",  "medium", "Invoice approval rule"),
    ]
    return [
        DiscoveredObjectRow(
            pillar="customisations",
            category="Concurrent Program",
            name=name, external_id=name, risk_level=risk,
            last_used_at=_last_used(rng, 30),
            metadata=_tag({
                "application_short_name": app,
                "fusion_target": fusion,
                "risk_reason": fusion,
                "context_bucket": "OPS",
                "executions_last_30d": rng.randint(0, 84),
            }, _ebs_modules_for_app(app)),
        )
        for name, app, risk, fusion in samples
    ]


def _generate_ebs_forms(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        ("XX_QUICK_AP_VOUCHER", "AP",  "high"),
        ("XX_INV_LOT_INQUIRY",  "INV", "medium"),
        ("XX_GL_JE_REVIEWER",   "GL",  "medium"),
        ("XX_AR_HOLD_OVERRIDE", "AR",  "high"),
        ("XX_PO_OPEN_LIST",     "PO",  "low"),
        ("XX_OM_ORDER_TRIAGE",  "OM",  "high"),
        ("XX_HR_SUCCESSION",    "HR",  "high"),
    ]
    return [
        DiscoveredObjectRow(
            pillar="customisations", category="Custom Form",
            name=name, external_id=name, risk_level=risk,
            metadata=_tag({
                "application_short_name": app,
                "fusion_target": "Page Composer / Visual Builder",
                "context_bucket": "OPS",
                "risk_reason": (
                    "Custom OAF form — rebuild as Page Composer/VB"
                ),
            }, _ebs_modules_for_app(app)),
        )
        for name, app, risk in samples
    ]


def _generate_ebs_reports(rng: random.Random) -> list[DiscoveredObjectRow]:
    samples = [
        ("AR Aging by Customer (BIP)",     "BI Publisher", "BIP",   28, "low",    ("financials",)),
        ("AP Invoice Register (BIP)",      "BI Publisher", "BIP",   34, "low",    ("financials",)),
        ("GL Trial Balance (FSG)",         "BI Publisher", "BIP",   12, "low",    ("financials",)),
        ("INV On-hand by Subinventory",    "BI Publisher", "BIP",   19, "low",    ("scm",)),
        ("Discoverer Margin by Region",    "Discoverer",   "Disco",  6, "high",   ("financials",)),
        ("Discoverer Cost Roll-up",        "Discoverer",   "Disco",  4, "high",   ("scm", "financials")),
        ("Discoverer Order Backlog",       "Discoverer",   "Disco",  3, "high",   ("scm",)),
        ("XX Vendor 1099 Run",             "BI Publisher", "BIP",    1, "medium", ("financials",)),
        ("XX Statutory Tax Return",        "BI Publisher", "BIP",    2, "medium", ("financials",)),
        ("Headcount Reconciliation",       "BI Publisher", "BIP",    3, "medium", ("hcm",)),
        ("Time-Off Liability",             "Saved Search", "OTBI",  10, "low",    ("hcm",)),
    ]
    rows: list[DiscoveredObjectRow] = []
    for name, kind, platform, instances, risk, mods in samples:
        rows.append(DiscoveredObjectRow(
            pillar="reports", category=kind,
            name=name, external_id=name.replace(" ", "_").lower(),
            risk_level=risk,
            last_used_at=_last_used(rng, 60),
            metadata=_tag({
                "platform": platform,
                "instance_count": instances,
                "fusion_target": (
                    "Native OTBI / BI Publisher" if platform == "BIP"
                    else "Re-platform to OTBI / BI Publisher"
                ),
                "risk_reason": (
                    "Discoverer is deprecated — must re-platform before cutover"
                    if platform == "Disco" else
                    "Standard re-platform; OTBI subject area exists"
                ),
                "context_bucket": "OPS",
            }, mods),
        ))
    return rows


_EBS_INTEGRATIONS: tuple[tuple[str, str], ...] = (
    ("XX_SFDC_OUTBOUND_INTERFACE",     ""),
    ("XX_WORKDAY_HR_INBOUND",          ""),
    ("XX_AVATAX_INBOUND",              ""),
    ("XX_WELLS_FARGO_MT940",           ""),
    ("XX_BILLCOM_AP_INBOUND",          ""),
    ("XX_BOOMI_REPORTING",             ""),
    ("XX_ADP_PAYROLL_FEED",            ""),
    ("XX_CUSTOM_RESTLET_GL",           ""),
    ("XX_WAREHOUSE_WMS_BRIDGE",        ""),
    ("XX_SHIPSTATION_OUTBOUND",        ""),
)


def scan_oracle_ebs(
    *,
    connection_metadata: dict[str, Any] | None,
    credentials: dict[str, Any] | None,
) -> ScanResult:
    meta = connection_metadata or {}
    host = meta.get("host", "ebs-prod-db.internal")
    rng = _seed("oracle_ebs", host)

    objects: list[DiscoveredObjectRow] = []

    # Data — one row per master/transactional table, tagged with module.
    parties = 8800 + rng.randint(0, 1500)
    items   = 14_500 + rng.randint(0, 3000)
    employees = 480 + rng.randint(0, 200)
    je_lines  = 412_000 + rng.randint(0, 80_000)
    open_sos  = 5200 + rng.randint(0, 1100)
    open_pos  = 2400 + rng.randint(0, 700)
    objects += [
        DiscoveredObjectRow(
            pillar="data", category="HZ Parties",
            name=f"hz_parties × {parties:,}", external_id="hz_parties",
            metadata=_tag({"row_count": parties, "table": "HZ_PARTIES"},
                          ("financials", "scm")),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Items (MTL_SYSTEM_ITEMS_B)",
            name=f"mtl_system_items_b × {items:,}",
            external_id="mtl_system_items_b",
            metadata=_tag({"row_count": items, "table": "MTL_SYSTEM_ITEMS_B"},
                          ("scm",)),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Employees (PER_ALL_PEOPLE_F)",
            name=f"per_all_people_f × {employees:,}", external_id="per_all_people_f",
            metadata=_tag({"row_count": employees, "table": "PER_ALL_PEOPLE_F"},
                          ("hcm",)),
        ),
        DiscoveredObjectRow(
            pillar="data", category="GL Journal Lines",
            name=f"gl_je_lines × {je_lines:,}", external_id="gl_je_lines",
            metadata=_tag({"row_count": je_lines, "table": "GL_JE_LINES"},
                          ("financials",)),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Open Sales Orders (OE_ORDER_HEADERS_ALL)",
            name=f"oe_order_headers × {open_sos:,}", external_id="oe_order_headers_all",
            metadata=_tag({"row_count": open_sos, "table": "OE_ORDER_HEADERS_ALL"},
                          ("scm",)),
        ),
        DiscoveredObjectRow(
            pillar="data", category="Open Purchase Orders (PO_HEADERS_ALL)",
            name=f"po_headers × {open_pos:,}", external_id="po_headers_all",
            metadata=_tag({"row_count": open_pos, "table": "PO_HEADERS_ALL"},
                          ("scm", "financials")),
        ),
    ]

    # Configuration — per-entity rows.
    legal_entities = [
        "Vertex Mfg Inc (US)", "Vertex Mfg Ltd (UK)",
        "Vertex Mfg GmbH (DE)", "Vertex Mfg Pty (AU)",
    ]
    for le in legal_entities:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Legal Entity",
            name=le, external_id=f"xle_{le.lower().replace(' ', '_')}",
            metadata=_tag({"type": "legal_entity"}, ("financials",)),
        ))
    ledger_specs = [
        ("US Ledger — Primary",   "USD", "USCOA"),
        ("UK Ledger — Primary",   "GBP", "INTLCOA"),
        ("DE Ledger — Primary",   "EUR", "INTLCOA"),
        ("Australia Ledger",      "AUD", "INTLCOA"),
        ("Reporting Ledger — IFRS","USD", "USCOA"),
        ("Reporting Ledger — Mgmt","USD", "USCOA"),
    ]
    for name, ccy, coa in ledger_specs:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Ledger",
            name=name, external_id=f"ledger_{name.lower().split(' — ')[0].replace(' ', '_')}",
            metadata=_tag({"functional_currency": ccy, "coa": coa},
                          ("financials",)),
        ))
    ou_names = [
        "US Operations OU",  "EMEA Operations OU",
        "APAC Operations OU","US Service OU",
        "US Distribution OU","UK Distribution OU",
        "DE Distribution OU","AU Distribution OU",
        "US Holding Co OU",  "Intercompany OU", "Sandbox OU",
    ]
    for name in ou_names:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Operating Unit / BU",
            name=name, external_id=name.lower().replace(" ", "_"),
            metadata=_tag({"type": "operating_unit"},
                          ("financials", "scm")),
        ))
    coa_segs = [
        ("Company",          2, ("financials",)),
        ("Cost Center",      4, ("financials",)),
        ("Natural Account",  6, ("financials",)),
        ("Sub Account",      4, ("financials",)),
        ("Product / Line",   4, ("scm",)),
    ]
    for seg_name, length, mods in coa_segs:
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="COA Segment",
            name=f"{seg_name} ({length} char)",
            external_id=f"coa_seg_{seg_name.lower().replace(' ', '_').replace('/', '_')}",
            metadata=_tag({"segment": seg_name, "length": length}, mods),
        ))
    for code in (
        "Net 15", "Net 30", "Net 45", "Net 60", "Net 90",
        "2/10 Net 30", "Due on Receipt", "EOM Net 30", "Net 60 (Govt)",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Payment Term",
            name=code, external_id=f"paymentterm_{code.lower().replace(' ', '_').replace('/', '_')}",
            metadata=_tag({"discount_pct": 2 if "2/10" in code else 0},
                          ("financials",)),
        ))
    for code in (
        "Customer Class · Trade", "Customer Class · Wholesale",
        "Customer Class · OEM",   "Customer Class · Federal",
        "Customer Class · State", "Customer Class · Internal",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Customer Class",
            name=code, external_id=code.lower().replace(" · ", "_").replace(" ", "_"),
            metadata=_tag({"class_segment": code}, ("financials", "scm")),
        ))
    for code in (
        "Subinventory · NY-Main",  "Subinventory · NY-RMA",
        "Subinventory · UK-Main",  "Subinventory · DE-Main",
        "Subinventory · AU-Main",  "Subinventory · 3PL-East",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Subinventory",
            name=code, external_id=code.lower().replace(" · ", "_").replace(" ", "_").replace("-", "_"),
            metadata=_tag({"locator_controlled": True}, ("scm",)),
        ))
    for code in (
        "Job · Accountant — AR", "Job · Accountant — AP",
        "Job · Controller",      "Job · Financial Analyst",
        "Job · Plant Manager",   "Job · Procurement Buyer",
        "Job · Production Planner", "Job · HR Business Partner",
    ):
        objects.append(DiscoveredObjectRow(
            pillar="configuration", category="Job / Position",
            name=code, external_id=code.lower().replace(" · ", "_").replace(" — ", "_").replace(" ", "_"),
            metadata=_tag({"hcm_template": True}, ("hcm",)),
        ))

    # Processes — per-workflow / per-alert rows.
    workflow_specs: tuple[tuple[str, str, str], ...] = (
        ("XX_AR_INVOICE_APPROVAL_WF",      "financials", "AR"),
        ("XX_AP_INVOICE_APPROVAL_WF",      "financials", "AP"),
        ("XX_GL_JE_APPROVAL_WF",           "financials", "GL"),
        ("XX_OM_HOLD_RELEASE_WF",          "scm",        "OM"),
        ("XX_OM_BACKORDER_WF",             "scm",        "OM"),
        ("XX_PO_APPROVAL_3TIER_WF",        "scm",        "PO"),
        ("XX_PO_AUTO_CREATE_DROPSHIP_WF",  "scm",        "PO"),
        ("XX_INV_ADJ_APPROVAL_WF",         "scm",        "INV"),
        ("XX_FA_CAPITALIZATION_WF",        "financials", "FA"),
        ("XX_HR_NEW_HIRE_WF",              "hcm",        "HR"),
        ("XX_HR_TERMINATION_WF",           "hcm",        "HR"),
        ("XX_HR_TIMEOFF_WF",               "hcm",        "HR"),
        ("XX_AR_LOCKBOX_AUTOMATCH_WF",     "financials", "AR"),
        ("XX_AR_CREDIT_HOLD_WF",           "financials", "AR"),
    )
    for name, mod, area in workflow_specs:
        objects.append(DiscoveredObjectRow(
            pillar="processes", category="Custom Workflow",
            name=name, external_id=name.lower(),
            metadata=_tag({"area": area, "type": "WF_ITEM_TYPE"}, (mod,)),
        ))
    alert_specs: tuple[tuple[str, str], ...] = (
        ("Open Invoice > 60 days (AR)",     "financials"),
        ("Unposted JE Aging > 5 days",      "financials"),
        ("Inventory Below Reorder Point",   "scm"),
        ("Lot Expiration < 30 days",        "scm"),
        ("Open PO Past Promise Date",       "scm"),
        ("Vendor Bill Discount Expiring",   "financials"),
        ("Bank Reconciliation Variance",    "financials"),
        ("Cycle-Count Variance > Threshold","scm"),
        ("Employee Birthday / Anniversary", "hcm"),
        ("Performance Review Due",          "hcm"),
        ("Intercompany Imbalance",          "financials"),
    )
    for name, mod in alert_specs:
        objects.append(DiscoveredObjectRow(
            pillar="processes", category="Alert",
            name=name, external_id=name.lower().replace(" ", "_").replace(">", "gt").replace("<", "lt"),
            metadata=_tag({"alert_engine": "ALR_ALERTS"}, (mod,)),
        ))

    # Customisations — per-DFF / per-concurrent-program rows.
    objects += _generate_ebs_dff_segments(rng)
    objects += _generate_ebs_concurrent_programs(rng)
    objects += _generate_ebs_forms(rng)

    # Reports — per-template rows with Fusion target + platform.
    objects += _generate_ebs_reports(rng)

    # Integrations
    _EBS_INT_MODULES: dict[str, tuple[str, ...]] = {
        "XX_SFDC_OUTBOUND_INTERFACE":  ("scm", "financials"),
        "XX_WORKDAY_HR_INBOUND":       ("hcm",),
        "XX_AVATAX_INBOUND":           ("financials",),
        "XX_WELLS_FARGO_MT940":        ("financials",),
        "XX_BILLCOM_AP_INBOUND":       ("financials",),
        "XX_BOOMI_REPORTING":          ("financials", "scm"),
        "XX_ADP_PAYROLL_FEED":         ("hcm",),
        "XX_CUSTOM_RESTLET_GL":        ("financials",),
        "XX_WAREHOUSE_WMS_BRIDGE":     ("scm",),
        "XX_SHIPSTATION_OUTBOUND":     ("scm",),
    }
    integration_rows: list[DiscoveredObjectRow] = []
    health_counts = {"healthy": 0, "degraded": 0, "not_tested": 0}
    for raw_name, ck in _EBS_INTEGRATIONS:
        cls = classify_integration(raw_name, consumer_key=ck)
        roll = rng.random()
        if roll < 0.55:
            status, message = "healthy", "concurrent program last ran without errors"
        elif roll < 0.82:
            status, message = "degraded", "alert hit · 12% failure rate last 24h"
        else:
            status, message = "not_tested", "no run within 30 days"
        health_counts[status] += 1
        integration_rows.append(DiscoveredObjectRow(
            pillar="integrations",
            category=cls.category,
            name=cls.brand,
            external_id=raw_name,
            risk_level="medium" if status == "degraded" else "low",
            last_used_at=_last_used(rng, days_ago_max=30),
            metadata=_tag({
                "raw_name": raw_name,
                "transport": cls.transport,
                "direction": cls.direction,
                "status": status,
                "message": message,
                "matched_rule": cls.matched_rule,
            }, _EBS_INT_MODULES.get(raw_name, ("financials", "scm"))),
        ))
    objects += integration_rows

    pillar_counts = _pillar_counts_from(objects)

    complexity = min(100.0, round(
        (pillar_counts.get("customisations", 0) * 0.40)
        + (pillar_counts.get("integrations", 0) * 1.4)
        + (pillar_counts.get("processes", 0) * 0.4)
        + (pillar_counts.get("configuration", 0) * 0.3)
        + 32,
        1,
    ))

    return ScanResult(
        pillar_counts=pillar_counts,
        objects=objects,
        integration_health=health_counts,
        complexity_score=complexity,
        scan_notes=(
            f"Mock EBS inventory · host={host} · "
            f"{pillar_counts.get('integrations', 0)} integrations classified by vendor catalog"
        ),
    )

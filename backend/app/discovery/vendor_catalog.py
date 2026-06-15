"""Vendor catalog — known SaaS integration signatures.

Scanners discover raw integration registrations (a SuiteTalk consumer key,
an EBS concurrent program calling a web service, an SFTP inbox pattern,
an outbound message). The catalog maps those raw signatures to *brand
names* — "Celigo integrator.io", "Avalara AvaTax", "Workday HCM" — plus
the transport type and a suggested health-probe endpoint.

This is what makes a raw "third-party SOAP user #4827" render in the
Integration Health table as "Workday HCM · REST · Inbound · Healthy".

Match rules are evaluated top-to-bottom; the first match wins. Each rule
inspects the integration's ``name``, ``consumer_key``, ``script_id``,
``sftp_inbox``, or any other raw field the scanner emits — see
``classify_integration`` for the full contract.

Add new vendors by appending to ``CATALOG``. Keep the most specific rules
first so a generic "REST" doesn't shadow a Workday-specific one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VendorRule:
    brand: str                       # display name in the Integration Health table
    transport: str                   # REST | SOAP | SFTP/AS2 | DB Link | MT940 | cXML | B-PIPE | Flat file | JMS
    direction: str = "Inbound"       # Inbound | Outbound | Bi-directional
    category: str = "Third-party SaaS"
    # Match predicates. Each is OR'd against the raw integration's name /
    # ids / inbox patterns. At least one must match for the rule to fire.
    name_regex: list[str] = field(default_factory=list)
    consumer_key_regex: list[str] = field(default_factory=list)
    inbox_regex: list[str] = field(default_factory=list)
    # Optional suggested health endpoint (relative). The health prober (a
    # later slice) calls this; for v1 we record it on the discovered row
    # so the UI can render "next probe target".
    health_endpoint: str | None = None


@dataclass
class IntegrationClassification:
    brand: str
    transport: str
    direction: str
    category: str
    health_endpoint: str | None
    matched_rule: str   # which rule fired — debugging aid


# Order matters — most-specific first. ~15 well-known partners is enough
# to demo well; full v1 production catalog is ~500 entries (loaded from a
# YAML at startup once the curation effort is funded — out of scope here).
CATALOG: tuple[VendorRule, ...] = (
    VendorRule(
        brand="Celigo integrator.io",
        transport="JMS",
        direction="Bi-directional",
        category="iPaaS",
        name_regex=[r"celigo", r"integrator\.io"],
        consumer_key_regex=[r"celigo"],
    ),
    VendorRule(
        brand="Boomi",
        transport="REST",
        direction="Bi-directional",
        category="iPaaS",
        name_regex=[r"\bboomi\b"],
    ),
    VendorRule(
        brand="Workday HCM",
        transport="REST",
        direction="Inbound",
        category="HCM",
        name_regex=[r"workday", r"wd[-_]?hcm"],
        consumer_key_regex=[r"workday"],
    ),
    VendorRule(
        brand="Salesforce CRM",
        transport="REST",
        direction="Bi-directional",
        category="CRM",
        name_regex=[r"salesforce", r"\bsfdc\b"],
        consumer_key_regex=[r"salesforce", r"sfdc"],
    ),
    VendorRule(
        brand="Avalara AvaTax",
        transport="SFTP/AS2",
        direction="Inbound",
        category="Tax",
        name_regex=[r"avalara", r"avatax"],
        inbox_regex=[r"avatax", r"avalara"],
    ),
    VendorRule(
        brand="Bill.com AP Sync",
        transport="SFTP/AS2",
        direction="Inbound",
        category="AP",
        name_regex=[r"bill\.?com", r"bill\.com"],
        inbox_regex=[r"billcom", r"bill\.com"],
    ),
    VendorRule(
        brand="Bank Feed · Wells Fargo",
        transport="MT940",
        direction="Inbound",
        category="Banking",
        # ``[\s_-]*`` tolerates the underscores common in EBS naming
        # conventions (XX_WELLS_FARGO_MT940) as well as the spaces used
        # in NetSuite display names ("Wells Fargo MT940").
        name_regex=[r"wells[\s_-]*fargo", r"wf[\s_-]?bank"],
        inbox_regex=[r"wf-mt940", r"wellsfargo"],
    ),
    VendorRule(
        brand="Shopify Storefront",
        transport="cXML",
        direction="Bi-directional",
        category="Commerce",
        name_regex=[r"shopify"],
    ),
    VendorRule(
        brand="Expensify",
        transport="REST",
        direction="Inbound",
        category="T&E",
        name_regex=[r"expensify"],
        inbox_regex=[r"expensify"],
    ),
    VendorRule(
        brand="ShipStation API",
        transport="REST",
        direction="Outbound",
        category="Shipping",
        name_regex=[r"shipstation"],
    ),
    VendorRule(
        brand="ADP Payroll",
        transport="B-PIPE",
        direction="Inbound",
        category="Payroll",
        name_regex=[r"\badp\b", r"adp.?payroll"],
        inbox_regex=[r"adp"],
    ),
    VendorRule(
        brand="Warehouse WMS Bridge",
        transport="SOAP",
        direction="Bi-directional",
        category="WMS",
        name_regex=[r"\bwms\b", r"warehouse"],
    ),
    VendorRule(
        brand="Boomi (reporting)",
        transport="DB Link",
        direction="Outbound",
        category="iPaaS",
        name_regex=[r"boomi.?(report|reporting)"],
    ),
    # Generic fallback bucket — anything that says "RESTlet" / "SOAP user"
    # without matching a known brand. Keeps the row in the inventory
    # without inventing a brand.
    VendorRule(
        brand="Custom RESTlet",
        transport="Flat file",
        direction="Inbound",
        category="Custom",
        name_regex=[r"restlet", r"custom.?api", r"custom.?integration"],
    ),
)


def classify_integration(
    name: str,
    *,
    consumer_key: str | None = None,
    inbox: str | None = None,
    fallback_transport: str = "REST",
    fallback_direction: str = "Inbound",
) -> IntegrationClassification:
    """Match a raw integration against the catalog. Returns a
    classification including a brand name; falls back to ``("(unrecognised)",
    fallback_transport, fallback_direction)`` when nothing matches."""
    name_l = (name or "").lower()
    ck_l = (consumer_key or "").lower()
    inbox_l = (inbox or "").lower()

    for rule in CATALOG:
        for rgx in rule.name_regex:
            if re.search(rgx, name_l):
                return IntegrationClassification(
                    brand=rule.brand,
                    transport=rule.transport,
                    direction=rule.direction,
                    category=rule.category,
                    health_endpoint=rule.health_endpoint,
                    matched_rule=f"name:{rgx}",
                )
        for rgx in rule.consumer_key_regex:
            if ck_l and re.search(rgx, ck_l):
                return IntegrationClassification(
                    brand=rule.brand,
                    transport=rule.transport,
                    direction=rule.direction,
                    category=rule.category,
                    health_endpoint=rule.health_endpoint,
                    matched_rule=f"consumer_key:{rgx}",
                )
        for rgx in rule.inbox_regex:
            if inbox_l and re.search(rgx, inbox_l):
                return IntegrationClassification(
                    brand=rule.brand,
                    transport=rule.transport,
                    direction=rule.direction,
                    category=rule.category,
                    health_endpoint=rule.health_endpoint,
                    matched_rule=f"inbox:{rgx}",
                )

    return IntegrationClassification(
        brand=name or "(unrecognised)",
        transport=fallback_transport,
        direction=fallback_direction,
        category="Unclassified",
        health_endpoint=None,
        matched_rule="fallback",
    )

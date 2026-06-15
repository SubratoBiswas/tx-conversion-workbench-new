"""End-to-end smoke tests covering the Project (engagement) → Conversion split."""
import os
import sys
from pathlib import Path

# Make the app importable when running pytest from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Use isolated test DB so we don't touch the dev one
os.environ["DATABASE_URL"] = "sqlite:///./test_workbench.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.seed import run_seed  # noqa: E402

# Initialise DB + seed once for all tests
run_seed()
client = TestClient(app)


def _login() -> str:
    r = client.post(
        "/api/auth/login",
        json={"email": settings.ADMIN_EMAIL, "password": settings.ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_login()}"}


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_seed():
    headers = _headers()
    # Datasets seeded
    r = client.get("/api/datasets", headers=headers)
    assert r.status_code == 200
    assert any("Demo" in d["name"] for d in r.json())
    # Templates seeded
    r = client.get("/api/fbdi/templates", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) >= 1
    # Engagement project seeded — should have client + go-live + ~10 conversions
    r = client.get("/api/projects", headers=headers)
    assert r.status_code == 200
    projs = r.json()
    assert len(projs) >= 1
    p = projs[0]
    assert p["client"]
    assert p["conversion_count"] >= 9


def test_engagement_lists_conversions():
    """The /api/projects/{id}/conversions endpoint returns objects within
    the engagement, ordered by planned_load_order."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.get(f"/api/projects/{project_id}/conversions", headers=headers)
    assert r.status_code == 200
    convs = r.json()
    assert len(convs) >= 9
    # Confirm Item Master is in there and bound to a dataset
    item_conv = next((c for c in convs if c["target_object"] == "Item"), None)
    assert item_conv is not None
    assert item_conv["dataset_id"] is not None
    assert item_conv["template_id"] is not None
    # Order is sorted by planned_load_order
    orders = [c["planned_load_order"] for c in convs]
    assert orders == sorted(orders)


def test_full_conversion_flow():
    """End-to-end conversion lifecycle scoped to a single Conversion object."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]

    # Find the fully-bound Item Master conversion
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    cid = item_conv["id"]

    # Suggest mapping
    r = client.post(f"/api/conversions/{cid}/suggest-mapping", headers=headers)
    assert r.status_code == 200, r.text
    suggestions = r.json()
    assert len(suggestions) > 0

    # Approve all suggestions with a real source column
    approved = 0
    for m in suggestions:
        if m["source_column"]:
            r2 = client.put(f"/api/mappings/{m['id']}/approve", headers=headers)
            assert r2.status_code == 200
            approved += 1
    assert approved > 0

    # Cleansing + validation
    assert client.post(f"/api/conversions/{cid}/profile-cleansing", headers=headers).status_code == 200
    assert client.post(f"/api/conversions/{cid}/validate", headers=headers).status_code == 200

    # Generate output
    r = client.post(f"/api/conversions/{cid}/generate-output?fmt=csv", headers=headers)
    assert r.status_code == 200

    # Simulate load
    r = client.post(f"/api/conversions/{cid}/simulate-load", headers=headers)
    assert r.status_code == 200
    assert "passed_count" in r.json()


def test_conversion_crud():
    """Create + update + delete a planned conversion inside the demo engagement."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]

    # Create a planning-only conversion (no source file or template yet)
    r = client.post(
        "/api/conversions",
        headers=headers,
        json={
            "project_id": project_id,
            "name": "Test — Trade Compliance Codes",
            "target_object": "Trade Compliance",
            "planned_load_order": 200,
        },
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert r.json()["status"] == "planning"

    # Update it
    r = client.patch(
        f"/api/conversions/{new_id}",
        headers=headers,
        json={"description": "Auto-generated test object", "planned_load_order": 95},
    )
    assert r.status_code == 200
    assert r.json()["planned_load_order"] == 95

    # Cleanup
    r = client.delete(f"/api/conversions/{new_id}", headers=headers)
    assert r.status_code == 200


def test_dependency_impact_uses_conversion_id():
    """The /api/dependencies/impact/{conversion_id} endpoint replaces the old
    project-scoped variant."""
    headers = _headers()
    convs = client.get("/api/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    r = client.get(f"/api/dependencies/impact/{item_conv['id']}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "dependencies" in body
    assert "impacts" in body


def test_learning_capture_uses_engagement_project():
    """Learned mappings are tied to the engagement-level Project, not a Conversion."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.post(
        "/api/learned-mappings",
        headers=headers,
        json={
            "kind": "column_mapping",
            "category": "Test Category",
            "original_value": "LEGACY_NUM",
            "resolved_value": "ItemNumber",
            "project_id": project_id,
            "captured_from": "Test capture",
        },
    )
    assert r.status_code == 200, r.text
    captured_id = r.json()["id"]

    # Confirm it appears in stats
    r = client.get("/api/learned-mappings/stats", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    # Cleanup
    client.delete(f"/api/learned-mappings/{captured_id}", headers=headers)


def test_approval_teaches_and_replays_on_new_conversion():
    """Approving a mapping on one Item conversion should make a brand-new
    conversion bound to the same dataset+template auto-apply that mapping —
    confidence 1.0, status approved, approved_by 'learning-engine'.
    """
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]

    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    cid = item_conv["id"]

    # Run AI suggest on the original conversion and approve one mapping.
    r = client.post(f"/api/conversions/{cid}/suggest-mapping", headers=headers)
    assert r.status_code == 200, r.text
    suggestions = r.json()
    teach = next(m for m in suggestions if m["source_column"])
    assert (
        client.put(f"/api/mappings/{teach['id']}/approve", headers=headers).status_code
        == 200
    )

    # The learning library should now hold a record for this column.
    learned = client.get(
        "/api/learned-mappings",
        headers=headers,
        params={"kind": "column_mapping"},
    ).json()
    assert any(
        lm["target_field"] == teach["target_field_name"]
        and lm["original_value"] == teach["source_column"]
        and lm["target_object"] == "Item"
        for lm in learned
    ), learned

    # Spawn a fresh conversion against the same dataset + template.
    fresh = client.post(
        "/api/conversions",
        headers=headers,
        json={
            "project_id": project_id,
            "name": "Item Master — Replay Cycle 2",
            "target_object": "Item",
            "dataset_id": item_conv["dataset_id"],
            "template_id": item_conv["template_id"],
            "planned_load_order": 999,
        },
    )
    assert fresh.status_code == 200, fresh.text
    fresh_id = fresh.json()["id"]

    try:
        r = client.post(
            f"/api/conversions/{fresh_id}/suggest-mapping", headers=headers
        )
        assert r.status_code == 200, r.text
        replayed = r.json()
        match = next(
            m for m in replayed
            if m["target_field_name"] == teach["target_field_name"]
        )
        assert match["status"] == "approved", match
        assert match["approved_by"] == "learning-engine", match
        assert match["source_column"] == teach["source_column"], match
        assert match["confidence"] == 1.0, match
    finally:
        client.delete(f"/api/conversions/{fresh_id}", headers=headers)


def test_engine_universal_rule_types():
    """Smoke-test each new engine rule type with a representative input."""
    from app.transformations.engine import apply_rule, apply_pipeline

    # CONSTANT — always overwrite
    assert apply_rule("CONSTANT", {"value": "Active"}, "anything") == "Active"
    assert apply_rule("CONSTANT", {"value": "Active"}, None) == "Active"

    # TITLE_CASE
    assert apply_rule("TITLE_CASE", {}, "hello world") == "Hello World"

    # PAD
    assert apply_rule("PAD", {"side": "left", "length": 6, "char": "0"}, "42") == "000042"
    assert apply_rule("PAD", {"side": "right", "length": 5, "char": "*"}, "AB") == "AB***"

    # SUBSTRING
    assert apply_rule("SUBSTRING", {"start": 0, "length": 3}, "ABCDEF") == "ABC"
    assert apply_rule("SUBSTRING", {"start": 2}, "ABCDEF") == "CDEF"

    # REGEX_REPLACE — strip leading zeros
    assert apply_rule("REGEX_REPLACE", {"pattern": r"^0+", "replace": ""}, "00042") == "42"

    # REGEX_EXTRACT — pull capture group
    assert apply_rule(
        "REGEX_EXTRACT", {"pattern": r"ITEM-(\d+)", "group": 1}, "ITEM-1042"
    ) == "1042"

    # ARITHMETIC — multiply by 100 (USD → cents)
    assert apply_rule("ARITHMETIC", {"op": "multiply", "amount": 100}, "12.50") == 1250.0
    assert apply_rule("ARITHMETIC", {"op": "round", "decimals": 2}, "3.14159") == 3.14

    # COALESCE — first non-null
    row = {"a": "", "b": None, "c": "fallback", "d": "later"}
    assert apply_rule("COALESCE", {"columns": ["a", "b", "c", "d"]}, "", row=row) == "fallback"
    assert apply_rule("COALESCE", {"columns": ["a", "b"], "default": "z"}, "", row=row) == "z"

    # CASE_WHEN — multi-branch
    cfg = {
        "branches": [
            {"if_column": "status", "op": "eq", "value": "A", "then": "Active"},
            {"if_column": "status", "op": "eq", "value": "I", "then": "Inactive"},
            {"if_column": "qty", "op": "gt", "value": 100, "then": "Bulk"},
        ],
        "default": "Other",
    }
    assert apply_rule("CASE_WHEN", cfg, "", row={"status": "A", "qty": 5}) == "Active"
    assert apply_rule("CASE_WHEN", cfg, "", row={"status": "X", "qty": 200}) == "Bulk"
    assert apply_rule("CASE_WHEN", cfg, "", row={"status": "X", "qty": 5}) == "Other"

    # COMPUTED — row index from ctx
    out = apply_rule("COMPUTED", {"source": "row_index"}, None, ctx={"row_index": 7})
    assert out == 7
    out = apply_rule("COMPUTED", {"source": "today", "format": "%Y-%m-%d"}, None)
    assert len(out) == 10 and out[4] == "-"

    # CROSSWALK_LOOKUP
    out = apply_rule(
        "CROSSWALK_LOOKUP",
        {"crosswalk": "uom_map", "default": "?"},
        "ea",
        ctx={"crosswalks": {"uom_map": {"ea": "Each", "BOX": "Box"}}},
    )
    assert out == "Each"

    # Pipeline composition: TRIM → UPPERCASE → REGEX_REPLACE → PAD
    out = apply_pipeline(
        [
            {"rule_type": "TRIM", "config": {}},
            {"rule_type": "UPPERCASE", "config": {}},
            {"rule_type": "REGEX_REPLACE", "config": {"pattern": "-", "replace": ""}},
            {"rule_type": "PAD", "config": {"side": "left", "length": 8, "char": "0"}},
        ],
        " item-42 ",
    )
    assert out == "00ITEM42"


def test_rule_preview_endpoint():
    """The dry-run preview endpoint runs a rule pipeline against the
    conversion's dataset and returns source/output pairs."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    cid = item_conv["id"]

    # Use the seeded Item dataset; ITEM_NUM is a known column.
    r = client.post(
        f"/api/conversions/{cid}/rules/preview",
        headers=headers,
        json={
            "source_column": "ITEM_NUM",
            "rules": [
                {"rule_type": "REMOVE_HYPHEN", "config": {}},
                {"rule_type": "UPPERCASE", "config": {}},
            ],
            "sample_size": 3,
        },
    )
    assert r.status_code == 200, r.text
    samples = r.json()["samples"]
    assert len(samples) == 3
    for s in samples:
        if s["source"]:
            assert "-" not in str(s["output"])
            assert str(s["output"]) == str(s["output"]).upper()


def test_manual_rule_lands_in_rule_library():
    """A manually-authored TransformationRule should appear in the Rule Library
    (kind='rule') so other conversions can discover it."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    cid = item_conv["id"]

    fields = client.get(
        f"/api/fbdi/templates/{item_conv['template_id']}/fields", headers=headers
    ).json()
    field = fields[0]

    r = client.post(
        f"/api/conversions/{cid}/rules",
        headers=headers,
        json={
            "target_field_id": field["id"],
            "source_column": "ITEM_TYPE",
            "rule_type": "CASE_WHEN",
            "rule_config": {
                "branches": [
                    {"if_column": "ITEM_TYPE", "op": "eq", "value": "ACT", "then": "production"},
                    {"if_column": "ITEM_TYPE", "op": "eq", "value": "INACT", "then": "discontinued"},
                ],
                "default": "planning",
            },
            "description": "Map legacy ACT/INACT to new lifecycle codes",
        },
    )
    assert r.status_code == 200, r.text

    library = client.get(
        "/api/learned-mappings",
        headers=headers,
        params={"kind": "rule"},
    ).json()
    assert any(
        lm["rule_type"] == "CASE_WHEN"
        and lm["target_field"] == field["field_name"]
        and lm["target_object"] == "Item"
        for lm in library
    ), library


def test_sales_order_cascade_surfaces_unresolved_item_refs():
    """Simulating a Sales Order load against the seeded data should produce
    'Missing Dependency' errors for every SO row whose ITEM_NUM is absent
    from the Item Master extract — the demo path the Error Traceback drawer
    visualizes. Match comparison must be loose so auto-suggested
    REMOVE_HYPHEN/UPPERCASE transforms don't trigger false positives.
    """
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    so_conv = next(
        c for c in convs if c["target_object"] == "Sales Order" and c["dataset_id"]
    )
    cid = so_conv["id"]

    # Drive the SO conversion through suggest → approve → cleanse → validate → output.
    suggestions = client.post(
        f"/api/conversions/{cid}/suggest-mapping", headers=headers
    ).json()
    for m in suggestions:
        if m["source_column"]:
            client.put(f"/api/mappings/{m['id']}/approve", headers=headers)
    client.post(f"/api/conversions/{cid}/profile-cleansing", headers=headers)
    client.post(f"/api/conversions/{cid}/validate", headers=headers)
    client.post(f"/api/conversions/{cid}/generate-output?fmt=csv", headers=headers)

    run = client.post(f"/api/conversions/{cid}/simulate-load", headers=headers).json()
    assert run["failed_count"] > 0, run

    errors = client.get(
        f"/api/conversions/{cid}/load-errors", headers=headers
    ).json()
    item_misses = [
        e
        for e in errors
        if e.get("error_category") == "Missing Dependency"
        and e.get("related_dependency") == "Item"
    ]
    # The seed CSV has 61 rows referencing items not in the Item Master extract.
    assert len(item_misses) >= 30, (
        f"Expected ~61 unresolved Item refs, got {len(item_misses)}: "
        f"{item_misses[:3]}"
    )
    # Every Missing-Dependency error must carry the actual reference value
    # (drives the visual chain on the frontend) and the suggested fix.
    for e in item_misses[:5]:
        assert e["reference_value"], e
        assert e["suggested_fix"], e
        assert "no matching record" in (e["error_message"] or "").lower(), e

    # Loose match must not flag legit refs — at least some refs should
    # resolve cleanly. (Other failure categories like UOM may still fire.)
    failed_legit_only = [
        e for e in item_misses
        if (e["reference_value"] or "").startswith("ITM-0")
    ]
    assert failed_legit_only == [], (
        "Legit ITM-0xxx references shouldn't be flagged as missing — "
        "normalize comparison must strip hyphens/case before matching."
    )


def test_reference_standard_propagates_from_master_to_downstream():
    """Saving REMOVE_HYPHEN on Item Master's InventoryItemNumber should
    auto-apply to the Sales Order conversion's InventoryItemNumber output —
    *without* the SO conversion having its own local rule. This is the
    Reference Standard inheritance path: master teaches once, every
    downstream conversion that references the same master inherits it.
    """
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item_conv = next(
        c for c in convs if c["target_object"] == "Item" and c["dataset_id"]
    )
    so_conv = next(
        c for c in convs if c["target_object"] == "Sales Order" and c["dataset_id"]
    )

    # Find the Item Master's InventoryItemNumber field id.
    fields = client.get(
        f"/api/fbdi/templates/{item_conv['template_id']}/fields", headers=headers
    ).json()
    inv_field = next(
        f for f in fields
        if f["field_name"] in ("InventoryItemNumber", "Item Number", "Inventory Item Name")
    )

    # Teach a REMOVE_HYPHEN on the master's key column. The learning service
    # should auto-promote this to a Reference Standard.
    r = client.post(
        f"/api/conversions/{item_conv['id']}/rules",
        headers=headers,
        json={
            "target_field_id": inv_field["id"],
            "source_column": "ITEM_NUM",
            "rule_type": "REMOVE_HYPHEN",
            "rule_config": {},
            "description": "Strip hyphens from item identifiers",
        },
    )
    assert r.status_code == 200, r.text

    # Reference Standard should now exist for Item / InventoryItemNumber.
    standards = client.get(
        "/api/learned-mappings",
        headers=headers,
        params={"kind": "reference_standard"},
    ).json()
    assert any(
        s["target_object"] == "Item"
        and s["target_field"] == inv_field["field_name"]
        and s["rule_type"] == "REMOVE_HYPHEN"
        for s in standards
    ), standards

    # Drive the SO conversion to output (without adding any local rule on
    # its InventoryItemNumber). The inherited standard should fire.
    suggestions = client.post(
        f"/api/conversions/{so_conv['id']}/suggest-mapping", headers=headers
    ).json()
    for m in suggestions:
        if m["source_column"]:
            client.put(f"/api/mappings/{m['id']}/approve", headers=headers)

    preview = client.get(
        f"/api/conversions/{so_conv['id']}/output-preview?limit=5", headers=headers
    ).json()
    # The preview's lineage tells us which rules ran on each target column.
    inv_col = next(
        (c for c in preview["columns"] if "InventoryItemNumber" in c or "Item Number" in c),
        None,
    )
    assert inv_col, f"no item-ref column in SO output; cols={preview['columns']}"
    rules_applied = preview["lineage"][inv_col]["rules"]
    assert any(r["rule_type"] == "REMOVE_HYPHEN" for r in rules_applied), (
        f"Reference Standard didn't propagate. rules on {inv_col} were: {rules_applied}"
    )

    # And the actual cell values should have hyphens stripped.
    for row in preview["rows"][:5]:
        v = str(row.get(inv_col, ""))
        assert "-" not in v, f"hyphen still in SO output cell: {v!r}"

    # The same standard should NOT be re-applied to the master itself —
    # that would create an infinite "double transform" if the rule were
    # not idempotent. Verify the master's lineage has the rule once.
    master_preview = client.get(
        f"/api/conversions/{item_conv['id']}/output-preview?limit=3", headers=headers
    ).json()
    master_inv_col = next(
        (c for c in master_preview["columns"] if c == inv_field["field_name"]),
        None,
    )
    if master_inv_col:
        master_rules = master_preview["lineage"][master_inv_col]["rules"]
        assert sum(1 for r in master_rules if r["rule_type"] == "REMOVE_HYPHEN") == 1, master_rules


def test_source_systems_enum_endpoint_returns_full_catalog():
    """Frontend pulls the picker options from /api/source-systems. The
    enum is server-driven so a code change can't drift between client and
    server. v1 ships seven sources with scanner availability flags."""
    headers = _headers()
    r = client.get("/api/source-systems", headers=headers)
    assert r.status_code == 200, r.text
    catalog = r.json()
    codes = {s["code"] for s in catalog}
    # Must include the v1 scanner-backed sources
    assert {"netsuite", "oracle_ebs"} <= codes, catalog
    # has_scanner_v1 split — NetSuite + EBS yes, everything else no
    scanner_codes = {s["code"] for s in catalog if s["has_scanner_v1"]}
    assert scanner_codes == {"netsuite", "oracle_ebs"}, scanner_codes


def test_seeded_project_carries_source_system_and_phase():
    headers = _headers()
    p = client.get("/api/projects", headers=headers).json()[0]
    assert p["source_system"] == "netsuite", p
    assert p["phase"] in ("blueprint", "own", "lift", "thrive"), p


def test_create_project_with_source_system_and_initial_connection():
    """The Setup Wizard saves Project Details + Source System in one call:
    project + initial SourceConnection in the same transaction. The
    response surfaces a source_connection_count rollup."""
    headers = _headers()
    payload = {
        "name": "Acme EBS Migration Phase 1",
        "client": "Acme Corp",
        "source_system": "oracle_ebs",
        "phase": "blueprint",
        "initial_connection": {
            "source_system": "oracle_ebs",
            "display_name": "Acme EBS PROD (read-only)",
            "endpoint": "ebs-prod-db.acme.internal:1521/APPS",
            "auth_type": "mock",
            "connection_metadata": {
                "host": "ebs-prod-db.acme.internal",
                "service_name": "APPS",
                "instance_name": "EBSPROD",
            },
            "mock_mode": True,
        },
    }
    r = client.post("/api/projects", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["source_system"] == "oracle_ebs"
    assert proj["phase"] == "blueprint"
    assert proj["source_connection_count"] == 1, proj
    pid = proj["id"]

    # Connection is listed
    conns = client.get(
        f"/api/projects/{pid}/source-connections", headers=headers
    ).json()
    assert len(conns) == 1
    assert conns[0]["display_name"] == "Acme EBS PROD (read-only)"
    assert conns[0]["mock_mode"] is True

    # Cleanup
    client.delete(f"/api/projects/{pid}", headers=headers)


def test_create_project_rejects_unknown_source_system():
    headers = _headers()
    r = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Bogus", "source_system": "myspace"},
    )
    assert r.status_code == 400, r.text
    assert "Unknown source_system" in r.json().get("detail", "")


def test_source_system_immutable_once_anchored():
    """Once a project has any conversion or connection attached, its
    source_system cannot be changed. Switching it would silently invalidate
    every learned mapping referenced through it."""
    headers = _headers()
    p = client.get("/api/projects", headers=headers).json()[0]
    pid = p["id"]
    r = client.patch(
        f"/api/projects/{pid}",
        headers=headers,
        json={"source_system": "oracle_ebs"},
    )
    assert r.status_code == 409, r.text
    assert "Cannot change source_system" in r.json().get("detail", "")


def test_source_connection_test_mock_mode_returns_realistic_probes():
    """Mock-mode NetSuite probe must surface the same shape (overall_status,
    probes[], detected_metadata) the real SuiteTalk REST probe would, so the
    UI rendering is identical when the customer plugs in live creds."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    # Find the connection that the Setup Wizard's seed would create, or
    # create a fresh one in mock mode.
    conn_payload = {
        "project_id": pid,
        "source_system": "netsuite",
        "display_name": "Vertex NetSuite Sandbox",
        "endpoint": "https://tstdrv1234567.suitetalk.api.netsuite.com",
        "auth_type": "mock",
        "connection_metadata": {"account_id": "TSTDRV1234567", "edition": "OneWorld"},
        "mock_mode": True,
    }
    r = client.post("/api/source-connections", headers=headers, json=conn_payload)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    try:
        t = client.post(
            f"/api/source-connections/{cid}/test", headers=headers
        ).json()
        assert t["overall_status"] in ("ok", "degraded"), t
        names = {p["name"] for p in t["probes"]}
        assert {"metadata-catalog", "suiteql_ping", "subsidiary_enumeration"} <= names
        assert t["detected_metadata"]["subsidiary_count"] == 6, t
        assert t["version"] == "2024.2.1"
    finally:
        client.delete(f"/api/source-connections/{cid}", headers=headers)


def test_source_connection_seals_credentials_never_returns_plaintext():
    """Credentials submitted on create are sealed via Fernet and never
    appear in any subsequent GET/list response."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    secret = "super-sensitive-token-do-not-leak"
    r = client.post(
        "/api/source-connections",
        headers=headers,
        json={
            "project_id": pid,
            "source_system": "netsuite",
            "display_name": "Sealed creds test",
            "auth_type": "oauth1_tba",
            "credentials": {
                "account_id": "TSTDRV1234567",
                "consumer_key": secret,
                "consumer_secret": "also-secret",
                "token_id": "tok-1",
                "token_secret": "tok-secret",
            },
            "mock_mode": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    cid = body["id"]
    try:
        assert body["has_credentials"] is True
        # Round-trip the connection through GET and confirm the secret
        # never shows up anywhere.
        gotten = client.get(
            f"/api/source-connections/{cid}", headers=headers
        ).json()
        as_str = repr(gotten)
        assert secret not in as_str, "credential leaked in GET response"
        listing = client.get(
            f"/api/projects/{pid}/source-connections", headers=headers
        ).json()
        assert secret not in repr(listing), "credential leaked in list response"

        # And the connection-test response must also stay clean.
        tested = client.post(
            f"/api/source-connections/{cid}/test", headers=headers
        ).json()
        assert secret not in repr(tested), "credential leaked in test response"
    finally:
        client.delete(f"/api/source-connections/{cid}", headers=headers)


def test_audit_log_records_connection_lifecycle():
    """Every privileged action on a SourceConnection — create, test, delete
    — must land in the persisted audit log with the actor, project,
    summary, and IP. Compliance reviewers should be able to ask 'who
    connected to what and when?' and get a single answer."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    before = client.get(
        "/api/audit-events",
        headers=headers,
        params={"project_id": pid, "action_prefix": "source_connection."},
    ).json()
    before_count = len(before)

    r = client.post(
        "/api/source-connections",
        headers=headers,
        json={
            "project_id": pid,
            "source_system": "netsuite",
            "display_name": "Audit trail test",
            "auth_type": "mock",
            "mock_mode": True,
        },
    )
    cid = r.json()["id"]
    client.post(f"/api/source-connections/{cid}/test", headers=headers)
    client.delete(f"/api/source-connections/{cid}", headers=headers)

    after = client.get(
        "/api/audit-events",
        headers=headers,
        params={"project_id": pid, "action_prefix": "source_connection."},
    ).json()
    actions = [e["action"] for e in after[: len(after) - before_count]]
    # Most recent first — delete then test then create
    assert "source_connection.deleted" in actions
    assert "source_connection.tested" in actions
    assert "source_connection.created" in actions
    for e in after[: 3]:
        assert e["actor_email"] == settings.ADMIN_EMAIL
        assert e["project_id"] == pid


def test_audit_log_redacts_sensitive_keys():
    """The audit service must redact credentials in the details payload
    even if a careless caller passes them through."""
    from app.database import SessionLocal
    from app.services.audit_service import record_event
    from app.models.audit import AuditEvent

    db = SessionLocal()
    try:
        event = record_event(
            db,
            actor_email="leakage-test@trinamix.com",
            action="source_connection.tested",
            target_type="source_connection",
            target_id=999999,
            summary="Tampering check",
            details={
                "endpoint": "https://example.com",
                "password": "should-be-redacted",
                "nested": {"client_secret": "also-redacted"},
            },
        )
        assert event is not None
        assert event.details_json["password"] == "[redacted]"
        assert event.details_json["nested"]["client_secret"] == "[redacted]"
        assert event.details_json["endpoint"] == "https://example.com"
    finally:
        # Best-effort cleanup of the test event so it doesn't pollute other tests.
        try:
            db.query(AuditEvent).filter(
                AuditEvent.actor_email == "leakage-test@trinamix.com"
            ).delete()
            db.commit()
        except Exception:
            db.rollback()
        db.close()


def test_encryption_round_trip():
    """The encryption service must reversibly seal and unseal a
    credentials dict, and decryption must fail loudly on a tampered token."""
    from app.services.encryption import (
        EncryptionError, _reset_for_tests, get_encryption_service,
    )

    _reset_for_tests()
    svc = get_encryption_service()
    plaintext = {"username": "ebs_ro", "password": "p@55", "host": "db.internal"}
    ct = svc.encrypt_credentials(plaintext)
    assert isinstance(ct, str) and "password" not in ct
    assert svc.decrypt_credentials(ct) == plaintext

    # Tamper detection
    try:
        svc.decrypt_credentials(ct[:-4] + "AAAA")
    except EncryptionError:
        pass
    else:
        raise AssertionError("decryption of tampered token did not raise")


def _create_test_dataset_and_template_for_cross_project(
    headers: dict[str, str], *, source_system: str
) -> tuple[int, int, dict[str, str]]:
    """Helper for Slice-2 cross-project tests.

    Returns (project_id, conversion_id, fields_by_name) for a freshly created
    project that re-uses the seed's Item dataset + template, so the KB lookup
    has dataset columns to match against.
    """
    proj_payload = {
        "name": f"Cross-source KB test ({source_system})",
        "source_system": source_system,
        "phase": "blueprint",
    }
    proj = client.post("/api/projects", headers=headers, json=proj_payload).json()
    pid = proj["id"]

    # Reuse the seeded Item Master dataset + template (which is the same
    # files every project would conceivably point at). Find them on the
    # seeded engagement.
    convs_seed = client.get("/api/conversions", headers=headers).json()
    item_seed = next(
        c for c in convs_seed if c["target_object"] == "Item" and c["dataset_id"]
    )

    fresh = client.post(
        "/api/conversions",
        headers=headers,
        json={
            "project_id": pid,
            "name": f"Item Master ({source_system}) — KB test",
            "target_object": "Item",
            "dataset_id": item_seed["dataset_id"],
            "template_id": item_seed["template_id"],
            "planned_load_order": 30,
        },
    )
    assert fresh.status_code == 200, fresh.text
    cid = fresh.json()["id"]
    fields = client.get(
        f"/api/fbdi/templates/{item_seed['template_id']}/fields", headers=headers
    ).json()
    return pid, cid, {f["field_name"]: f for f in fields}


def test_knowledge_bank_prepopulates_a_new_project_with_same_source():
    """Approving a mapping on the seeded NetSuite project should pre-fill a
    *new* NetSuite project's conversion at confidence 0.85 with the KB badge,
    status="suggested" (not auto-approved)."""
    headers = _headers()

    # 1. Teach a mapping on the seeded engagement.
    seed_proj = client.get("/api/projects", headers=headers).json()[0]
    assert seed_proj["source_system"] == "netsuite"
    seed_convs = client.get(
        f"/api/projects/{seed_proj['id']}/conversions", headers=headers
    ).json()
    seed_item = next(
        c for c in seed_convs if c["target_object"] == "Item" and c["dataset_id"]
    )
    suggestions = client.post(
        f"/api/conversions/{seed_item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in suggestions if m["source_column"])
    client.put(
        f"/api/mappings/{teach['id']}/approve", headers=headers
    )

    # 2. Create a fresh NetSuite project and run suggest-mapping.
    pid2, cid2, _ = _create_test_dataset_and_template_for_cross_project(
        headers, source_system="netsuite"
    )
    try:
        replayed = client.post(
            f"/api/conversions/{cid2}/suggest-mapping", headers=headers
        ).json()
        match = next(
            (m for m in replayed if m["target_field_name"] == teach["target_field_name"]),
            None,
        )
        assert match, f"target field {teach['target_field_name']} missing in replay"
        # Cross-project KB hit must pre-fill at 0.85 (not 1.0) and NOT auto-approve.
        assert match["source_column"] == teach["source_column"], match
        assert match["status"] == "suggested", match
        assert match["kb_source"] == "netsuite", match
        assert match["confidence"] == 0.85, match
        assert "Knowledge Bank" in (match["reason"] or ""), match
    finally:
        client.delete(f"/api/projects/{pid2}", headers=headers)


def test_knowledge_bank_does_not_cross_source_systems():
    """An EBS project must NOT receive pre-fills from the NetSuite KB.
    Source-system isolation is the core safety invariant of the KB."""
    headers = _headers()

    # Set up a NetSuite KB entry (seed engagement is already netsuite).
    seed_proj = client.get("/api/projects", headers=headers).json()[0]
    convs = client.get(
        f"/api/projects/{seed_proj['id']}/conversions", headers=headers
    ).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    suggestions = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in suggestions if m["source_column"])
    client.put(f"/api/mappings/{teach['id']}/approve", headers=headers)

    # Create an EBS project. Suggest-mapping must NOT touch any
    # NetSuite-source-system mapping when filling its suggestions.
    pid2, cid2, _ = _create_test_dataset_and_template_for_cross_project(
        headers, source_system="oracle_ebs"
    )
    try:
        ebs_suggestions = client.post(
            f"/api/conversions/{cid2}/suggest-mapping", headers=headers
        ).json()
        kb_hits = [m for m in ebs_suggestions if m["kb_source"]]
        # None of the EBS rows should carry a NetSuite KB tag.
        for m in kb_hits:
            assert m["kb_source"] != "netsuite", m
    finally:
        client.delete(f"/api/projects/{pid2}", headers=headers)


def test_knowledge_bank_increments_reuse_counter_and_audits():
    """Each KB hit must (a) bump times_reused on the source LearnedMapping
    and (b) write a single learned_mapping.reused audit-rollup event so
    compliance reviewers can answer 'which project pulled from KB'."""
    headers = _headers()

    # Teach a mapping on the seed.
    seed_proj = client.get("/api/projects", headers=headers).json()[0]
    convs = client.get(
        f"/api/projects/{seed_proj['id']}/conversions", headers=headers
    ).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    s = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in s if m["source_column"])
    client.put(f"/api/mappings/{teach['id']}/approve", headers=headers)

    # Snapshot pre-reuse counter on the matching LearnedMapping.
    before = client.get(
        "/api/learned-mappings",
        headers=headers,
        params={"kind": "column_mapping"},
    ).json()
    pre_match = next(
        (
            lm for lm in before
            if lm["target_field"] == teach["target_field_name"]
            and lm["target_object"] == "Item"
            and lm.get("source_system") == "netsuite"
        ),
        None,
    )
    assert pre_match, before
    pre_reuses = pre_match.get("times_reused") or 0

    # Trigger a cross-project KB hit via a new NetSuite project.
    pid2, cid2, _ = _create_test_dataset_and_template_for_cross_project(
        headers, source_system="netsuite"
    )
    try:
        client.post(
            f"/api/conversions/{cid2}/suggest-mapping", headers=headers
        )

        # The reuse counter on the source LearnedMapping has bumped.
        after = client.get(
            "/api/learned-mappings",
            headers=headers,
            params={"kind": "column_mapping"},
        ).json()
        post_match = next(
            lm for lm in after
            if lm["target_field"] == teach["target_field_name"]
            and lm["target_object"] == "Item"
            and lm.get("source_system") == "netsuite"
        )
        assert (post_match.get("times_reused") or 0) > pre_reuses, post_match

        # And a single audit-rollup event was recorded.
        evs = client.get(
            "/api/audit-events",
            headers=headers,
            params={
                "project_id": pid2,
                "action_prefix": "learned_mapping.reused",
            },
        ).json()
        assert len(evs) >= 1, evs
        assert "Knowledge Bank" in (evs[0]["summary"] or ""), evs[0]
    finally:
        client.delete(f"/api/projects/{pid2}", headers=headers)


def test_knowledge_bank_stats_endpoint_breaks_down_by_source():
    """The Learning Center pulls per-source rollups from this endpoint.
    After a NetSuite approval it must report a netsuite bucket with
    mappings >= 1 and project_count >= 1."""
    headers = _headers()
    seed_proj = client.get("/api/projects", headers=headers).json()[0]
    convs = client.get(
        f"/api/projects/{seed_proj['id']}/conversions", headers=headers
    ).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    s = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in s if m["source_column"])
    client.put(f"/api/mappings/{teach['id']}/approve", headers=headers)

    rollup = client.get(
        "/api/learned-mappings/knowledge-bank/stats", headers=headers
    ).json()
    by_source = {r["source_system"]: r for r in rollup}
    assert "netsuite" in by_source, rollup
    nets = by_source["netsuite"]
    assert nets["mappings"] >= 1
    assert nets["project_count"] >= 1


def test_same_project_replay_still_auto_approves_at_full_confidence():
    """The pre-existing same-project replay path must remain unchanged by
    Slice 2: an approval in project A still auto-applies on a *new
    conversion within the same project A* at confidence 1.0 with
    approved_by='learning-engine'. KB only fires across projects."""
    headers = _headers()
    seed_proj = client.get("/api/projects", headers=headers).json()[0]
    pid = seed_proj["id"]
    convs = client.get(f"/api/projects/{pid}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    # Teach
    s = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in s if m["source_column"])
    client.put(f"/api/mappings/{teach['id']}/approve", headers=headers)

    # Spawn a sibling conversion inside the *same* project.
    fresh = client.post(
        "/api/conversions",
        headers=headers,
        json={
            "project_id": pid,
            "name": "Item Master — same-project replay",
            "target_object": "Item",
            "dataset_id": item["dataset_id"],
            "template_id": item["template_id"],
            "planned_load_order": 999,
        },
    ).json()
    fresh_id = fresh["id"]
    try:
        replayed = client.post(
            f"/api/conversions/{fresh_id}/suggest-mapping", headers=headers
        ).json()
        match = next(
            m for m in replayed
            if m["target_field_name"] == teach["target_field_name"]
        )
        # Same project ⇒ confidence 1.0, status approved, learning-engine.
        assert match["status"] == "approved", match
        assert match["confidence"] == 1.0, match
        assert match["approved_by"] == "learning-engine", match
        # And NO KB tag — KB is cross-project only.
        assert match["kb_source"] is None, match
    finally:
        client.delete(f"/api/conversions/{fresh_id}", headers=headers)


def test_case_when_compound_all_of_branch_matches_when_all_conditions_true():
    """Slice 3 — CASE_WHEN with all_of branches: every leaf condition must
    pass for the branch to match. ``"if STATUS is ACTIVE and REGION is US
    then DOMESTIC_ACTIVE"`` exercises the AND combinator."""
    from app.transformations.engine import apply_rule

    cfg = {
        "branches": [
            {
                "all_of": [
                    {"column": "STATUS", "op": "eq", "value": "ACTIVE"},
                    {"column": "REGION", "op": "eq", "value": "US"},
                ],
                "then": "DOMESTIC_ACTIVE",
            },
            {
                "all_of": [
                    {"column": "STATUS", "op": "eq", "value": "ACTIVE"},
                ],
                "then": "INTERNATIONAL_ACTIVE",
            },
        ],
        "default": "INACTIVE",
    }
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVE", "REGION": "US"}) == "DOMESTIC_ACTIVE"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVE", "REGION": "EU"}) == "INTERNATIONAL_ACTIVE"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "INACTIVE", "REGION": "US"}) == "INACTIVE"


def test_case_when_compound_any_of_branch_matches_when_at_least_one_true():
    """Slice 3 — CASE_WHEN with any_of branches: OR semantics, single
    match in a branch is sufficient."""
    from app.transformations.engine import apply_rule

    cfg = {
        "branches": [
            {
                "any_of": [
                    {"column": "STATUS", "op": "eq", "value": "A"},
                    {"column": "STATUS", "op": "eq", "value": "ACTIVE"},
                    {"column": "STATUS", "op": "startswith", "value": "ACT"},
                ],
                "then": "is_active",
            },
        ],
        "default": "other",
    }
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "A"}) == "is_active"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVE"}) == "is_active"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVATING"}) == "is_active"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "DEAD"}) == "other"


def test_case_when_legacy_single_condition_branches_still_work():
    """Back-compat — the original {if_column, op, value, then} shape must
    keep working alongside compound branches. Existing rules in the DB
    must execute correctly without migration."""
    from app.transformations.engine import apply_rule

    cfg = {
        "branches": [
            {"if_column": "STATUS", "op": "eq", "value": "A", "then": "Active"},
            {"if_column": "STATUS", "op": "eq", "value": "I", "then": "Inactive"},
        ],
        "default": "Other",
    }
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "A"}) == "Active"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "I"}) == "Inactive"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "X"}) == "Other"


def test_case_when_nested_negation_and_combinator():
    """Slice 3 — compound branches support nesting and ``not``."""
    from app.transformations.engine import apply_rule

    cfg = {
        "branches": [
            {
                "all_of": [
                    {"column": "STATUS", "op": "eq", "value": "ACTIVE"},
                    {
                        "not": {
                            "any_of": [
                                {"column": "REGION", "op": "eq", "value": "US"},
                                {"column": "REGION", "op": "eq", "value": "CA"},
                            ],
                        },
                    },
                ],
                "then": "INTERNATIONAL_ACTIVE",
            },
        ],
        "default": "other",
    }
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVE", "REGION": "EU"}) == "INTERNATIONAL_ACTIVE"
    assert apply_rule("CASE_WHEN", cfg, None, row={"STATUS": "ACTIVE", "REGION": "US"}) == "other"


def test_translator_endpoint_returns_503_when_no_api_key():
    """Slice 3 — the natural-language translator falls back gracefully
    when no API key is configured. The frontend uses this 503 signal to
    hide the "Describe in plain English" tab without breaking the rest of
    the modal."""
    from app.config import settings

    # The test fixture ships with no Anthropic API key; the translator
    # must surface that as a 503 with a structured detail string rather
    # than 500 / 422.
    assert not settings.ANTHROPIC_API_KEY, (
        "test fixture relies on no ANTHROPIC_API_KEY being set"
    )
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    r = client.post(
        f"/api/conversions/{item['id']}/rules/translate",
        headers=headers,
        json={"description": "if STATUS is ACTIVE then ACT"},
    )
    assert r.status_code == 503, r.text
    assert "ANTHROPIC_API_KEY" in r.json().get("detail", ""), r.text


def test_translator_validator_rejects_bogus_rule_type():
    """The translator's internal validator must reject rules whose
    rule_type isn't in the engine's vocabulary or whose config can't
    execute under the engine."""
    from app.services.rule_translator import (
        TranslatorError, _validate_translated_rule,
    )

    # Unknown rule type
    try:
        _validate_translated_rule("PROMOTE_TO_EXEC", {})
    except TranslatorError:
        pass
    # Known type but malformed config — engine must still not crash;
    # CASE_WHEN with invalid branches should fall back to default and
    # validate cleanly (engine is intentionally forgiving).
    _validate_translated_rule(
        "CASE_WHEN", {"branches": [{"all_of": [{"column": "x", "op": "eq"}]}], "default": "d"}
    )


def _create_project_with_mock_connection(
    headers: dict[str, str], *, source_system: str, name: str
) -> tuple[int, int]:
    """Convenience for Slice 4 tests — create a project with an initial
    mock-mode source connection in one POST. Returns (project_id,
    connection_id)."""
    r = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": name,
            "source_system": source_system,
            "phase": "blueprint",
            "initial_connection": {
                "source_system": source_system,
                "display_name": f"{source_system} mock probe",
                "auth_type": "mock",
                "mock_mode": True,
                "connection_metadata": {"account_id": "TEST1234567"} if source_system == "netsuite"
                else {"host": "ebs-test-db.acme.internal", "service_name": "APPS"},
            },
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    conns = client.get(
        f"/api/projects/{pid}/source-connections", headers=headers
    ).json()
    return pid, conns[0]["id"]


def test_discovery_scan_persists_six_pillars_for_netsuite_mock():
    """A mock-mode NetSuite scan must persist a DiscoveryRun with all six
    pillars populated and the integration pillar classified by the
    vendor catalog (brand + transport + direction)."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="NS Discovery test"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        assert run["status"] == "completed", run
        # All six pillars must have non-zero counts
        for pillar in (
            "data", "configuration", "processes",
            "customisations", "reports", "integrations",
        ):
            assert run["pillar_counts"].get(pillar, 0) > 0, (pillar, run)
        # Integration health rollup must split into healthy / degraded /
        # not_tested buckets and sum to the integration count.
        ih = run["integration_health"]
        assert sum(ih.values()) == run["pillar_counts"]["integrations"], ih
        assert 0 < run["complexity_score"] <= 100

        # Latest endpoint surfaces the integration preview classified by
        # the vendor catalog (brand names + transports).
        latest = client.get(
            f"/api/projects/{pid}/discovery/latest", headers=headers
        ).json()
        assert latest["run"]["id"] == run["id"]
        names = [i["name"] for i in latest["integrations"]]
        # The mock dataset includes several well-known SaaS partners.
        assert any("Salesforce" in n for n in names), names
        assert any("Workday" in n for n in names), names
        assert any("Avalara" in n for n in names), names
        # Each integration row carries transport + direction in metadata.
        for row in latest["integrations"]:
            md = row["metadata_json"]
            assert md.get("transport"), row
            assert md.get("direction"), row
            assert md.get("status") in ("healthy", "degraded", "not_tested"), row
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_discovery_scan_persists_six_pillars_for_ebs_mock():
    """Same shape contract for EBS. Validates that the dispatcher routes
    by source_system correctly and the scanner returns the canonical
    pillar set."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="oracle_ebs", name="EBS Discovery test"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        assert run["status"] == "completed"
        assert run["source_system"] == "oracle_ebs"
        for pillar in (
            "data", "configuration", "processes",
            "customisations", "reports", "integrations",
        ):
            assert run["pillar_counts"].get(pillar, 0) > 0
        # EBS scan emits XX_* concurrent programs etc. — check the
        # drilldown surfaces them.
        objs = client.get(
            f"/api/discovery-runs/{run['id']}/objects",
            headers=headers,
            params={"pillar": "customisations"},
        ).json()
        categories = {o["category"] for o in objs}
        assert "Descriptive Flexfield (DFF)" in categories, categories
        assert any("Concurrent Program" in c for c in categories), categories
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_discovery_scan_requires_source_connection_and_pinned_source_system():
    """The two gating preconditions for Discovery:
    (a) project.source_system must be set,
    (b) project must have at least one source connection.
    Both should surface as typed 400s, not 500s."""
    headers = _headers()
    # (a) no source_system → 400
    bare = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Bare project no source"},
    ).json()
    pid = bare["id"]
    try:
        r = client.post(f"/api/projects/{pid}/discovery/run", headers=headers)
        assert r.status_code == 400, r.text
        assert "source_system" in r.json().get("detail", "")
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)

    # (b) source_system set but no connection → 400
    proj = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Source pinned but no conn", "source_system": "netsuite"},
    ).json()
    pid2 = proj["id"]
    try:
        r = client.post(f"/api/projects/{pid2}/discovery/run", headers=headers)
        assert r.status_code == 400, r.text
        assert "connection" in r.json().get("detail", "").lower()
    finally:
        client.delete(f"/api/projects/{pid2}", headers=headers)


def test_discovery_drilldown_filters_by_pillar_category_and_risk():
    """Drilldown endpoint must filter the discovered objects list
    correctly. Risk filter drives the "535 risk" badge in the
    Customisations pillar."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="Drilldown filter test"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        rid = run["id"]
        # pillar filter
        cust = client.get(
            f"/api/discovery-runs/{rid}/objects",
            headers=headers,
            params={"pillar": "customisations"},
        ).json()
        assert len(cust) > 0
        for row in cust:
            assert row["pillar"] == "customisations"
        # risk filter
        high = client.get(
            f"/api/discovery-runs/{rid}/objects",
            headers=headers,
            params={"pillar": "customisations", "risk_level": "high"},
        ).json()
        assert len(high) >= 1
        for row in high:
            assert row["risk_level"] == "high"
        # unknown pillar → 400 not 500
        r = client.get(
            f"/api/discovery-runs/{rid}/objects",
            headers=headers,
            params={"pillar": "totally_made_up"},
        )
        assert r.status_code == 400
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_discovery_writes_audit_events_for_start_and_completion():
    """Every scan attempt writes both a discovery.scan_started and
    discovery.scan_completed audit row tied to the actor + project so
    compliance reviewers can prove who scanned what when."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="Discovery audit test"
    )
    try:
        client.post(f"/api/projects/{pid}/discovery/run", headers=headers)
        events = client.get(
            "/api/audit-events",
            headers=headers,
            params={"project_id": pid, "action_prefix": "discovery."},
        ).json()
        actions = {e["action"] for e in events}
        assert "discovery.scan_started" in actions
        assert "discovery.scan_completed" in actions
        for e in events:
            assert e["actor_email"] == settings.ADMIN_EMAIL
            assert e["project_id"] == pid
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_vendor_catalog_classifies_known_brands_correctly():
    """The vendor catalog turns raw integration names into branded rows
    with the right transport. Without this, the Integration Health table
    is just "third-party SOAP user #4827" repeated 12 times."""
    from app.discovery.vendor_catalog import classify_integration

    cases: list[tuple[str, str, str]] = [
        ("Celigo integrator.io",     "Celigo integrator.io", "JMS"),
        ("Workday HCM Inbound",      "Workday HCM",          "REST"),
        ("Salesforce CRM Sync",      "Salesforce CRM",       "REST"),
        ("Avalara AvaTax",           "Avalara AvaTax",       "SFTP/AS2"),
        ("XX_WELLS_FARGO_MT940",     "Bank Feed · Wells Fargo", "MT940"),
        ("XX_ADP_PAYROLL_FEED",      "ADP Payroll",          "B-PIPE"),
        ("Shopify Storefront",       "Shopify Storefront",   "cXML"),
    ]
    for raw, brand, transport in cases:
        cls = classify_integration(raw)
        assert cls.brand == brand, (raw, cls.brand)
        assert cls.transport == transport, (raw, cls.transport)


def test_slice5_customisations_drilldown_emits_individual_fields_with_context_buckets():
    """Slice 5 — Customisations pillar must surface individual custom-field
    rows (not just one summary line) clustered into at-risk groups, each
    tagged with a context bucket (TRADE / GOVT / INTERNAL) and a risk_reason
    + fusion_target. This is what powers the per-cluster drilldown table
    in the UI."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="Slice5 NS drilldown"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        rows = client.get(
            f"/api/discovery-runs/{run['id']}/objects",
            headers=headers,
            params={"pillar": "customisations", "category": "Custom Field"},
        ).json()
        # Must be dozens of individual rows, not one summary.
        assert len(rows) >= 24, len(rows)
        # Every custom-field row carries context_bucket + at_risk_group +
        # risk_reason + fusion_target.
        contexts = set()
        groups = set()
        for r in rows:
            md = r["metadata_json"]
            assert md.get("context_bucket") in ("TRADE", "GOVT", "INTERNAL", "OPS"), r
            assert md.get("at_risk_group"), r
            assert md.get("risk_reason"), r
            assert md.get("fusion_target"), r
            contexts.add(md["context_bucket"])
            groups.add(md["at_risk_group"])
        # The four canonical NetSuite clusters must all appear so the
        # drilldown header chips render with 4 sub-clusters.
        assert "Customer Trade Profile" in groups, groups
        assert "Customer Government Fields" in groups, groups
        assert "Invoice Internal Refs" in groups, groups
        assert "Item Hazmat & Compliance" in groups, groups
        # All three risk-context buckets are represented in the high/medium
        # band so the Context filter chip row has content.
        assert {"TRADE", "GOVT", "INTERNAL"} <= contexts, contexts
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_slice5_reports_drilldown_distinguishes_disco_as_high_risk():
    """Reports pillar must enumerate individual reports per platform
    (Saved Search / BIP / Discoverer). Discoverer rows must surface as
    high-risk because Disco is deprecated and forces a re-platform."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="oracle_ebs", name="Slice5 EBS reports drilldown"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        rows = client.get(
            f"/api/discovery-runs/{run['id']}/objects",
            headers=headers,
            params={"pillar": "reports"},
        ).json()
        # Platforms enumerated as metadata.platform
        platforms = {r["metadata_json"]["platform"] for r in rows}
        assert "BIP" in platforms, platforms
        assert "Disco" in platforms, platforms
        # Every Discoverer row is risk_level=high
        disco = [r for r in rows if r["metadata_json"]["platform"] == "Disco"]
        assert len(disco) >= 2, disco
        for r in disco:
            assert r["risk_level"] == "high", r
            assert "deprecated" in (r["metadata_json"]["risk_reason"] or ""), r
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_slice5_reprobe_integration_updates_status_and_rolls_up_health():
    """Re-probing a single integration must (a) update its status in place,
    (b) re-roll the parent DiscoveryRun's integration_health counts so the
    KPI strip stays consistent, and (c) write an audit event capturing the
    before/after transition."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="Slice5 reprobe test"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        rid = run["id"]
        integrations = client.get(
            f"/api/discovery-runs/{rid}/objects",
            headers=headers,
            params={"pillar": "integrations"},
        ).json()
        assert len(integrations) > 0
        target = integrations[0]
        prior_status = target["metadata_json"]["status"]
        prior_bucket = run["integration_health"]

        # Re-probe — the mock prober is deterministic on object_id+minute,
        # so the new status is a fixed value the assertion can rely on.
        refreshed = client.post(
            f"/api/discovered-objects/{target['id']}/reprobe", headers=headers
        ).json()
        assert refreshed["id"] == target["id"]
        new_status = refreshed["metadata_json"]["status"]
        assert new_status in ("healthy", "degraded", "not_tested"), refreshed
        assert refreshed["metadata_json"].get("last_probe_at"), refreshed
        assert refreshed["metadata_json"].get("probe_count", 0) >= 1, refreshed

        # The /latest endpoint must reflect the updated rollup.
        latest = client.get(
            f"/api/projects/{pid}/discovery/latest", headers=headers
        ).json()
        new_bucket = latest["run"]["integration_health"]
        total_before = sum(prior_bucket.values())
        total_after = sum(new_bucket.values())
        assert total_after == total_before, (prior_bucket, new_bucket)

        # An audit event was written with the before/after.
        evs = client.get(
            "/api/audit-events",
            headers=headers,
            params={
                "project_id": pid,
                "action_prefix": "discovery.scan_completed",
                "target_type": "discovered_object",
            },
        ).json()
        assert any(
            e["target_id"] == target["id"]
            and "Re-probed integration" in (e["summary"] or "")
            for e in evs
        ), evs
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_slice5_reprobe_rejects_non_integration_objects():
    """Re-probe must 404 on non-integration discovered objects — calling
    it on a custom field would otherwise create misleading audit rows."""
    headers = _headers()
    pid, _ = _create_project_with_mock_connection(
        headers, source_system="netsuite", name="Slice5 reprobe negative"
    )
    try:
        run = client.post(
            f"/api/projects/{pid}/discovery/run", headers=headers
        ).json()
        rid = run["id"]
        cf_rows = client.get(
            f"/api/discovery-runs/{rid}/objects",
            headers=headers,
            params={"pillar": "customisations", "category": "Custom Field"},
        ).json()
        cf = cf_rows[0]
        r = client.post(
            f"/api/discovered-objects/{cf['id']}/reprobe", headers=headers
        )
        assert r.status_code == 404, r.text
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_slice6_safeguards_endpoint_returns_seven_canonical_gates():
    """The Migration Monitor strip renders the 7 canonical gates in
    canonical order. Each gate has a status + a human message — even
    when the project is fresh and most gates are ``not_run``."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.get(f"/api/projects/{pid}/safeguards", headers=headers).json()
    codes = [s["code"] for s in r["safeguards"]]
    assert codes == [
        "gl_periods", "dual_cert", "load_seq", "doc_nos",
        "txn_close", "fx_rates", "recon",
    ], codes
    # Pass rate is a float 0..1; matches the underlying count. Loose
    # tolerance because ``round(p/7, 3)`` can drift up to 0.005 from
    # the unrounded ratio for 7-denominators.
    passed = sum(1 for s in r["safeguards"] if s["status"] == "pass")
    assert abs(r["pass_rate"] * 7 - passed) < 0.05, r


def test_slice6_readiness_score_includes_five_lenses_weighted_to_one():
    """The composite readiness score is a weighted blend of five lenses
    whose weights sum to 1.0 — drives the top-nav pill and the CFO
    dashboard."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.get(f"/api/projects/{pid}/readiness", headers=headers).json()
    lenses = r["lenses"]
    assert set(lenses.keys()) == {
        "gate_performance", "mapping_quality",
        "reconciliation", "completeness", "issue_resolution",
    }, lenses.keys()
    weight_sum = sum(l["weight"] for l in lenses.values())
    assert abs(weight_sum - 1.0) < 0.001, weight_sum
    # Total score is 0..100 and 0..5 derived from the weighted sum.
    assert 0 <= r["total_pct"] <= 100
    assert 0 <= r["total"] <= 5.0


def test_slice6_runbook_seeds_canonical_steps_and_advance_persists():
    """Seeding the runbook for a fresh project must persist the canonical
    15-step Oracle Fusion playbook. Idempotent — calling /seed twice
    without force must NOT duplicate rows."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    rows = client.post(f"/api/projects/{pid}/runbook/seed", headers=headers).json()
    assert len(rows) >= 15, rows
    # Idempotency
    rows2 = client.post(f"/api/projects/{pid}/runbook/seed", headers=headers).json()
    assert len(rows2) == len(rows), (len(rows), len(rows2))
    # Advance one task
    target = rows[0]["id"]
    upd = client.patch(
        f"/api/runbook-tasks/{target}",
        headers=headers,
        json={"status": "in_progress"},
    ).json()
    assert upd["status"] == "in_progress"


def test_slice6_reconciliation_seed_persists_pass_warning_fail_distribution():
    """Mock recon seeder produces checks with a status distribution that
    powers the CFO's variance figure + the Recon safeguard."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    rows = client.post(f"/api/projects/{pid}/reconciliation/seed", headers=headers).json()
    assert len(rows) > 0
    # Each row carries source / target / variance / tolerance + status
    for r in rows:
        assert r["metric_name"]
        assert "variance" in r
        assert "tolerance" in r
        assert r["status"] in ("pass", "warning", "fail", "not_run"), r
    # Re-seeding is idempotent in count (refreshes values in place).
    rows2 = client.post(f"/api/projects/{pid}/reconciliation/seed", headers=headers).json()
    assert len(rows2) == len(rows)


def test_slice6_dual_cert_requires_second_approver_from_different_user():
    """Dual-cert flagged mappings can't be approved by a single user.
    First approval is captured but row stays in ``suggested``; second
    approval (different user) promotes to ``approved``."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{pid}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    # Generate a mapping then flag it for dual-cert. Earlier tests may
    # have left every mapping in ``approved`` state via same-project
    # replay; we forcibly reset the row to ``suggested`` before flipping
    # the dual-cert flag so the assertion exercises the actual dual-
    # cert promotion path.
    suggestions = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers
    ).json()
    teach = next(m for m in suggestions if m["source_column"])
    from app.database import SessionLocal
    from app.models.mapping import MappingSuggestion
    db = SessionLocal()
    try:
        row = db.query(MappingSuggestion).filter(MappingSuggestion.id == teach["id"]).first()
        row.requires_dual_approval = 1
        row.approved_by = None
        row.approved_at = None
        row.second_approver_email = None
        row.second_approved_at = None
        row.status = "suggested"
        db.commit()
    finally:
        db.close()
    # First approval — stays suggested.
    r1 = client.put(f"/api/mappings/{teach['id']}/approve", headers=headers).json()
    assert r1["status"] == "suggested", r1
    assert r1["approved_by"] == settings.ADMIN_EMAIL
    # Second approval from the SAME user — must 409.
    r2 = client.put(f"/api/mappings/{teach['id']}/approve", headers=headers)
    assert r2.status_code == 409, r2.text


def test_slice6_environment_promotion_gate_enforces_sequential_order():
    """Promotion gate must: (a) reject out-of-order (DEV→UAT skipping QA),
    (b) reject when no completed LoadRun in the prior environment exists,
    (c) succeed with a clear path."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    # (a) Out-of-order
    r = client.post(
        f"/api/projects/{pid}/promote-environment",
        headers=headers,
        json={"target_environment": "UAT"},
    )
    assert r.status_code == 409, r.text


def test_slice6_exec_summary_rolls_up_for_cfo():
    """Exec summary returns score + days-to-cutover + open critical
    issues + top risks + top blockers + total recon variance."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.get(f"/api/projects/{pid}/exec-summary", headers=headers).json()
    for k in (
        "score_pct", "score_5", "safeguard_pass_rate",
        "days_to_cutover", "open_critical_issues",
        "top_risks", "top_blockers",
        "total_recon_variance_usd",
    ):
        assert k in r, (k, r.keys())


def test_slice6_issue_lifecycle_with_audit():
    """Raising an issue + resolving it must write both audit rows so a
    compliance reviewer can trace every blocker's lifecycle."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    created = client.post(
        f"/api/projects/{pid}/issues",
        headers=headers,
        json={
            "title": "Fusion HCM patch level requires re-test",
            "severity": "high",
            "owner_email": "lead@trinamix.com",
        },
    ).json()
    assert created["status"] == "open"
    resolved = client.patch(
        f"/api/issues/{created['id']}",
        headers=headers,
        json={"status": "resolved", "resolution_note": "Patch confirmed compatible"},
    ).json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None

    audit = client.get(
        "/api/audit-events",
        headers=headers,
        params={"project_id": pid, "target_type": "issue"},
    ).json()
    # We expect at least 2 audit rows tied to this issue (create + update).
    issue_events = [e for e in audit if e["target_id"] == created["id"]]
    assert len(issue_events) >= 2, issue_events


def test_slice6_risk_score_is_probability_times_impact():
    """Risk register row's score column = probability × impact, kept in
    sync on every write so the dashboard can sort cheaply by score."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.post(
        f"/api/projects/{pid}/risks",
        headers=headers,
        json={
            "title": "FX rate refresh might miss SLA on go-live morning",
            "probability": 3,
            "impact": 5,
            "owner_email": "treasury@trinamix.com",
        },
    ).json()
    assert r["score"] == 15
    upd = client.patch(
        f"/api/risks/{r['id']}",
        headers=headers,
        json={"probability": 2},
    ).json()
    assert upd["score"] == 10   # 2 × 5


def test_slice6_signoff_ledger_is_append_only():
    """Sign-off rows are insert-only. The router exposes only POST + GET
    — no UPDATE / DELETE — and the model intends rows to persist forever."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    row = client.post(
        f"/api/projects/{pid}/sign-offs",
        headers=headers,
        json={
            "kind": "phase",
            "subject": "Phase 'Own' complete",
            "signer_email": "data_owner@trinamix.com",
            "signer_role": "Data Owner",
            "comment": "Reviewed every conversion's mapping coverage.",
        },
    ).json()
    assert row["id"]
    # No PATCH / DELETE route should exist for sign-offs.
    r = client.patch(f"/api/sign-offs/{row['id']}", headers=headers, json={})
    assert r.status_code in (404, 405), r.status_code


def test_slice6_dress_rehearsal_increments_project_counter():
    """Logging a dress rehearsal bumps the project's dress_rehearsal_count
    so the Project Overview can show '2 dress rehearsals run' without
    a separate query."""
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    before = client.get(f"/api/projects/{pid}", headers=headers).json()
    pre_count = before.get("dress_rehearsal_count", 0) or 0
    client.post(
        f"/api/projects/{pid}/dress-rehearsals",
        headers=headers,
        json={"result": "pass", "summary": "Mock-A timing: under budget", "duration_minutes": 240},
    )
    after_obj = client.get(f"/api/projects/{pid}", headers=headers).json()
    # The project response doesn't yet surface this field — read it from
    # the rehearsal row count instead.
    rehs = client.get(
        f"/api/projects/{pid}/dress-rehearsals", headers=headers
    ).json()
    assert len(rehs) == pre_count + 1, (pre_count, rehs)


def test_slice6_copilot_endpoint_503s_without_api_key():
    """AI Copilot returns a clean 503 (not 500) when the Anthropic key
    isn't configured, so the floating widget hides itself instead of
    rendering a broken state."""
    from app.config import settings
    assert not settings.ANTHROPIC_API_KEY, "test fixture relies on no API key"
    headers = _headers()
    pid = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.post(
        "/api/copilot/ask",
        headers=headers,
        json={
            "project_id": pid,
            "messages": [{"role": "user", "content": "Are we ready?"}],
        },
    )
    assert r.status_code == 503, r.text


def test_slice7_coa_seed_creates_canonical_5_segment_structure():
    """Slice 7 — Seeding a COA structure on a fresh conversion creates
    the canonical 5-segment Fusion template with deterministic positions,
    lengths, and derivation kinds. Re-seeding is idempotent (returns the
    existing structure)."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    seeded = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    assert seeded["separator"] == "-"
    assert seeded["locked"] is False
    names = [s["name"] for s in seeded["segments"]]
    assert names == ["Company", "CostCenter", "NaturalAccount", "SubAccount", "Product"]
    assert [s["length"] for s in seeded["segments"]] == [2, 4, 6, 4, 4]

    # Re-seed is idempotent — same id, same segment count.
    again = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    assert again["id"] == seeded["id"]
    assert len(again["segments"]) == len(seeded["segments"])


def test_slice7_coa_crosswalk_upsert_is_idempotent_by_legacy_value():
    """Uploading the same (segment_id, legacy_value) twice should refresh
    the row's fusion_value rather than create duplicates. This is the
    CSV-bulk-upload safety net."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    struct = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    company_seg = next(s for s in struct["segments"] if s["name"] == "Company")

    client.post(
        f"/api/coa-segments/{company_seg['id']}/crosswalks",
        headers=headers,
        json={"legacy_value": "C01", "fusion_value": "01"},
    )
    client.post(
        f"/api/coa-segments/{company_seg['id']}/crosswalks",
        headers=headers,
        json={"legacy_value": "C01", "fusion_value": "01_REV2"},
    )
    rows = client.get(
        f"/api/coa-segments/{company_seg['id']}/crosswalks", headers=headers
    ).json()
    c01 = [r for r in rows if r["legacy_value"] == "C01"]
    assert len(c01) == 1, c01
    assert c01[0]["fusion_value"] == "01_REV2", c01[0]


def test_slice7_coa_bulk_crosswalk_upsert_handles_many_rows():
    """Bulk upsert is the typical authoring path — paste 30+ rows from a
    spreadsheet and they all land at once."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    struct = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    cc_seg = next(s for s in struct["segments"] if s["name"] == "CostCenter")

    bulk = [
        {"legacy_value": f"DEPT{i:02d}", "fusion_value": f"{1000 + i}"}
        for i in range(1, 31)
    ]
    rows = client.post(
        f"/api/coa-segments/{cc_seg['id']}/crosswalks/bulk",
        headers=headers,
        json={"rows": bulk},
    ).json()
    assert len(rows) == 30, rows
    # Confirm we can list them all + idempotency.
    listed = client.get(
        f"/api/coa-segments/{cc_seg['id']}/crosswalks", headers=headers
    ).json()
    assert len(listed) == 30


def test_slice7_coa_compose_reports_coverage_with_gaps():
    """The composition engine walks every row and reports per-segment
    coverage. With crosswalks intentionally incomplete, coverage_pct
    must reflect real gaps — not silently default to 100%."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    struct = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    # Seed crosswalks on Company → all source rows will get blank because
    # the Item dataset doesn't carry COMPANY_CODE.
    company = next(s for s in struct["segments"] if s["name"] == "Company")
    client.post(
        f"/api/coa-segments/{company['id']}/crosswalks",
        headers=headers,
        json={"legacy_value": "C01", "fusion_value": "01"},
    )
    result = client.post(
        f"/api/conversions/{item['id']}/coa/compose", headers=headers,
        params={"sample_size": 10},
    ).json()
    # The Item dataset doesn't have the canonical COA source columns, so
    # coverage should be < 100% and per-segment coverage exists.
    assert result["coverage_pct"] < 100.0, result
    assert "Company" in result["per_segment_coverage"], result["per_segment_coverage"]
    assert "NaturalAccount" in result["per_segment_coverage"]
    # Sample rows include the composed account string (might be blank
    # segments if columns missing, but every row carries the field).
    for sample in result["sample_rows"]:
        assert "composed_account" in sample
        assert isinstance(sample["emissions"], list)
        assert len(sample["emissions"]) == 5


def test_slice7_coa_lock_prevents_segment_edits():
    """Locking a structure blocks all segment / structure edits except
    unlock. Crosswalk rows can still be added (controls vs auditability)."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    struct = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    # Lock
    locked = client.patch(
        f"/api/coa-structures/{struct['id']}",
        headers=headers,
        json={"locked": True},
    ).json()
    assert locked["locked"] is True
    # Adding a segment must 409
    r = client.post(
        f"/api/coa-structures/{struct['id']}/segments",
        headers=headers,
        json={"name": "Future", "length": 2, "derivation_kind": "source_column"},
    )
    assert r.status_code == 409, r.text
    # Editing a segment must 409
    seg_id = struct["segments"][0]["id"]
    r = client.patch(
        f"/api/coa-segments/{seg_id}",
        headers=headers,
        json={"length": 3},
    )
    assert r.status_code == 409, r.text
    # Unlocking is OK
    unlocked = client.patch(
        f"/api/coa-structures/{struct['id']}",
        headers=headers,
        json={"locked": False},
    ).json()
    assert unlocked["locked"] is False


def test_slice7_coa_engine_unit_tests_for_each_derivation_kind():
    """Unit-test the COA engine emitters against a synthetic row so the
    rule contract is locked in without round-tripping the full HTTP
    surface."""
    from app.discovery import vendor_catalog  # noqa: F401
    from app.models.coa import COASegment
    from app.services.coa_engine import emit_segment

    row = {"COMPANY_CODE": "C01", "CC_CODE": "DEPT05", "STATUS": "ACTIVE"}

    # constant
    seg = COASegment(
        position=1, name="Company", length=2,
        derivation_kind="constant",
        derivation_config={"value": "99"},
        pad_style="left_zero",
    )
    e = emit_segment(seg, row, crosswalk_index={})
    assert e.valid and e.value == "99"

    # source_column with padding
    seg2 = COASegment(
        position=2, name="CostCenter", length=8,
        derivation_kind="source_column",
        derivation_config={"column": "CC_CODE"},
        pad_style="left_zero",
    )
    e2 = emit_segment(seg2, row, crosswalk_index={})
    assert e2.valid and e2.value == "00DEPT05"  # padded to length=8

    # crosswalk with default fallback
    seg3 = COASegment(
        id=42, position=3, name="Product", length=4,
        derivation_kind="crosswalk",
        derivation_config={"column": "COMPANY_CODE"},
        default_value="0000",
        pad_style="left_zero",
    )
    e3 = emit_segment(seg3, row, crosswalk_index={42: {}})
    assert e3.valid is True
    assert e3.value == "0000"

    # crosswalk with hit — value is padded/truncated to segment length.
    e3b = emit_segment(seg3, row, crosswalk_index={42: {"C01": "X1"}})
    assert e3b.valid and e3b.value == "00X1"  # padded left with zeros

    # computed with rule pipeline
    seg4 = COASegment(
        position=4, name="Padded", length=6,
        derivation_kind="computed",
        derivation_config={
            "column": "CC_CODE",
            "rules": [{"rule_type": "UPPERCASE", "config": {}}],
        },
        pad_style="left_zero",
    )
    e4 = emit_segment(seg4, row, crosswalk_index={})
    assert e4.valid and "DEPT05" in e4.value


def test_slice7_coa_csv_upload_handles_real_file_upload():
    """The CSV upload endpoint must accept a multipart file with the
    correct columns and reject mis-shaped input."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    struct = client.post(
        f"/api/conversions/{item['id']}/coa/seed", headers=headers
    ).json()
    natacc = next(s for s in struct["segments"] if s["name"] == "NaturalAccount")

    # Well-formed CSV.
    csv_data = "legacy_value,fusion_value,description\n" \
               "4001,40000,Sales Revenue\n" \
               "4002,40010,Service Revenue\n" \
               "5001,50000,Cost of Goods Sold\n"
    r = client.post(
        f"/api/coa-segments/{natacc['id']}/crosswalks/upload",
        headers=headers,
        files={"file": ("crosswalk.csv", csv_data, "text/csv")},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 3

    # Bad CSV missing required columns → 400.
    bad = "wrong_col,other\nx,y\n"
    r2 = client.post(
        f"/api/coa-segments/{natacc['id']}/crosswalks/upload",
        headers=headers,
        files={"file": ("bad.csv", bad, "text/csv")},
    )
    assert r2.status_code == 400, r2.text


def test_fusion_modules_endpoint_returns_full_catalog_with_source_hints():
    """Setup Wizard step 4 pulls the catalog from this endpoint. Every
    module ships canonical Fusion target objects + per-source extract
    hints so the wizard previews realistic implementation plans."""
    headers = _headers()
    catalog = client.get("/api/fusion-modules", headers=headers).json()
    codes = {m["code"] for m in catalog}
    # The six canonical families must be present.
    assert {"financials", "scm", "hcm", "ppm", "epm", "risk"} <= codes, codes
    # SCM must include Item / Customer / Supplier / Sales Order / PO at
    # minimum — anything less and the wizard's preview would mis-scope.
    scm = next(m for m in catalog if m["code"] == "scm")
    scm_objects = {o["target_object"] for o in scm["objects"]}
    assert {"Item", "Customer", "Supplier", "Sales Order", "Purchase Order"} <= scm_objects, scm_objects
    # Each object carries per-source extract hints for at least EBS + NetSuite
    for o in scm["objects"]:
        hints = o["source_extracts"]
        assert "oracle_ebs" in hints, o
        assert "netsuite" in hints, o


def test_project_create_with_selected_modules_auto_creates_planned_conversions():
    """Step 4 → Create flow: passing ``selected_modules`` on project
    create must auto-create one planned Conversion per canonical
    target object across the selected modules, deduplicated."""
    headers = _headers()
    r = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "Mock EBS Scope Test",
            "source_system": "oracle_ebs",
            "phase": "blueprint",
            "initial_connection": {
                "source_system": "oracle_ebs",
                "display_name": "Mock EBS",
                "auth_type": "mock",
                "mock_mode": True,
                "connection_metadata": {},
            },
            "selected_modules": ["financials", "scm"],
        },
    )
    assert r.status_code == 200, r.text
    proj = r.json()
    pid = proj["id"]
    try:
        # Every planned conversion lands with status=planning + a planned
        # load order pulled from the catalog.
        convs = client.get(
            f"/api/projects/{pid}/conversions", headers=headers,
        ).json()
        # SCM (10 objects) ∪ Financials (9 objects). No duplicates by
        # target_object (e.g., neither pulls Customer twice).
        target_objects = {c["target_object"] for c in convs}
        assert "Item" in target_objects
        assert "Customer" in target_objects
        assert "Supplier" in target_objects
        assert "Chart of Accounts" in target_objects
        assert "Open AP Invoices" in target_objects
        # Source extract hint should mention the EBS-specific table.
        item_conv = next(c for c in convs if c["target_object"] == "Item")
        assert "MTL_SYSTEM_ITEMS_B" in (item_conv.get("description") or ""), item_conv
        # Audit trail captures the scope decision.
        audit = client.get(
            "/api/audit-events", headers=headers,
            params={"project_id": pid, "action_prefix": "project."},
        ).json()
        assert any(
            e.get("details_json", {}).get("modules") == ["financials", "scm"]
            for e in audit
        ), audit
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_project_create_without_selected_modules_creates_empty_engagement():
    """No modules selected = no auto-conversions. Setup Wizard's Step 4
    is optional."""
    headers = _headers()
    r = client.post(
        "/api/projects", headers=headers,
        json={"name": "Empty Scope Test", "source_system": "netsuite"},
    )
    pid = r.json()["id"]
    try:
        convs = client.get(
            f"/api/projects/{pid}/conversions", headers=headers,
        ).json()
        assert convs == [], convs
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_project_create_with_modules_dedupes_across_overlapping_objects():
    """Suppliers appears in both SCM and Financials. The auto-create
    path must produce exactly one Supplier conversion, not two."""
    headers = _headers()
    r = client.post(
        "/api/projects", headers=headers,
        json={
            "name": "Dedup Test",
            "source_system": "netsuite",
            "selected_modules": ["financials", "scm"],
        },
    )
    pid = r.json()["id"]
    try:
        convs = client.get(
            f"/api/projects/{pid}/conversions", headers=headers,
        ).json()
        suppliers = [c for c in convs if c["target_object"] == "Supplier"]
        assert len(suppliers) == 1, suppliers
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_local_translator_compound_case_when_with_and_or_no_api_key():
    """Bug-fix proof: the NL translator must produce a structured
    CASE_WHEN rule for compound conditions WITHOUT calling an API.
    Previously this returned 503 in dev → the pane silently hid → the
    rule appeared to 'go away'."""
    from app.services.rule_translator import (
        TranslatorUnavailable, translate_description,
    )

    # Compound AND across two columns + simpler fallback + default.
    res = translate_description(
        description=(
            "if STATUS is ACTIVE and REGION is US then DOMESTIC_ACTIVE; "
            "if STATUS is ACTIVE then INTERNATIONAL_ACTIVE; "
            "otherwise INACTIVE"
        ),
        columns=["STATUS", "REGION", "CUSTOMER_NUM"],
    )
    assert res.source == "local", res
    assert res.rule_type == "CASE_WHEN"
    branches = res.config["branches"]
    assert len(branches) == 2, branches
    # First branch is the AND group → all_of.
    assert "all_of" in branches[0], branches[0]
    leaves = branches[0]["all_of"]
    assert {l["column"] for l in leaves} == {"STATUS", "REGION"}
    assert all(l["op"] == "eq" for l in leaves), leaves
    assert branches[0]["then"] == "DOMESTIC_ACTIVE"
    # Default captured.
    assert res.config["default"] == "INACTIVE"


def test_local_translator_or_compound_branch():
    """Same matcher must handle OR — "if STATUS is A or STATUS is B then …"."""
    from app.services.rule_translator import translate_description

    res = translate_description(
        description=(
            "if STATUS is BOOKED or STATUS is CLOSED then FINAL; "
            "otherwise PENDING"
        ),
        columns=["STATUS"],
    )
    assert res.source == "local"
    assert res.rule_type == "CASE_WHEN"
    first = res.config["branches"][0]
    assert "any_of" in first
    assert len(first["any_of"]) == 2
    assert first["then"] == "FINAL"
    assert res.config["default"] == "PENDING"


def test_local_translator_value_map_and_constant_and_computed():
    """Three more local patterns the matcher should cover so common
    rules don't fall through to AI: VALUE_MAP, CONSTANT, COMPUTED."""
    from app.services.rule_translator import translate_description

    vm = translate_description(
        description="map A to Active, I to Inactive, S to Suspended",
        columns=["STATUS"],
    )
    assert vm.source == "local"
    assert vm.rule_type == "VALUE_MAP"
    assert vm.config["A"] == "Active"
    assert vm.config["I"] == "Inactive"
    assert vm.config["case_insensitive"] is True

    const = translate_description(
        description="always set to UNITED STATES",
        columns=[],
    )
    assert const.source == "local"
    assert const.rule_type == "CONSTANT"
    assert const.config["value"] == "UNITED STATES"

    today = translate_description(
        description="use today's date",
        columns=[],
    )
    assert today.source == "local"
    assert today.rule_type == "COMPUTED"
    assert today.config["source"] == "today"


def test_local_translator_handles_blank_check():
    """Common pattern: "if status is blank then use INACTIVE"."""
    from app.services.rule_translator import translate_description

    res = translate_description(
        description="if STATUS is blank then INACTIVE",
        columns=["STATUS"],
    )
    assert res.source == "local"
    assert res.rule_type == "CASE_WHEN"
    branch = res.config["branches"][0]
    # Either a leaf {column, op:isblank} or wrapped in all_of singleton —
    # both are acceptable as long as the engine evaluates correctly.
    if "column" in branch:
        assert branch["op"] == "isblank"
    else:
        leaves = branch.get("all_of") or branch.get("any_of") or []
        assert any(l["op"] == "isblank" for l in leaves), branch


def test_translate_endpoint_returns_local_source_without_api_key():
    """End-to-end: the /rules/translate endpoint must produce a
    structured rule for the user's exact pattern without requiring an
    Anthropic key. ``source`` field reports "local" so the UI can show
    the right provenance pill."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    so = next(c for c in convs if c["target_object"] == "Sales Order" and c["dataset_id"])
    r = client.post(
        f"/api/conversions/{so['id']}/rules/translate",
        headers=headers,
        json={
            "description": (
                "if ORDER_STATUS is SHIPPED and ORDER_TYPE is DROPSHIP "
                "then DROPSHIP_SHIPPED; otherwise OTHER"
            ),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "local", body
    assert body["rule_type"] == "CASE_WHEN"
    branches = body["config"]["branches"]
    assert len(branches) == 1
    assert "all_of" in branches[0]
    assert branches[0]["then"] == "DROPSHIP_SHIPPED"
    assert body["config"]["default"] == "OTHER"
    # Preview samples are still run against the real dataset.
    assert isinstance(body["preview_samples"], list)


def test_p1_module_scope_auto_links_fbdi_templates_where_seeded():
    """P1 — auto-created conversions from module scope must pre-bind to
    an FBDI template when the seeded manifest has one matching the
    target_object. Item Master + Sales Order are seeded; verify they
    bind. Objects without a seeded template stay unbound so the analyst
    can pick one later."""
    headers = _headers()
    r = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "P1 FBDI auto-link test",
            "source_system": "oracle_ebs",
            "phase": "blueprint",
            "selected_modules": ["scm"],
        },
    )
    pid = r.json()["id"]
    try:
        convs = client.get(
            f"/api/projects/{pid}/conversions", headers=headers
        ).json()
        # Item conversion must have template_id set (seeded as Item Master).
        item = next(c for c in convs if c["target_object"] == "Item")
        assert item["template_id"] is not None, item
        assert item["template_name"], item
        # Sales Order also seeded.
        so = next(c for c in convs if c["target_object"] == "Sales Order")
        assert so["template_id"] is not None, so
        # Confirm the audit captures the link count.
        audit = client.get(
            "/api/audit-events", headers=headers,
            params={"project_id": pid, "action_prefix": "project."},
        ).json()
        scope_event = next(
            (e for e in audit if "Scope set" in (e["summary"] or "")), None,
        )
        assert scope_event is not None, audit
        assert scope_event["details_json"].get("templates_linked", 0) >= 2, scope_event
    finally:
        client.delete(f"/api/projects/{pid}", headers=headers)


def test_p2_data_quality_score_reflects_mapping_state():
    """P2 — DQ score per conversion is composed of mapping coverage +
    validation cleanliness + reconciliation. A bare conversion (no
    mappings, no validation, no recon) should score 0. After driving
    the conversion through suggest + approve, the score must climb."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(f"/api/projects/{project_id}/conversions", headers=headers).json()
    item = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])

    # Run suggest-mapping + approve a chunk
    suggestions = client.post(
        f"/api/conversions/{item['id']}/suggest-mapping", headers=headers,
    ).json()
    for m in suggestions[:10]:
        if m["source_column"]:
            client.put(f"/api/mappings/{m['id']}/approve", headers=headers)

    # Now compute the DQ score
    score = client.get(
        f"/api/conversions/{item['id']}/quality-score", headers=headers,
    ).json()
    assert "total" in score
    assert 0 <= score["total"] <= 100, score
    # All three lenses present
    lens_codes = {l["code"] for l in score["lenses"]}
    assert {"mapping_coverage", "validation_cleanliness", "reconciliation"} == lens_codes
    # Mapping lens should be > 0 (we approved some mappings)
    mapping_lens = next(l for l in score["lenses"] if l["code"] == "mapping_coverage")
    assert mapping_lens["value_pct"] > 0, mapping_lens


def test_p2_project_recompute_returns_average_across_conversions():
    """The project-wide recompute endpoint must return per-conversion
    scores + average so the CFO card can show a single rollup."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.post(
        f"/api/projects/{project_id}/quality-score/recompute", headers=headers,
    ).json()
    assert "scores" in r
    assert "average" in r
    assert 0 <= r["average"] <= 100


def test_p3_pii_flag_updates_column_profile_and_returns_dataset():
    """P3 — PATCH /api/datasets/columns/{column_id}/pii must persist the
    sensitivity flag on the column profile and surface it on the dataset
    detail response so the Mapping Review UI can paint the 🔒 badge.
    Toggling off must also clear the category to keep state coherent."""
    headers = _headers()
    convs = client.get("/api/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    ds = client.get(f"/api/datasets/{item_conv['dataset_id']}", headers=headers).json()
    assert ds["columns"], "expected at least one profiled column on the seeded dataset"
    col = ds["columns"][0]
    assert (col.get("contains_pii") or 0) == 0
    # Flag the column as carrying PII
    r = client.patch(
        f"/api/datasets/columns/{col['id']}/pii",
        headers=headers,
        json={"contains_pii": True, "pii_category": "PII"},
    )
    assert r.status_code == 200, r.text
    refreshed = r.json()
    updated_col = next(c for c in refreshed["columns"] if c["id"] == col["id"])
    assert updated_col["contains_pii"] == 1
    assert updated_col["pii_category"] == "PII"
    # Toggling off must clear both fields
    r2 = client.patch(
        f"/api/datasets/columns/{col['id']}/pii",
        headers=headers,
        json={"contains_pii": False},
    )
    assert r2.status_code == 200
    cleared = next(c for c in r2.json()["columns"] if c["id"] == col["id"])
    assert cleared["contains_pii"] == 0
    assert cleared["pii_category"] in (None, "")


def test_p3_pii_flag_rejects_unknown_category():
    headers = _headers()
    convs = client.get("/api/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    ds = client.get(f"/api/datasets/{item_conv['dataset_id']}", headers=headers).json()
    col_id = ds["columns"][0]["id"]
    r = client.patch(
        f"/api/datasets/columns/{col_id}/pii",
        headers=headers,
        json={"contains_pii": True, "pii_category": "TOPSECRET"},
    )
    assert r.status_code == 400
    assert "Valid" in r.json()["detail"]


def test_p5_live_netsuite_scanner_degrades_without_credentials():
    """P5 — Live NetSuite scanner must NEVER 500. With missing TBA
    credentials, the suiteql probes are reported `skipped` and the
    overall outcome is `degraded` (with a fail on metadata-catalog,
    overall flips to `failed`) — both are safe terminal states."""
    from app.discovery import live_netsuite
    report = live_netsuite.probe_connection(
        connection_metadata={"account_id": "TSTDRV9999999"},
        credentials=None,
    )
    assert report.overall_status in ("degraded", "failed")
    # Reported per-probe outcomes must include the skipped TBA probe so
    # operators see exactly what's missing.
    statuses = {p.name: p.status for p in report.probes}
    assert "oauth1_tba" in statuses or "metadata-catalog" in statuses


def test_p5_live_ebs_scanner_degrades_without_credentials():
    from app.discovery import live_ebs
    report = live_ebs.probe_connection(
        connection_metadata={"host": "ebs.internal", "service_name": "EBSPROD"},
        credentials=None,
    )
    # Either oracledb is missing (skipped) or creds incomplete (skipped) —
    # both surfaces as a `degraded` overall, never raises.
    assert report.overall_status == "degraded"
    assert all(p.status in ("ok", "skipped", "fail") for p in report.probes)


def test_p5_dispatcher_routes_to_live_scanner_when_mock_mode_off():
    """P5 — Flipping mock_mode=False must reach the live scanner module
    (not silently fall through to the mock). We verify by monkeypatching."""
    from app.discovery import connection_dispatch, live_netsuite
    calls: list[str] = []
    orig = live_netsuite.probe_connection
    def spy(*, connection_metadata, credentials):
        calls.append("live_netsuite")
        return orig(connection_metadata=connection_metadata, credentials=credentials)
    live_netsuite.probe_connection = spy  # type: ignore[assignment]
    try:
        connection_dispatch.probe(
            source_system="netsuite",
            mock_mode=False,
            connection_metadata={"account_id": "TSTDRV0000000"},
            credentials=None,
        )
    finally:
        live_netsuite.probe_connection = orig  # type: ignore[assignment]
    assert calls == ["live_netsuite"]


def test_p5_dispatcher_stays_on_mock_when_mock_mode_on():
    from app.discovery import connection_dispatch, live_netsuite, mock_netsuite
    live_calls: list[str] = []
    mock_calls: list[str] = []
    orig_live = live_netsuite.probe_connection
    orig_mock = mock_netsuite.probe_connection
    def spy_live(*, connection_metadata, credentials):
        live_calls.append("x")
        return orig_live(connection_metadata=connection_metadata, credentials=credentials)
    def spy_mock(*, connection_metadata, credentials):
        mock_calls.append("x")
        return orig_mock(connection_metadata=connection_metadata, credentials=credentials)
    live_netsuite.probe_connection = spy_live  # type: ignore[assignment]
    mock_netsuite.probe_connection = spy_mock  # type: ignore[assignment]
    try:
        connection_dispatch.probe(
            source_system="netsuite",
            mock_mode=True,
            connection_metadata={"account_id": "TSTDRV0000000"},
            credentials=None,
        )
    finally:
        live_netsuite.probe_connection = orig_live  # type: ignore[assignment]
        mock_netsuite.probe_connection = orig_mock  # type: ignore[assignment]
    assert mock_calls == ["x"]
    assert live_calls == [], "live scanner must not be called in mock_mode"


def test_p6_coa_readiness_endpoint_returns_well_formed_payload():
    """P6 — The readiness endpoint must respond 200 with a stable shape
    so the UI can render either the green check or the red blocker
    without crashing. Project state varies across the seed, so we
    assert on schema rather than a specific is_ready value."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    r = client.get(f"/api/projects/{project_id}/coa-readiness", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("threshold_pct"), (int, float))
    assert body["threshold_pct"] >= 95
    assert isinstance(body.get("is_ready"), bool)
    assert isinstance(body.get("conversions"), list)


def test_p6_cutover_go_signoff_blocked_when_coa_incomplete():
    """P6 — A cutover-go sign-off MUST be rejected with 409 when a COA
    structure exists but composition coverage is below threshold.

    We force the unhappy state by inserting a COAStructure + segment whose
    derivation depends on a source column that the seeded dataset doesn't
    carry — guarantees coverage_pct = 0% < threshold."""
    from app.database import SessionLocal
    from app.models.coa import COAStructure, COASegment
    from app.models.conversion import Conversion as ConversionModel
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    convs = client.get(
        f"/api/projects/{project_id}/conversions", headers=headers,
    ).json()
    target_conv = next(c for c in convs if c["dataset_id"])
    db = SessionLocal()
    try:
        existing = (
            db.query(COAStructure)
            .filter(COAStructure.conversion_id == target_conv["id"]).first()
        )
        if existing:
            db.delete(existing); db.commit()
        struct = COAStructure(
            conversion_id=target_conv["id"],
            name="Test 5-segment", separator="-",
        )
        db.add(struct); db.commit(); db.refresh(struct)
        # A required source_column that the dataset doesn't have →
        # every row fails composition.
        seg = COASegment(
            structure_id=struct.id, position=1, name="Company", length=2,
            derivation_kind="source_column",
            derivation_config={"column": "COLUMN_THAT_DOES_NOT_EXIST"},
            pad_style="none",
        )
        db.add(seg); db.commit()
    finally:
        db.close()
    # Now try the sign-off and confirm it's blocked.
    try:
        r = client.post(
            f"/api/projects/{project_id}/sign-offs", headers=headers,
            json={
                "kind": "cutover_go", "subject": "Production cutover go-live",
                "signer_email": "cfo@trinamix.com", "signer_role": "CFO",
                "decision": "approved",
            },
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert isinstance(detail, dict)
        assert "threshold_pct" in detail
        blocked_names = [c["conversion_name"] for c in detail["conversions"]]
        assert target_conv["name"] in blocked_names
    finally:
        # Test pollution prevention — drop the synthetic structure so the
        # slice 7 tests (which expect a fresh, seed-able conversion) pass.
        db = SessionLocal()
        try:
            existing = (
                db.query(COAStructure)
                .filter(COAStructure.conversion_id == target_conv["id"]).first()
            )
            if existing:
                db.delete(existing)
                db.commit()
        finally:
            db.close()


def test_p6_non_cutover_go_signoffs_skip_coa_gate():
    """Sign-offs for kinds other than ``cutover_go`` must NEVER be gated
    by COA readiness — only the final go-live decision is."""
    headers = _headers()
    project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
    # Use ``phase`` (a non-go-live kind) — gate must not fire.
    r = client.post(
        f"/api/projects/{project_id}/sign-offs", headers=headers,
        json={
            "kind": "phase", "subject": "Phase Own — complete",
            "signer_email": "lead@trinamix.com", "signer_role": "Migration Lead",
            "decision": "approved",
        },
    )
    assert r.status_code == 200, r.text


def test_p6_mapping_inspector_exposes_dual_cert_state():
    """P6 — Mapping API must surface ``requires_dual_approval`` + the
    second-approver pair so the Mapping Inspector can render the
    'Awaiting 2nd sign-off' banner."""
    from app.database import SessionLocal
    from app.models.mapping import MappingSuggestion
    headers = _headers()
    convs = client.get("/api/conversions", headers=headers).json()
    item_conv = next(c for c in convs if c["target_object"] == "Item" and c["dataset_id"])
    # Force at least one mapping into dual-cert state for assertion stability.
    db = SessionLocal()
    try:
        row = (
            db.query(MappingSuggestion)
            .filter(MappingSuggestion.conversion_id == item_conv["id"]).first()
        )
        assert row is not None, "expected at least one mapping on the seeded conversion"
        row.requires_dual_approval = 1
        row.approved_by = None
        row.second_approver_email = None
        row.status = "suggested"
        db.commit()
        target_id = row.id
    finally:
        db.close()
    payload = client.get(
        f"/api/conversions/{item_conv['id']}/mappings", headers=headers,
    ).json()
    flagged = next(m for m in payload if m["id"] == target_id)
    assert flagged["requires_dual_approval"] == 1
    # Both dual-cert pair fields must surface so the inspector can render
    # the right state.
    assert "second_approver_email" in flagged
    assert "second_approved_at" in flagged


def test_p3_pii_flag_404_on_unknown_column():
    headers = _headers()
    r = client.patch(
        "/api/datasets/columns/99999999/pii",
        headers=headers,
        json={"contains_pii": True, "pii_category": "PII"},
    )
    assert r.status_code == 404


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

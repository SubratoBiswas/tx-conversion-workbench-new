"""Capture and re-apply human-approved mapping decisions across runs.

Two halves of the same loop:

* ``record_learning_from_mapping`` — called whenever a MappingSuggestion lands
  in 'approved' or 'overridden' status. Persists (or refreshes) a
  LearnedMapping row keyed by the business object + target field +
  normalized source column.

* ``apply_learned_to_conversion`` — called after the AI provider returns fresh
  suggestions. For each suggestion still in 'suggested' status, if the
  learning library has a pattern that matches the current dataset's columns,
  the suggestion is mutated to source from the matched column at confidence
  1.0 and auto-approved with ``approved_by='learning-engine'``.

Match scope is ``FBDITemplate.business_object`` — so a learned alias on one
Oracle Item template re-fires on a newer version of the same template.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.conversion import Conversion
from app.models.dataset import DatasetColumnProfile
from app.models.fbdi import FBDIField
from app.models.learned import LearnedMapping
from app.models.mapping import MappingSuggestion
from app.models.transformation import TransformationRule


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


# A "Reference Standard" is a transformation rule taught on a master entity's
# key column that auto-applies to every downstream conversion's FK column
# referencing the same entity. The mapping below identifies (a) which target
# field on a master conversion counts as the canonical key, and (b) which
# field name on a downstream conversion is the inheriting FK. By FBDI
# convention these have the same field name, so one list serves both roles.
# NOTE: this list spans both the *master* key field names (Oracle's "Inventory
# Item Name" on the Item Master FBDI template) and the *downstream FK* names
# (the SO/PO/BOM "InventoryItemNumber" convention). They differ because Oracle
# FBDI uses different field-name conventions across modules. At apply time we
# match by ``business_object`` alone — any field on a downstream conversion
# whose name is in this list inherits any active standard for that object.
REFERENCE_KEY_FIELDS: dict[str, list[str]] = {
    "Item":     ["InventoryItemNumber", "Inventory Item Name", "Item Number", "ItemNumber"],
    "Customer": ["CustomerNumber", "Customer Number"],
    "Supplier": ["SupplierNumber", "Supplier Number"],
    "UOM":      ["UnitOfMeasureCode", "Unit of Measure Code"],
}


def _is_master_key_field(
    target_object: str | None, target_field: str | None
) -> bool:
    if not target_object or not target_field:
        return False
    return target_field in REFERENCE_KEY_FIELDS.get(target_object, [])


def _normalize(name: str | None) -> str:
    """Loose key for column-name comparison: ignores case, spaces, and
    punctuation so 'Item_No', 'ITEM NO', and 'item-no' collapse to the same
    key. Required because legacy extracts rarely keep the same header style.
    """
    if not name:
        return ""
    return _NORMALIZE_RE.sub("", name.lower())


def _business_object_for(conversion: Conversion) -> str | None:
    tpl = conversion.template
    if tpl and tpl.business_object:
        return tpl.business_object
    if conversion.target_object:
        return conversion.target_object
    return tpl.name if tpl else None


def _category_for(rule_type: str | None) -> str:
    if not rule_type:
        return "Column Mapping Alias"
    rt = rule_type.upper()
    if rt == "DATE_FORMAT":
        return "Date Format Rule"
    if rt in ("VALUE_MAP", "CROSSWALK_LOOKUP", "CASE_WHEN", "CONDITIONAL"):
        return "Status Value Mapping"
    if rt in ("CONSTANT", "DEFAULT_VALUE", "COMPUTED", "COALESCE"):
        return "Default & Computed Value"
    if rt in ("ARITHMETIC", "NUMBER_FORMAT"):
        return "Numeric Rule"
    if rt in (
        "UPPERCASE", "LOWERCASE", "TITLE_CASE", "REMOVE_HYPHEN",
        "REMOVE_SPECIAL_CHARS", "TRIM", "PAD", "SUBSTRING",
        "REPLACE", "REGEX_REPLACE", "REGEX_EXTRACT", "CONCAT", "SPLIT",
    ):
        return "Text Format Rule"
    return "Column Mapping Alias"


def _upsert(
    db: Session,
    *,
    kind: str,
    category: str,
    original_value: str,
    resolved_value: str,
    target_object: str,
    target_field: str,
    rule_type: str | None,
    rule_config: dict | None,
    project_id: int | None,
    captured_from: str,
    captured_by: str | None,
    source_system: str | None = None,
) -> LearnedMapping:
    """Idempotent upsert keyed by (kind, source_system, target_object,
    target_field, normalized original_value, rule_type). Re-approving the
    same column or re-confirming the same rule refreshes the row instead of
    duplicating it.

    ``source_system`` is part of the key because the same legacy column
    name can carry a different meaning across source ERPs. Keeping mappings
    isolated by source prevents cross-pollination between unrelated systems.
    """
    src_norm = _normalize(original_value)
    candidates = (
        db.query(LearnedMapping)
        .filter(
            LearnedMapping.kind == kind,
            LearnedMapping.source_system == source_system,
            LearnedMapping.target_object == target_object,
            LearnedMapping.target_field == target_field,
            LearnedMapping.rule_type == rule_type,
        )
        .all()
    )
    matched = next(
        (lm for lm in candidates if _normalize(lm.original_value) == src_norm),
        None,
    )
    if matched:
        matched.resolved_value = resolved_value
        matched.rule_config = rule_config
        matched.category = category
        matched.captured_by = captured_by or matched.captured_by
        matched.captured_from = captured_from
        matched.captured_at = datetime.utcnow()
        # Backfill source_system if older row pre-dates this field.
        if source_system and not matched.source_system:
            matched.source_system = source_system
        db.commit()
        return matched

    item = LearnedMapping(
        kind=kind,
        category=category,
        original_value=original_value,
        resolved_value=resolved_value,
        target_object=target_object,
        target_field=target_field,
        rule_type=rule_type,
        rule_config=rule_config,
        project_id=project_id,
        originated_in_project_id=project_id,
        source_system=source_system,
        captured_from=captured_from,
        captured_by=captured_by,
        confidence_boost=0.26,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def record_learning_from_mapping(
    db: Session,
    mapping: MappingSuggestion,
    conversion: Conversion,
    captured_by: str | None,
) -> LearnedMapping | None:
    """An approval teaches two things at once:

    * the **alias** — legacy column → FBDI field — lands as ``kind=column_mapping``
      and powers auto-replay on future files (Learning Center).
    * the **rule** — any transformation attached to that mapping (REMOVE_HYPHEN,
      DATE_FORMAT, VALUE_MAP, …) — also lands as ``kind=rule`` so it surfaces
      in the Rule Library as a reusable transformation.

    Returns the alias row (the rule row, when written, is independent).
    """
    if not mapping.source_column:
        return None
    business_object = _business_object_for(conversion)
    if not business_object:
        return None

    target_field = None
    if conversion.template:
        for f in conversion.template.fields:
            if f.id == mapping.target_field_id:
                target_field = f.field_name
                break
    if not target_field:
        return None

    rule = mapping.suggested_transformation or {}
    rule_type = rule.get("rule_type") if isinstance(rule, dict) else None
    rule_config = rule.get("config") if isinstance(rule, dict) else None

    project_label = conversion.name or f"Conversion #{conversion.id}"
    captured_from = f"{project_label} — {target_field}"
    # Denormalize the project's source_system onto every learned mapping
    # so cross-project Knowledge Bank lookups don't need a join through
    # Project on every suggest-mapping call.
    source_system = (
        conversion.project.source_system if conversion.project else None
    )

    alias = _upsert(
        db,
        kind="column_mapping",
        category="Column Mapping Alias",
        original_value=mapping.source_column,
        resolved_value=target_field,
        target_object=business_object,
        target_field=target_field,
        rule_type=rule_type,
        rule_config=rule_config,
        project_id=conversion.project_id,
        captured_from=captured_from,
        captured_by=captured_by,
        source_system=source_system,
    )

    if rule_type:
        _upsert(
            db,
            kind="rule",
            category=_category_for(rule_type),
            original_value=mapping.source_column,
            resolved_value=target_field,
            target_object=business_object,
            target_field=target_field,
            rule_type=rule_type,
            rule_config=rule_config,
            project_id=conversion.project_id,
            captured_from=captured_from,
            captured_by=captured_by,
            source_system=source_system,
        )

    # If the rule lands on a master entity's key column (e.g. Item's
    # InventoryItemNumber), also auto-promote it to a Reference Standard so
    # every downstream conversion that references this master inherits the
    # same transformation at output time.
    if rule_type and _is_master_key_field(business_object, target_field):
        record_reference_standard(
            db,
            target_object=business_object,
            target_field=target_field,
            rule_type=rule_type,
            rule_config=rule_config,
            project_id=conversion.project_id,
            captured_from=captured_from,
            captured_by=captured_by,
            source_system=source_system,
        )

    return alias


def record_reference_standard(
    db: Session,
    *,
    target_object: str,
    target_field: str,
    rule_type: str,
    rule_config: dict | None,
    project_id: int | None,
    captured_from: str,
    captured_by: str | None,
    source_system: str | None = None,
) -> LearnedMapping:
    """Persist (or refresh) a reference standard — a transformation rule that
    will auto-prepend on every downstream conversion's FK column referencing
    this master entity. ``source_system`` carries the project's source so
    the cross-source Knowledge Bank doesn't accidentally apply NetSuite
    standards to an EBS conversion (and vice versa)."""
    return _upsert(
        db,
        kind="reference_standard",
        category="Reference Key Standard",
        original_value=target_field,
        resolved_value=target_field,
        target_object=target_object,
        target_field=target_field,
        rule_type=rule_type,
        rule_config=rule_config,
        project_id=project_id,
        captured_from=captured_from,
        captured_by=captured_by,
        source_system=source_system,
    )


def list_reference_standards_for_object(
    db: Session, target_object: str, source_system: str | None = None,
) -> list[LearnedMapping]:
    """All active reference standards for a master entity, optionally
    scoped to a source system so an EBS-taught standard doesn't apply to a
    NetSuite project's FK columns.

    ``source_system`` semantics:

    * ``None`` (legacy / unset): returns every standard for the object —
      used by background code paths that don't know the source.
    * ``"netsuite"`` / etc.: returns standards whose ``source_system``
      matches OR is null (older rows captured before source_system was
      denormalized). Backfill rewrap is a maintenance script; for v1 we
      treat null as "applies anywhere" so old projects don't lose their
      standards mid-migration.
    """
    q = db.query(LearnedMapping).filter(
        LearnedMapping.kind == "reference_standard",
        LearnedMapping.target_object == target_object,
    )
    if source_system:
        from sqlalchemy import or_
        q = q.filter(
            or_(
                LearnedMapping.source_system == source_system,
                LearnedMapping.source_system.is_(None),
            )
        )
    return q.order_by(LearnedMapping.captured_at).all()


def record_learning_from_rule(
    db: Session,
    rule: TransformationRule,
    conversion: Conversion,
    captured_by: str | None,
) -> LearnedMapping | None:
    """Surface a manually-authored transformation rule in the Rule Library.

    A user-written rule is treated as authoritative — it lands as ``kind=rule``
    keyed by the same (business_object, target_field, normalized source
    column, rule_type) as approved-mapping rules, so they unify in one place.
    """
    business_object = _business_object_for(conversion)
    if not business_object:
        return None

    target_field = None
    if conversion.template and rule.target_field_id:
        for f in conversion.template.fields:
            if f.id == rule.target_field_id:
                target_field = f.field_name
                break
    if not target_field:
        # rule has no target field — Rule Library entries need one to be useful
        return None

    src = rule.source_column or ""
    project_label = conversion.name or f"Conversion #{conversion.id}"
    captured_from = f"{project_label} — {target_field} (manual)"
    source_system = (
        conversion.project.source_system if conversion.project else None
    )

    learned = _upsert(
        db,
        kind="rule",
        category=_category_for(rule.rule_type),
        original_value=src,
        resolved_value=target_field,
        target_object=business_object,
        target_field=target_field,
        rule_type=rule.rule_type,
        rule_config=rule.rule_config or {},
        project_id=conversion.project_id,
        captured_from=captured_from,
        captured_by=captured_by,
        source_system=source_system,
    )

    # Manually-authored rules on the master's key column also become
    # Reference Standards. Same auto-prepend mechanic as approved mappings.
    if _is_master_key_field(business_object, target_field):
        record_reference_standard(
            db,
            target_object=business_object,
            target_field=target_field,
            rule_type=rule.rule_type,
            rule_config=rule.rule_config or {},
            project_id=conversion.project_id,
            captured_from=captured_from,
            captured_by=captured_by,
            source_system=source_system,
        )

    return learned


def apply_learned_to_conversion(
    db: Session,
    conversion: Conversion,
    mappings: Iterable[MappingSuggestion],
) -> int:
    """Same-project auto-apply (the "autopilot" path).

    Walks ``mappings`` (suggestions already persisted for this conversion)
    and, for any whose target field has a matching LearnedMapping captured in
    the **same project**, replaces the source column with the learned one at
    confidence 1.0 and marks the row ``approved`` by the ``learning-engine``.

    Cross-project re-use lives in :func:`prepopulate_from_cross_source_kb` —
    that path is scoped by source system, runs at confidence 0.85, and never
    auto-approves (the analyst stays in the loop).
    """
    business_object = _business_object_for(conversion)
    if not business_object:
        return 0
    learned = (
        db.query(LearnedMapping)
        .filter(
            LearnedMapping.kind == "column_mapping",
            LearnedMapping.target_object == business_object,
            # Same-project only — cross-project hits go through the KB path
            # so the analyst sees a "🧠 from KB" badge and can review.
            LearnedMapping.project_id == conversion.project_id,
        )
        .all()
    )
    if not learned:
        return 0

    by_target: dict[str, list[LearnedMapping]] = {}
    for lm in learned:
        if not lm.target_field:
            continue
        by_target.setdefault(lm.target_field, []).append(lm)

    src_index: dict[str, str] = {}
    if conversion.dataset_id:
        cols = (
            db.query(DatasetColumnProfile)
            .filter(DatasetColumnProfile.dataset_id == conversion.dataset_id)
            .all()
        )
        for c in cols:
            src_index[_normalize(c.column_name)] = c.column_name

    fields = {
        f.id: f.field_name
        for f in db.query(FBDIField)
        .filter(FBDIField.template_id == conversion.template_id)
        .all()
    }

    auto_count = 0
    now = datetime.utcnow()
    for m in mappings:
        if m.status != "suggested":
            continue
        tgt_name = fields.get(m.target_field_id)
        if not tgt_name:
            continue
        candidates = by_target.get(tgt_name)
        if not candidates:
            continue
        for lm in candidates:
            actual_src = src_index.get(_normalize(lm.original_value))
            if not actual_src:
                continue
            m.source_column = actual_src
            m.confidence = 1.0
            m.review_required = 0
            m.reason = (
                f"Auto-applied from learning library "
                f"(captured from “{lm.captured_from}”)"
            )
            if lm.rule_type:
                m.suggested_transformation = {
                    "rule_type": lm.rule_type,
                    "config": lm.rule_config or {},
                    "description": "Re-applied from learned rule",
                }
            m.status = "approved"
            m.approved_by = "learning-engine"
            m.approved_at = now
            # Clear any KB provenance — same-project auto-apply is the
            # stronger signal and now owns this suggestion.
            m.kb_source = None
            m.kb_origin_project_id = None
            auto_count += 1
            lm.records_auto_fixed = (lm.records_auto_fixed or 0) + 1
            break
    if auto_count:
        db.commit()
    return auto_count


def prepopulate_from_cross_source_kb(
    db: Session,
    conversion: Conversion,
    mappings: Iterable[MappingSuggestion],
) -> int:
    """Cross-project Mapping Knowledge Base pre-population.

    For any suggestion still in ``suggested`` status, look up the project's
    source system Knowledge Bank — every approved ``column_mapping`` taught
    against the same source ERP in any *other* project. When a target field
    has a match whose normalized source-column name exists in the current
    dataset, mutate the suggestion to point at it at **confidence 0.85**
    with ``status=suggested``, set the ``kb_source`` provenance, and bump
    reuse stats on the LearnedMapping.

    Differences from :func:`apply_learned_to_conversion`:

    * Stays at 0.85, never auto-approves — different customer, different
      customizations, analyst stays in the loop.
    * Scoped by ``source_system`` so an EBS mapping doesn't pre-populate a
      NetSuite conversion.
    * Skips rows that already have a higher-confidence signal (e.g. the AI
      engine returned a strong match) — KB is a supplement, not a clobber.

    Returns the number of pre-populations performed (drives the run-AI
    toast: "N pre-filled from EBS-KB, M auto-applied, K AI-suggested").
    """
    business_object = _business_object_for(conversion)
    if not business_object:
        return 0
    project = conversion.project
    source_system = project.source_system if project else None
    if not source_system:
        return 0

    learned = (
        db.query(LearnedMapping)
        .filter(
            LearnedMapping.kind == "column_mapping",
            LearnedMapping.target_object == business_object,
            LearnedMapping.source_system == source_system,
            # Cross-project — exclude the current project's own captures
            # so we don't double-count same-project mappings (those go
            # through apply_learned_to_conversion).
            LearnedMapping.project_id != conversion.project_id,
        )
        .all()
    )
    if not learned:
        return 0

    by_target: dict[str, list[LearnedMapping]] = {}
    for lm in learned:
        if lm.target_field:
            by_target.setdefault(lm.target_field, []).append(lm)

    # Index the current dataset's columns by normalized name so we know
    # which learned source columns actually exist in this file.
    src_index: dict[str, str] = {}
    if conversion.dataset_id:
        for c in (
            db.query(DatasetColumnProfile)
            .filter(DatasetColumnProfile.dataset_id == conversion.dataset_id)
            .all()
        ):
            src_index[_normalize(c.column_name)] = c.column_name

    fields = {
        f.id: f.field_name
        for f in db.query(FBDIField)
        .filter(FBDIField.template_id == conversion.template_id)
        .all()
    }

    KB_CONFIDENCE = 0.85
    now = datetime.utcnow()
    hits = 0
    for m in mappings:
        if m.status != "suggested":
            continue
        # Don't overwrite a stronger AI signal. KB only fills weak suggestions.
        if m.confidence and m.confidence >= KB_CONFIDENCE:
            continue
        tgt_name = fields.get(m.target_field_id)
        if not tgt_name:
            continue
        candidates = by_target.get(tgt_name)
        if not candidates:
            continue
        # Pick the most-reused candidate first (more reuse = more battle-tested).
        candidates_sorted = sorted(
            candidates, key=lambda c: -(c.times_reused or 0),
        )
        for lm in candidates_sorted:
            actual_src = src_index.get(_normalize(lm.original_value))
            if not actual_src:
                continue
            origin = lm.captured_from or "prior project"
            uses = (lm.times_reused or 0)
            m.source_column = actual_src
            m.confidence = KB_CONFIDENCE
            m.review_required = 1
            m.reason = (
                f"🧠 From {source_system} Knowledge Bank — captured in "
                f"“{origin}”" + (f", {uses} prior reuse{'s' if uses != 1 else ''}" if uses else "")
            )
            if lm.rule_type:
                m.suggested_transformation = {
                    "rule_type": lm.rule_type,
                    "config": lm.rule_config or {},
                    "description": f"From {source_system} KB",
                }
            m.kb_source = source_system
            m.kb_origin_project_id = lm.originated_in_project_id or lm.project_id
            m.kb_times_reused = uses
            # Bump reuse stats on the source LearnedMapping.
            lm.times_reused = uses + 1
            lm.last_reused_at = now
            lm.last_reused_in_project_id = conversion.project_id
            hits += 1
            break
    if hits:
        db.commit()
    return hits

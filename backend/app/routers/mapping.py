"""Mapping suggestion endpoints — scoped to a Conversion (object)."""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from pydantic import BaseModel

from app.database import get_db
from app.models.conversion import Conversion
from app.models.mapping import MappingSuggestion
from app.models.transformation import Crosswalk, TransformationRule
from app.models.user import User
from app.parsers import parse_tabular
from app.schemas.mapping import MappingOut, MappingUpdate
from app.schemas.transformation import TransformationRuleCreate, TransformationRuleOut
from app.services.auth_service import get_current_user
from app.services.learning_service import (
    REFERENCE_KEY_FIELDS,
    list_reference_standards_for_object,
    record_learning_from_mapping,
    record_learning_from_rule,
)
from app.services.mapping_service import enrich_mapping_with_samples, run_mapping_suggestions
from app.transformations.engine import apply_pipeline

router = APIRouter(prefix="/api", tags=["mapping"])


def _require_conversion(db: Session, conversion_id: int) -> Conversion:
    c = db.query(Conversion).filter(Conversion.id == conversion_id).first()
    if not c:
        raise HTTPException(404, "Conversion not found")
    if not c.dataset_id or not c.template_id:
        raise HTTPException(
            400,
            "Conversion is not fully bound — set both a source dataset and a target FBDI template first.",
        )
    return c


@router.post(
    "/conversions/{conversion_id}/suggest-mapping", response_model=list[MappingOut]
)
def suggest_mapping(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conv = _require_conversion(db, conversion_id)
    saved = run_mapping_suggestions(db, conv)
    return enrich_mapping_with_samples(db, conv, saved)


@router.get(
    "/conversions/{conversion_id}/mappings", response_model=list[MappingOut]
)
def list_mappings(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conv = _require_conversion(db, conversion_id)
    items = (
        db.query(MappingSuggestion)
        .filter(MappingSuggestion.conversion_id == conversion_id)
        .all()
    )
    return enrich_mapping_with_samples(db, conv, items)


@router.get("/conversions/{conversion_id}/inherited-standards")
def list_inherited_standards(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """List the Reference Standards this conversion inherits from
    upstream masters. Drives the "↶ inherited from Item Master" badge
    in the Mapping Review inspector for downstream FK columns.

    A standard inherits when both hold:

      * The conversion's ``target_object`` is *not* the master entity
        (the master never inherits from itself).
      * One of the conversion's FBDI fields has a name that appears
        in ``REFERENCE_KEY_FIELDS[<master>]`` — i.e. it's an FK column
        for that master.

    Returns a list of ``{target_field, master_object, rule_type,
    rule_config, captured_from}`` rows. Scoped to the project's
    source_system so an EBS-taught standard doesn't bleed into a
    NetSuite engagement (and vice versa)."""
    conv = _require_conversion(db, conversion_id)
    project_source = (
        conv.project.source_system if conv.project else None
    )
    out: list[dict[str, Any]] = []
    if not conv.template:
        return out
    field_names = {f.field_name for f in conv.template.fields}
    for master_obj, key_fields in REFERENCE_KEY_FIELDS.items():
        if conv.target_object == master_obj:
            continue
        intersecting = sorted(set(key_fields) & field_names)
        if not intersecting:
            continue
        standards = list_reference_standards_for_object(
            db, master_obj, source_system=project_source,
        )
        for std in standards:
            if not std.rule_type:
                continue
            for fname in intersecting:
                out.append({
                    "target_field": fname,
                    "master_object": master_obj,
                    "rule_type": std.rule_type,
                    "rule_config": std.rule_config or {},
                    "captured_from": std.captured_from,
                    "originated_in_project_id": std.originated_in_project_id,
                })
    return out


@router.put("/mappings/{mapping_id}", response_model=MappingOut)
def update_mapping(
    mapping_id: int,
    payload: MappingUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = db.query(MappingSuggestion).filter(MappingSuggestion.id == mapping_id).first()
    if not m:
        raise HTTPException(404, "Mapping not found")
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] == "approved":
        m.approved_by = user.email
        m.approved_at = datetime.utcnow()
    for k, v in data.items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    conv = db.query(Conversion).filter(Conversion.id == m.conversion_id).first()
    if m.status in ("approved", "overridden") and m.source_column:
        record_learning_from_mapping(db, m, conv, captured_by=user.email)
    return enrich_mapping_with_samples(db, conv, [m])[0]


@router.put("/mappings/{mapping_id}/approve", response_model=MappingOut)
def approve_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = db.query(MappingSuggestion).filter(MappingSuggestion.id == mapping_id).first()
    if not m:
        raise HTTPException(404, "Mapping not found")

    # Slice 6 — dual-cert path. When ``requires_dual_approval=1``, the
    # first approval lands as ``approved_by`` and the row stays in
    # ``status="suggested"`` (visible as "Awaiting 2nd sign-off" in the
    # Mapping Inspector). The second approval (by a *different* user)
    # promotes it to ``approved``.
    if int(m.requires_dual_approval or 0) == 1:
        if not m.approved_by:
            m.approved_by = user.email
            m.approved_at = datetime.utcnow()
            db.commit()
            db.refresh(m)
            conv = db.query(Conversion).filter(Conversion.id == m.conversion_id).first()
            return enrich_mapping_with_samples(db, conv, [m])[0]
        if m.approved_by == user.email:
            raise HTTPException(
                409,
                "Dual-cert: second approval must come from a different user.",
            )
        m.second_approver_email = user.email
        m.second_approved_at = datetime.utcnow()
        m.status = "approved"
        db.commit()
        db.refresh(m)
        conv = db.query(Conversion).filter(Conversion.id == m.conversion_id).first()
        if m.source_column:
            record_learning_from_mapping(db, m, conv, captured_by=user.email)
        return enrich_mapping_with_samples(db, conv, [m])[0]

    m.status = "approved"
    m.approved_by = user.email
    m.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(m)
    conv = db.query(Conversion).filter(Conversion.id == m.conversion_id).first()
    if m.source_column:
        record_learning_from_mapping(db, m, conv, captured_by=user.email)
    return enrich_mapping_with_samples(db, conv, [m])[0]


@router.post(
    "/conversions/{conversion_id}/rules", response_model=TransformationRuleOut
)
def add_rule(
    conversion_id: int,
    payload: TransformationRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = db.query(Conversion).filter(Conversion.id == conversion_id).first()
    if not conv:
        raise HTTPException(404, "Conversion not found")
    seq = (
        db.query(TransformationRule)
        .filter(TransformationRule.conversion_id == conversion_id)
        .count()
    )
    r = TransformationRule(
        conversion_id=conversion_id, sequence=seq, **payload.model_dump()
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    # A manually-authored rule is just as authoritative as one approved on a
    # mapping — surface it in the Rule Library so future cycles can reuse it.
    record_learning_from_rule(db, r, conv, captured_by=user.email)
    return r


class PreviewRule(BaseModel):
    rule_type: str
    config: dict[str, Any] = {}


class PreviewRequest(BaseModel):
    rules: list[PreviewRule]
    source_column: str | None = None
    sample_size: int = 5


class PreviewSample(BaseModel):
    source: Any
    output: Any
    error: str | None = None


class PreviewResponse(BaseModel):
    samples: list[PreviewSample]


@router.post(
    "/conversions/{conversion_id}/rules/preview", response_model=PreviewResponse
)
def preview_rules(
    conversion_id: int,
    payload: PreviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dry-run a rule pipeline against the conversion's dataset and return
    source/output pairs for the first N rows. Powers the Studio live preview.
    """
    conv = db.query(Conversion).filter(Conversion.id == conversion_id).first()
    if not conv or not conv.dataset:
        raise HTTPException(404, "Conversion or dataset not found")

    df = parse_tabular(conv.dataset.file_path, file_type=conv.dataset.file_type)

    crosswalks: dict[str, dict[str, str]] = {}
    for cw in (
        db.query(Crosswalk).filter(Crosswalk.conversion_id == conversion_id).all()
    ):
        crosswalks.setdefault(cw.name, {})[cw.source_value] = cw.target_value

    rules = [{"rule_type": r.rule_type, "config": r.config} for r in payload.rules]

    out: list[PreviewSample] = []
    n = max(1, min(int(payload.sample_size), 20))
    for idx, row in df.head(n).iterrows():
        row_dict = {k: ("" if v is None else v) for k, v in row.to_dict().items()}
        src_value = (
            row_dict.get(payload.source_column)
            if payload.source_column
            else None
        )
        ctx = {
            "row_index": int(idx) + 1,
            "current_user": user.email,
            "now": datetime.utcnow(),
            "crosswalks": crosswalks,
        }
        try:
            transformed = apply_pipeline(rules, src_value, row=row_dict, ctx=ctx)
            out.append(PreviewSample(source=src_value, output=transformed))
        except Exception as exc:  # surface engine errors to the UI
            out.append(
                PreviewSample(source=src_value, output=None, error=str(exc))
            )
    return PreviewResponse(samples=out)


class TranslateRequest(BaseModel):
    description: str
    target_field_id: int | None = None
    source_column: str | None = None
    # Limited to the user's conversion so we ground the LLM in the actual
    # column catalog + sample rows. The endpoint never accepts a free-form
    # column list.
    sample_size: int = 5


class TranslateAmbiguity(BaseModel):
    phrase: str
    interpreted_as: str
    alternatives: list[str] = []


class TranslateResponse(BaseModel):
    rule_type: str
    config: dict[str, Any]
    explanation: str
    ambiguities: list[TranslateAmbiguity] = []
    # Mirror the live-preview shape so the UI can render the post-translate
    # before/after table without a second round-trip.
    preview_samples: list[PreviewSample] = []
    # "local" when the deterministic pattern matcher resolved the rule,
    # "ai" when Claude was called. Surfaced in the modal so the analyst
    # knows whether to scrutinise more carefully.
    source: str = "ai"


@router.post(
    "/conversions/{conversion_id}/rules/translate",
    response_model=TranslateResponse,
    responses={
        503: {"description": "Translator is unavailable (no API key configured)."},
    },
)
def translate_rule(
    conversion_id: int,
    payload: TranslateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Translate a natural-language description into a structured engine
    rule by calling Claude with a tool-use JSON schema. Returns the
    structured rule + an explanation + ambiguity callouts + a runnable
    preview against the conversion's dataset.

    The result is **never persisted** here — the UI displays the translated
    structured form, lets the analyst confirm/edit, then POSTs the final
    config to ``/conversions/{id}/rules`` like any other rule.
    """
    from app.services.rule_translator import (
        TranslatorError, TranslatorUnavailable, translate_description,
    )

    conv = db.query(Conversion).filter(Conversion.id == conversion_id).first()
    if not conv or not conv.dataset:
        raise HTTPException(404, "Conversion or dataset not found")

    df = parse_tabular(conv.dataset.file_path, file_type=conv.dataset.file_type)
    columns = list(df.columns.astype(str))
    sample_rows: list[dict[str, Any]] = [
        {k: ("" if v is None else v) for k, v in r.to_dict().items()}
        for _, r in df.head(max(1, min(int(payload.sample_size), 20))).iterrows()
    ]

    target_field_name: str | None = None
    target_data_type: str | None = None
    if payload.target_field_id and conv.template:
        for f in conv.template.fields:
            if f.id == payload.target_field_id:
                target_field_name = f.field_name
                target_data_type = f.data_type
                break

    try:
        result = translate_description(
            description=payload.description,
            columns=columns,
            sample_rows=sample_rows,
            target_field=target_field_name,
            target_data_type=target_data_type,
        )
    except TranslatorUnavailable as e:
        raise HTTPException(503, str(e))
    except TranslatorError as e:
        raise HTTPException(422, str(e))

    # Now run the translated rule through the same dry-run path the
    # preview endpoint uses, so the UI's before/after panel is consistent
    # across "Translate" and "Save".
    crosswalks: dict[str, dict[str, str]] = {}
    for cw in (
        db.query(Crosswalk).filter(Crosswalk.conversion_id == conversion_id).all()
    ):
        crosswalks.setdefault(cw.name, {})[cw.source_value] = cw.target_value

    rules = [{"rule_type": result.rule_type, "config": result.config}]
    preview: list[PreviewSample] = []
    for idx, row_dict in enumerate(sample_rows, start=1):
        src_value = (
            row_dict.get(payload.source_column) if payload.source_column else None
        )
        ctx = {
            "row_index": idx,
            "current_user": user.email,
            "now": datetime.utcnow(),
            "crosswalks": crosswalks,
        }
        try:
            transformed = apply_pipeline(rules, src_value, row=row_dict, ctx=ctx)
            preview.append(PreviewSample(source=src_value, output=transformed))
        except Exception as exc:
            preview.append(
                PreviewSample(source=src_value, output=None, error=str(exc))
            )

    return TranslateResponse(
        rule_type=result.rule_type,
        config=result.config,
        explanation=result.explanation,
        ambiguities=[
            TranslateAmbiguity(
                phrase=a["phrase"],
                interpreted_as=a["interpreted_as"],
                alternatives=a.get("alternatives") or [],
            )
            for a in result.ambiguities
        ],
        preview_samples=preview,
        source=result.source,
    )


@router.get(
    "/conversions/{conversion_id}/rules", response_model=list[TransformationRuleOut]
)
def list_rules(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return (
        db.query(TransformationRule)
        .filter(TransformationRule.conversion_id == conversion_id)
        .order_by(TransformationRule.sequence)
        .all()
    )


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    r = db.query(TransformationRule).filter(TransformationRule.id == rule_id).first()
    if not r:
        raise HTTPException(404, "Rule not found")
    db.delete(r)
    db.commit()
    return {"deleted": rule_id}

"""Chart-of-Accounts Engine endpoints.

  GET    /conversions/{id}/coa                       — structure + segments
  POST   /conversions/{id}/coa/seed                  — seed canonical 5-segment template
  PATCH  /coa-structures/{id}                        — separator / lock / name
  POST   /coa-structures/{id}/segments               — append segment
  PATCH  /coa-segments/{id}                          — edit segment
  DELETE /coa-segments/{id}                          — remove segment
  GET    /coa-segments/{id}/crosswalks               — list crosswalk rows
  POST   /coa-segments/{id}/crosswalks               — single upsert
  POST   /coa-segments/{id}/crosswalks/bulk          — CSV / JSON bulk upsert
  DELETE /coa-crosswalks/{id}
  POST   /conversions/{id}/coa/compose               — dry-run, returns sample
                                                       rows + coverage
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.coa import (
    COA_DERIVATION_KINDS, COASegment, COAStructure, COAValueCrosswalk,
)
from app.models.conversion import Conversion
from app.models.user import User
from app.parsers import parse_tabular
from app.services.audit_service import record_event
from app.services.auth_service import get_current_user
from app.services.coa_engine import compose_accounts


router = APIRouter(prefix="/api", tags=["coa-engine"])


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _require_conversion(db: Session, conversion_id: int) -> Conversion:
    c = db.query(Conversion).filter(Conversion.id == conversion_id).first()
    if not c:
        raise HTTPException(404, "Conversion not found")
    return c


def _require_structure(db: Session, structure_id: int) -> COAStructure:
    s = db.query(COAStructure).filter(COAStructure.id == structure_id).first()
    if not s:
        raise HTTPException(404, "COA structure not found")
    return s


def _require_segment(db: Session, segment_id: int) -> COASegment:
    s = db.query(COASegment).filter(COASegment.id == segment_id).first()
    if not s:
        raise HTTPException(404, "COA segment not found")
    return s


# ─── Schemas ───────────────────────────────────────────────────────


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    structure_id: int
    position: int
    name: str
    length: int
    derivation_kind: str
    derivation_config: dict[str, Any] = {}
    default_value: str | None = None
    valid_values: list[str] = []
    pad_style: str = "left_zero"
    description: str | None = None


class StructureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversion_id: int
    name: str
    separator: str
    target_ledger: str | None = None
    description: str | None = None
    locked: bool = False
    segments: list[SegmentOut] = []


class StructureUpdate(BaseModel):
    name: str | None = None
    separator: str | None = None
    target_ledger: str | None = None
    description: str | None = None
    locked: bool | None = None


class SegmentCreate(BaseModel):
    name: str
    length: int
    derivation_kind: str = "source_column"
    derivation_config: dict[str, Any] = {}
    default_value: str | None = None
    valid_values: list[str] = []
    pad_style: str = "left_zero"
    description: str | None = None
    position: int | None = None


class SegmentUpdate(BaseModel):
    name: str | None = None
    length: int | None = None
    derivation_kind: str | None = None
    derivation_config: dict[str, Any] | None = None
    default_value: str | None = None
    valid_values: list[str] | None = None
    pad_style: str | None = None
    description: str | None = None
    position: int | None = None


class CrosswalkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    segment_id: int
    legacy_value: str
    fusion_value: str
    description: str | None = None
    notes: str | None = None
    approved_by: str | None = None
    approved_at: Any | None = None
    created_by: str | None = None


class CrosswalkUpsert(BaseModel):
    legacy_value: str
    fusion_value: str
    description: str | None = None
    notes: str | None = None


class CrosswalkBulkUpsert(BaseModel):
    rows: list[CrosswalkUpsert]


class ComposeSegmentEmission(BaseModel):
    segment: str
    value: str
    valid: bool
    reason: str | None = None


class ComposedRowOut(BaseModel):
    source_index: int
    composed_account: str
    valid: bool
    emissions: list[ComposeSegmentEmission]


class ComposeResponse(BaseModel):
    sample_rows: list[ComposedRowOut]
    total_rows: int
    valid_rows: int
    invalid_rows: int
    coverage_pct: float
    per_segment_coverage: dict[str, dict[str, Any]] = {}
    per_segment_unmapped_values: dict[str, list[str]] = {}


# ─── Structure ─────────────────────────────────────────────────────


@router.get("/conversions/{conversion_id}/coa", response_model=StructureOut | None)
def get_structure(
    conversion_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_conversion(db, conversion_id)
    s = (
        db.query(COAStructure)
        .filter(COAStructure.conversion_id == conversion_id)
        .first()
    )
    return s


_CANONICAL_SEGMENTS = [
    {
        "position": 1, "name": "Company", "length": 2, "pad_style": "left_zero",
        "derivation_kind": "crosswalk",
        "derivation_config": {"column": "COMPANY_CODE"},
        "description": "Legal entity / company segment",
    },
    {
        "position": 2, "name": "CostCenter", "length": 4, "pad_style": "left_zero",
        "derivation_kind": "crosswalk",
        "derivation_config": {"column": "CC_CODE"},
        "description": "Cost centre / department segment",
    },
    {
        "position": 3, "name": "NaturalAccount", "length": 6, "pad_style": "left_zero",
        "derivation_kind": "crosswalk",
        "derivation_config": {"column": "ACCOUNT_CODE"},
        "description": "Natural account (GL account) segment",
    },
    {
        "position": 4, "name": "SubAccount", "length": 4, "pad_style": "left_zero",
        "derivation_kind": "source_column",
        "derivation_config": {"column": "SUB_ACCOUNT"},
        "default_value": "0000",
        "description": "Location / sub-account; defaults to 0000",
    },
    {
        "position": 5, "name": "Product", "length": 4, "pad_style": "left_zero",
        "derivation_kind": "crosswalk",
        "derivation_config": {"column": "ITEM_CATEGORY"},
        "default_value": "0000",
        "description": "Product / category segment",
    },
]


@router.post(
    "/conversions/{conversion_id}/coa/seed", response_model=StructureOut,
)
def seed_structure(
    conversion_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = _require_conversion(db, conversion_id)
    existing = (
        db.query(COAStructure)
        .filter(COAStructure.conversion_id == conversion_id)
        .first()
    )
    if existing:
        return existing
    struct = COAStructure(
        conversion_id=conversion_id,
        name="Fusion COA Structure (Demo)",
        separator="-",
        target_ledger="USCOA",
        description="Canonical 5-segment Fusion Chart-of-Accounts template.",
    )
    db.add(struct)
    db.flush()
    for tpl in _CANONICAL_SEGMENTS:
        db.add(COASegment(structure_id=struct.id, **tpl))
    db.commit()
    db.refresh(struct)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="coa_structure",
        target_id=struct.id,
        project_id=conv.project_id,
        summary=f"Seeded canonical 5-segment COA on '{conv.name}'",
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return struct


@router.patch("/coa-structures/{structure_id}", response_model=StructureOut)
def update_structure(
    structure_id: int,
    payload: StructureUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _require_structure(db, structure_id)
    if s.locked and payload.locked is not False:
        raise HTTPException(409, "Structure is locked. Unlock before editing.")
    data = payload.model_dump(exclude_unset=True)
    if data.get("locked") is True and not s.locked:
        from datetime import datetime
        s.locked_at = datetime.utcnow()
        s.locked_by = user.email
    if data.get("locked") is False and s.locked:
        s.locked_at = None
        s.locked_by = None
    for k, v in data.items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="coa_structure",
        target_id=s.id,
        summary=(
            f"COA structure {'locked' if s.locked else 'updated'}"
        ),
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return s


# ─── Segments ──────────────────────────────────────────────────────


@router.post(
    "/coa-structures/{structure_id}/segments", response_model=SegmentOut,
)
def add_segment(
    structure_id: int,
    payload: SegmentCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    s = _require_structure(db, structure_id)
    if s.locked:
        raise HTTPException(409, "Structure is locked.")
    if payload.derivation_kind not in COA_DERIVATION_KINDS:
        raise HTTPException(
            400,
            f"Unknown derivation_kind '{payload.derivation_kind}'. "
            f"Valid: {COA_DERIVATION_KINDS}",
        )
    next_pos = payload.position or (
        max((seg.position for seg in s.segments), default=0) + 1
    )
    row = COASegment(
        structure_id=s.id, position=next_pos,
        **{k: v for k, v in payload.model_dump().items() if k != "position"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/coa-segments/{segment_id}", response_model=SegmentOut)
def update_segment(
    segment_id: int,
    payload: SegmentUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    seg = _require_segment(db, segment_id)
    if seg.structure.locked:
        raise HTTPException(409, "Structure is locked.")
    data = payload.model_dump(exclude_unset=True)
    if "derivation_kind" in data and data["derivation_kind"] not in COA_DERIVATION_KINDS:
        raise HTTPException(400, f"Unknown derivation_kind '{data['derivation_kind']}'")
    for k, v in data.items():
        setattr(seg, k, v)
    db.commit()
    db.refresh(seg)
    return seg


@router.delete("/coa-segments/{segment_id}")
def remove_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    seg = _require_segment(db, segment_id)
    if seg.structure.locked:
        raise HTTPException(409, "Structure is locked.")
    db.delete(seg)
    db.commit()
    return {"deleted": segment_id}


# ─── Crosswalk rows ────────────────────────────────────────────────


@router.get(
    "/coa-segments/{segment_id}/crosswalks", response_model=list[CrosswalkOut],
)
def list_crosswalks(
    segment_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _require_segment(db, segment_id)
    return (
        db.query(COAValueCrosswalk)
        .filter(COAValueCrosswalk.segment_id == segment_id)
        .order_by(COAValueCrosswalk.legacy_value)
        .all()
    )


def _upsert_crosswalk_row(
    db: Session, segment: COASegment, payload: CrosswalkUpsert, user_email: str,
) -> COAValueCrosswalk:
    existing = (
        db.query(COAValueCrosswalk)
        .filter(
            COAValueCrosswalk.segment_id == segment.id,
            COAValueCrosswalk.legacy_value == payload.legacy_value,
        )
        .first()
    )
    if existing:
        existing.fusion_value = payload.fusion_value
        if payload.description is not None:
            existing.description = payload.description
        if payload.notes is not None:
            existing.notes = payload.notes
        return existing
    row = COAValueCrosswalk(
        structure_id=segment.structure_id,
        segment_id=segment.id,
        legacy_value=payload.legacy_value,
        fusion_value=payload.fusion_value,
        description=payload.description,
        notes=payload.notes,
        created_by=user_email,
    )
    db.add(row)
    return row


@router.post(
    "/coa-segments/{segment_id}/crosswalks", response_model=CrosswalkOut,
)
def upsert_crosswalk(
    segment_id: int,
    payload: CrosswalkUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    seg = _require_segment(db, segment_id)
    row = _upsert_crosswalk_row(db, seg, payload, user.email)
    db.commit()
    db.refresh(row)
    return row


@router.post(
    "/coa-segments/{segment_id}/crosswalks/bulk", response_model=list[CrosswalkOut],
)
def upsert_crosswalk_bulk(
    segment_id: int,
    payload: CrosswalkBulkUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    seg = _require_segment(db, segment_id)
    rows: list[COAValueCrosswalk] = []
    for r in payload.rows:
        rows.append(_upsert_crosswalk_row(db, seg, r, user.email))
    db.commit()
    return rows


@router.post(
    "/coa-segments/{segment_id}/crosswalks/upload", response_model=list[CrosswalkOut],
)
async def upload_crosswalk_csv(
    segment_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Bulk upload a CSV with at minimum two columns: ``legacy_value``
    and ``fusion_value``. Optional columns: ``description``, ``notes``.
    Rows are upserted by (segment_id, legacy_value) so re-uploading
    refreshes rather than duplicates."""
    seg = _require_segment(db, segment_id)
    text = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    needed = {"legacy_value", "fusion_value"}
    if not needed.issubset(set(reader.fieldnames or [])):
        raise HTTPException(
            400,
            f"CSV must include columns: {sorted(needed)}; got "
            f"{reader.fieldnames}",
        )
    out: list[COAValueCrosswalk] = []
    for r in reader:
        payload = CrosswalkUpsert(
            legacy_value=(r.get("legacy_value") or "").strip(),
            fusion_value=(r.get("fusion_value") or "").strip(),
            description=(r.get("description") or "").strip() or None,
            notes=(r.get("notes") or "").strip() or None,
        )
        if not payload.legacy_value:
            continue
        out.append(_upsert_crosswalk_row(db, seg, payload, user.email))
    db.commit()
    return out


@router.delete("/coa-crosswalks/{crosswalk_id}")
def remove_crosswalk(
    crosswalk_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = db.query(COAValueCrosswalk).filter(COAValueCrosswalk.id == crosswalk_id).first()
    if not row:
        raise HTTPException(404, "Crosswalk not found")
    db.delete(row)
    db.commit()
    return {"deleted": crosswalk_id}


# ─── Composition (dry run) ─────────────────────────────────────────


@router.post(
    "/conversions/{conversion_id}/coa/compose", response_model=ComposeResponse,
)
def compose(
    conversion_id: int,
    sample_size: int = 25,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    conv = _require_conversion(db, conversion_id)
    if not conv.dataset:
        raise HTTPException(400, "Conversion is not bound to a dataset.")
    structure = (
        db.query(COAStructure)
        .filter(COAStructure.conversion_id == conversion_id)
        .first()
    )
    if not structure:
        raise HTTPException(400, "No COA structure on this conversion. Seed first.")
    df = parse_tabular(conv.dataset.file_path, file_type=conv.dataset.file_type)
    result = compose_accounts(structure, df, sample_size=max(1, min(int(sample_size), 200)))
    return ComposeResponse(
        sample_rows=[
            ComposedRowOut(
                source_index=r.source_index,
                composed_account=r.composed_account,
                valid=r.valid,
                emissions=[
                    ComposeSegmentEmission(
                        segment=structure.segments[i].name,
                        value=e.value, valid=e.valid, reason=e.reason,
                    )
                    for i, e in enumerate(r.segment_emissions)
                ],
            )
            for r in result.sample_rows
        ],
        total_rows=result.total_rows,
        valid_rows=result.valid_rows,
        invalid_rows=result.invalid_rows,
        coverage_pct=result.coverage_pct,
        per_segment_coverage=result.per_segment_coverage,
        per_segment_unmapped_values=result.per_segment_unmapped_values,
    )

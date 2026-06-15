"""Learning library endpoints — registry of human-approved mappings/rules."""
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.learned import LearnedMapping
from app.models.user import User
from app.schemas.learned import LearnedMappingCreate, LearnedMappingOut, LearningStats
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/learned-mappings", tags=["learning"])


# Default category seeds shown in the empty-state grid (mirrors CHRM AI's pattern)
DEFAULT_CATEGORIES = [
    "Column Mapping Alias",
    "SKU / Item Format Alias",
    "Customer Alias",
    "Supplier Alias",
    "UOM Conversion Rule",
    "Status Value Mapping",
    "Date Format Rule",
    "Currency Mapping",
    "Organization Code Mapping",
    "Branch Code Mapping",
]


@router.post("", response_model=LearnedMappingOut)
def create_learned(
    payload: LearnedMappingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = LearnedMapping(**payload.model_dump(), captured_by=user.email)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("", response_model=list[LearnedMappingOut])
def list_learned(
    kind: str | None = None,
    category: str | None = None,
    project_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(LearnedMapping)
    if kind:
        q = q.filter(LearnedMapping.kind == kind)
    if category:
        q = q.filter(LearnedMapping.category == category)
    # Scope to one engagement when project_id is set. Learned mappings
    # are tagged with ``originated_in_project_id`` at capture time so
    # the Learning Center can show "what did this engagement teach the
    # bank" — not just the cross-project rollup.
    if project_id is not None:
        q = q.filter(LearnedMapping.originated_in_project_id == project_id)
    return q.order_by(LearnedMapping.captured_at.desc()).all()


@router.get("/stats", response_model=LearningStats)
def learning_stats(
    project_id: int | None = None,
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    q = db.query(LearnedMapping)
    if project_id is not None:
        q = q.filter(LearnedMapping.originated_in_project_id == project_id)
    items = q.all()
    total = len(items)
    avg_boost = round(
        sum(i.confidence_boost or 0 for i in items) / total, 3
    ) if total else 0.0
    records_fixed = sum(int(i.records_auto_fixed or 0) for i in items)

    # Heuristic — assume each captured rule saves ~4 minutes of analyst time
    minutes_saved = total * 4

    by_cat = Counter(i.category for i in items)
    # Always include the seed categories so the UI shows the empty buckets too
    cat_rows = []
    for c in DEFAULT_CATEGORIES:
        cat_rows.append({"category": c, "count": by_cat.get(c, 0)})
    # Plus any extras captured from approvals not in the default set
    for c in by_cat:
        if c not in DEFAULT_CATEGORIES:
            cat_rows.append({"category": c, "count": by_cat[c]})

    return {
        "total": total,
        "avg_confidence_boost": avg_boost,
        "records_auto_fixed": records_fixed,
        "analyst_minutes_saved": minutes_saved,
        "by_category": cat_rows,
    }


@router.delete("/{learned_id}")
def delete_learned(
    learned_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    item = db.query(LearnedMapping).filter(LearnedMapping.id == learned_id).first()
    if not item:
        raise HTTPException(404, "Not found")
    db.delete(item)
    db.commit()
    return {"deleted": learned_id}


@router.get("/knowledge-bank/stats")
def knowledge_bank_stats(
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    """Per-source-system rollup for the Knowledge Bank section on the
    Learning Center page.

    For each source system the workbench has ever captured a mapping for,
    returns:

    * ``mappings`` — count of distinct (kind=column_mapping) records.
    * ``rules`` — count of (kind=rule) records.
    * ``reference_standards`` — count of (kind=reference_standard) records.
    * ``projects`` — distinct projects that contributed.
    * ``total_reuses`` — sum of ``times_reused`` across the bank.
    * ``avg_reuse_per_mapping`` — float, 0 when the bank has never been
      hit on a fresh project.
    """
    rows = (
        db.query(LearnedMapping)
        .filter(LearnedMapping.source_system.isnot(None))
        .all()
    )
    by_source: dict[str, dict[str, Any]] = {}
    for lm in rows:
        src = lm.source_system or "unknown"
        bucket = by_source.setdefault(
            src,
            {
                "source_system": src,
                "mappings": 0,
                "rules": 0,
                "reference_standards": 0,
                "projects": set(),
                "total_reuses": 0,
                "last_reused_at": None,
            },
        )
        if lm.kind == "column_mapping":
            bucket["mappings"] += 1
        elif lm.kind == "rule":
            bucket["rules"] += 1
        elif lm.kind == "reference_standard":
            bucket["reference_standards"] += 1
        if lm.project_id:
            bucket["projects"].add(lm.project_id)
        bucket["total_reuses"] += int(lm.times_reused or 0)
        if lm.last_reused_at and (
            bucket["last_reused_at"] is None
            or lm.last_reused_at > bucket["last_reused_at"]
        ):
            bucket["last_reused_at"] = lm.last_reused_at
    # Serialize
    out = []
    for src, b in by_source.items():
        out.append({
            "source_system": src,
            "mappings": b["mappings"],
            "rules": b["rules"],
            "reference_standards": b["reference_standards"],
            "project_count": len(b["projects"]),
            "total_reuses": b["total_reuses"],
            "avg_reuse_per_mapping": (
                round(b["total_reuses"] / b["mappings"], 2)
                if b["mappings"] else 0.0
            ),
            "last_reused_at": (
                b["last_reused_at"].isoformat() if b["last_reused_at"] else None
            ),
        })
    out.sort(key=lambda r: (-r["mappings"], r["source_system"]))
    return out

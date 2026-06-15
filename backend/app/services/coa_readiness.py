"""COA Readiness — gate the Cutover-Go sign-off on chart-of-accounts completeness.

When a project has at least one conversion with a ``COAStructure`` attached
(the GL Coding Combinations conversion does, by definition), the
``cutover_go`` sign-off MUST be blocked until composition coverage is at
or above the configured threshold — and the user MUST see why.

This module exposes:

  * :func:`compute_project_coa_readiness` — per-conversion readiness rows
    plus a project-level worst-case rollup. Called both by the API
    endpoint that the UI polls before showing the sign-off modal, and by
    the create_sign_off gate.
  * :func:`require_coa_ready_for_cutover` — raises ``HTTPException(409)``
    when readiness is below the threshold. Designed to be called from
    the ``cutover_go`` branch in the sign-off router.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.conversion import Conversion
from app.models.coa import COAStructure
from app.services.coa_engine import compose_accounts
from app.services.dataset_service import parse_tabular


# A 1% slop band. In practice a single legacy row failing to compose can be
# the difference between 100.0% and 99.94%, which is usually a "we'll
# write-off these 3 rows" decision rather than a true cutover-blocker.
COA_READINESS_THRESHOLD_PCT = 99.0


@dataclass
class ConversionCOAReadiness:
    conversion_id: int
    conversion_name: str
    has_structure: bool
    has_dataset: bool
    coverage_pct: float | None
    total_rows: int
    invalid_rows: int
    gaps_by_segment: dict[str, int]
    blocker_reason: str | None


@dataclass
class ProjectCOAReadiness:
    threshold_pct: float
    is_ready: bool
    worst_coverage_pct: float | None
    conversions: list[ConversionCOAReadiness]
    blocker_reason: str | None  # human-readable summary for the UI banner


def _evaluate_conversion(
    db: Session, conv: Conversion,
) -> ConversionCOAReadiness:
    structure = (
        db.query(COAStructure)
        .filter(COAStructure.conversion_id == conv.id)
        .first()
    )
    if not structure:
        return ConversionCOAReadiness(
            conversion_id=conv.id, conversion_name=conv.name,
            has_structure=False, has_dataset=bool(conv.dataset),
            coverage_pct=None, total_rows=0, invalid_rows=0,
            gaps_by_segment={}, blocker_reason=None,
        )
    if not conv.dataset:
        return ConversionCOAReadiness(
            conversion_id=conv.id, conversion_name=conv.name,
            has_structure=True, has_dataset=False,
            coverage_pct=None, total_rows=0, invalid_rows=0,
            gaps_by_segment={},
            blocker_reason="COA structure exists but no dataset is bound — cannot evaluate coverage.",
        )
    try:
        df = parse_tabular(conv.dataset.file_path, file_type=conv.dataset.file_type)
    except Exception as exc:
        # Catalog a clean blocker rather than 500ing the gate.
        return ConversionCOAReadiness(
            conversion_id=conv.id, conversion_name=conv.name,
            has_structure=True, has_dataset=True,
            coverage_pct=None, total_rows=0, invalid_rows=0,
            gaps_by_segment={},
            blocker_reason=f"Dataset failed to parse: {type(exc).__name__}",
        )
    result = compose_accounts(structure, df, sample_size=1)
    gaps = {
        name: payload.get("invalid_rows", 0)
        for name, payload in (result.per_segment_coverage or {}).items()
        if payload.get("invalid_rows", 0) > 0
    }
    blocker = None
    if result.coverage_pct < COA_READINESS_THRESHOLD_PCT:
        blocker = (
            f"COA coverage is {result.coverage_pct:.1f}% — "
            f"{result.invalid_rows} of {result.total_rows} rows "
            f"will not compose. Threshold is {COA_READINESS_THRESHOLD_PCT}%."
        )
    return ConversionCOAReadiness(
        conversion_id=conv.id, conversion_name=conv.name,
        has_structure=True, has_dataset=True,
        coverage_pct=result.coverage_pct,
        total_rows=result.total_rows,
        invalid_rows=result.invalid_rows,
        gaps_by_segment=gaps,
        blocker_reason=blocker,
    )


def compute_project_coa_readiness(db: Session, project_id: int) -> ProjectCOAReadiness:
    convs = (
        db.query(Conversion)
        .filter(Conversion.project_id == project_id)
        .all()
    )
    rows = [_evaluate_conversion(db, c) for c in convs]
    # Only conversions that actually carry a COA structure participate in
    # the gate — most conversions don't (Item Master, Suppliers, etc.).
    coa_rows = [r for r in rows if r.has_structure]
    if not coa_rows:
        # No COA scope on this project — the gate doesn't apply, and we
        # report that affirmatively so the UI can render "COA gate: N/A"
        # rather than a misleading green tick.
        return ProjectCOAReadiness(
            threshold_pct=COA_READINESS_THRESHOLD_PCT,
            is_ready=True,
            worst_coverage_pct=None,
            conversions=rows,
            blocker_reason=None,
        )
    blockers = [r for r in coa_rows if r.blocker_reason]
    worst = None
    for r in coa_rows:
        if r.coverage_pct is None:
            continue
        if worst is None or r.coverage_pct < worst:
            worst = r.coverage_pct
    return ProjectCOAReadiness(
        threshold_pct=COA_READINESS_THRESHOLD_PCT,
        is_ready=not blockers,
        worst_coverage_pct=worst,
        conversions=rows,
        blocker_reason=(
            f"{len(blockers)} COA conversion(s) below threshold: "
            + "; ".join(b.conversion_name for b in blockers[:3])
            if blockers else None
        ),
    )


def require_coa_ready_for_cutover(db: Session, project_id: int) -> None:
    """Raise 409 if the cutover-go sign-off would land on an incomplete COA.
    No-op when the project has no COA scope at all (some implementations
    aren't Finance-led)."""
    state = compute_project_coa_readiness(db, project_id)
    if state.is_ready:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "message": state.blocker_reason or "COA coverage below threshold for cutover go-live.",
            "threshold_pct": state.threshold_pct,
            "worst_coverage_pct": state.worst_coverage_pct,
            "conversions": [
                {
                    "conversion_id": r.conversion_id,
                    "conversion_name": r.conversion_name,
                    "coverage_pct": r.coverage_pct,
                    "invalid_rows": r.invalid_rows,
                    "total_rows": r.total_rows,
                    "blocker_reason": r.blocker_reason,
                }
                for r in state.conversions if r.has_structure
            ],
        },
    )


def serialize_readiness(state: ProjectCOAReadiness) -> dict[str, Any]:
    return {
        "threshold_pct": state.threshold_pct,
        "is_ready": state.is_ready,
        "worst_coverage_pct": state.worst_coverage_pct,
        "blocker_reason": state.blocker_reason,
        "conversions": [
            {
                "conversion_id": r.conversion_id,
                "conversion_name": r.conversion_name,
                "has_structure": r.has_structure,
                "has_dataset": r.has_dataset,
                "coverage_pct": r.coverage_pct,
                "total_rows": r.total_rows,
                "invalid_rows": r.invalid_rows,
                "gaps_by_segment": r.gaps_by_segment,
                "blocker_reason": r.blocker_reason,
            }
            for r in state.conversions
        ],
    }

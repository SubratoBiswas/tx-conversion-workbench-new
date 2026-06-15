"""Data Quality Score for a single conversion.

A composite signal in the range [0..100] computed from three lenses:

  Mapping coverage           weight 0.45  — % of target fields with a source
                                            column + approved status.
  Validation cleanliness     weight 0.30  — penalty for unresolved cleansing /
                                            validation issues per row.
  Reconciliation status      weight 0.25  — % of recon checks at "pass".

The score is *honest by design*:

* A conversion with zero mappings scores 0.
* A conversion where every required FBDI field is mapped + approved AND
  every recon check passes scores 100.
* Open critical issues on the project don't drag a specific conversion's
  score (they drag the project-level Readiness Score instead).

Service entry point: :func:`compute_for_conversion`. The CFO summary,
Project Overview tiles, and Migration Readiness Score all call this; it
also persists the latest score onto ``Conversion.data_quality_score`` so
downstream reads are cheap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.conversion import Conversion
from app.models.cutover import ReconciliationCheck
from app.models.fbdi import FBDIField
from app.models.mapping import MappingSuggestion
from app.models.validation import ValidationIssue


@dataclass
class QualityScoreLens:
    code: str
    value: float        # 0..1
    value_pct: int      # 0..100
    weight: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityScoreResult:
    total: float        # 0..100
    lenses: list[QualityScoreLens]


_WEIGHTS = {
    "mapping_coverage":         0.45,
    "validation_cleanliness":   0.30,
    "reconciliation":           0.25,
}


def _mapping_coverage(db: Session, conversion: Conversion) -> QualityScoreLens:
    if not conversion.template_id:
        return QualityScoreLens(
            code="mapping_coverage", value=0.0, value_pct=0,
            weight=_WEIGHTS["mapping_coverage"],
            details={"reason": "No FBDI template bound"},
        )
    total_targets = (
        db.query(FBDIField)
        .filter(FBDIField.template_id == conversion.template_id)
        .count()
    )
    required_targets = (
        db.query(FBDIField)
        .filter(
            FBDIField.template_id == conversion.template_id,
            FBDIField.required.is_(True),
        )
        .count()
    )
    mappings = (
        db.query(MappingSuggestion)
        .filter(MappingSuggestion.conversion_id == conversion.id)
        .all()
    )
    if total_targets == 0:
        return QualityScoreLens(
            code="mapping_coverage", value=0.0, value_pct=0,
            weight=_WEIGHTS["mapping_coverage"],
            details={"reason": "FBDI template has zero target fields"},
        )
    mapped = sum(1 for m in mappings if m.source_column)
    approved = sum(
        1 for m in mappings
        if m.status in ("approved", "overridden") and m.source_column
    )
    # Required-coverage matters more than total coverage at cutover.
    required_mapped = 0
    if required_targets:
        approved_field_ids = {
            m.target_field_id for m in mappings
            if m.status in ("approved", "overridden") and m.source_column
        }
        required_mapped = (
            db.query(FBDIField)
            .filter(
                FBDIField.template_id == conversion.template_id,
                FBDIField.required.is_(True),
                FBDIField.id.in_(approved_field_ids or [-1]),
            )
            .count()
        )
    required_coverage = (
        required_mapped / required_targets if required_targets else 1.0
    )
    overall_approval = (approved / total_targets) if total_targets else 0.0
    # Blend: required coverage carries 70%, overall approval 30%.
    score = round(0.7 * required_coverage + 0.3 * overall_approval, 3)
    return QualityScoreLens(
        code="mapping_coverage", value=score, value_pct=int(score * 100),
        weight=_WEIGHTS["mapping_coverage"],
        details={
            "total_targets": total_targets,
            "required_targets": required_targets,
            "required_mapped": required_mapped,
            "approved": approved,
        },
    )


def _validation_cleanliness(db: Session, conversion: Conversion) -> QualityScoreLens:
    issues = (
        db.query(ValidationIssue)
        .filter(ValidationIssue.conversion_id == conversion.id)
        .all()
    )
    if not issues:
        # No issues recorded yet ≠ clean. It might just mean nothing has
        # been validated. Return 0 so the score honestly says "unproven".
        # Once at least one validation run completes, missing issues
        # would imply a clean run and the score climbs to 1.0.
        if conversion.status in (
            "validated", "output_generated", "loaded",
        ):
            return QualityScoreLens(
                code="validation_cleanliness", value=1.0, value_pct=100,
                weight=_WEIGHTS["validation_cleanliness"],
                details={"total_issues": 0, "status": conversion.status},
            )
        return QualityScoreLens(
            code="validation_cleanliness", value=0.0, value_pct=0,
            weight=_WEIGHTS["validation_cleanliness"],
            details={
                "reason": "No validation run yet",
                "status": conversion.status,
            },
        )

    rows = conversion.estimated_row_count or 100
    error_count = sum(1 for i in issues if (i.severity or "").lower() in ("error", "critical"))
    warning_count = sum(1 for i in issues if (i.severity or "").lower() == "warning")
    # Issue density per 100 rows; clamp.
    density_per_100 = (error_count + 0.25 * warning_count) / max(rows, 1) * 100
    # Map density → score:
    #   density 0 → 1.0
    #   density ≥ 10 → 0.0 (every 10th row has an error)
    score = max(0.0, min(1.0, 1.0 - density_per_100 / 10.0))
    return QualityScoreLens(
        code="validation_cleanliness", value=round(score, 3),
        value_pct=int(score * 100),
        weight=_WEIGHTS["validation_cleanliness"],
        details={
            "total_issues": len(issues),
            "errors": error_count,
            "warnings": warning_count,
            "density_per_100": round(density_per_100, 2),
        },
    )


def _reconciliation(db: Session, conversion: Conversion) -> QualityScoreLens:
    checks = (
        db.query(ReconciliationCheck)
        .filter(ReconciliationCheck.conversion_id == conversion.id)
        .all()
    )
    if not checks:
        return QualityScoreLens(
            code="reconciliation", value=0.0, value_pct=0,
            weight=_WEIGHTS["reconciliation"],
            details={"reason": "No reconciliation checks recorded"},
        )
    passed = sum(1 for c in checks if c.status == "pass")
    score = passed / len(checks)
    return QualityScoreLens(
        code="reconciliation", value=round(score, 3),
        value_pct=int(score * 100),
        weight=_WEIGHTS["reconciliation"],
        details={"total": len(checks), "passed": passed},
    )


def compute_for_conversion(
    db: Session, conversion: Conversion,
) -> QualityScoreResult:
    lenses = [
        _mapping_coverage(db, conversion),
        _validation_cleanliness(db, conversion),
        _reconciliation(db, conversion),
    ]
    total = round(sum(l.value * l.weight for l in lenses) * 100, 1)
    # Persist on the conversion for cheap reads downstream.
    conversion.data_quality_score = total
    db.commit()
    return QualityScoreResult(total=total, lenses=lenses)


def recompute_for_project(db: Session, project_id: int) -> dict[int, float]:
    """Convenience — recompute every conversion's score in the project
    and return ``{conversion_id: score}``. Used by the readiness score
    + CFO summary endpoints when they want a fresh snapshot."""
    convs = db.query(Conversion).filter(Conversion.project_id == project_id).all()
    out: dict[int, float] = {}
    for c in convs:
        result = compute_for_conversion(db, c)
        out[c.id] = result.total
    return out

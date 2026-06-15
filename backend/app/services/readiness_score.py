"""Migration Readiness Score — the single number every exec asks for.

Composite of five lenses, each 0..1, weighted blend → 0..5 on the
top-nav pill and 0..100 in the CFO dashboard. Each lens is computed
from existing state — no extra data entry — so the score moves the
moment the underlying signal moves.

  Gate Performance     ×  weight 0.30  — % of 7 safeguards passing
  Mapping Quality      ×  weight 0.25  — approved + required-covered
  Reconciliation       ×  weight 0.15  — % checks passing
  Completeness         ×  weight 0.20  — % conversions reached loaded/validated
  Issue Resolution     ×  weight 0.10  — open critical issues drag the score

Designed to be honest, not flattering. ``not_run`` safeguards count as
zero; missing reconciliations score as zero. The point is "what's the
chance this cutover succeeds on Saturday at 6 PM" — wishful arithmetic
won't help anyone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.conversion import Conversion
from app.models.cutover import Issue, ReconciliationCheck
from app.models.mapping import MappingSuggestion
from app.models.project import Project
from app.services.safeguards import evaluate_safeguards, safeguard_pass_rate


@dataclass
class ReadinessScore:
    total: float            # 0..5
    total_pct: int          # 0..100
    delta_2w: float         # change over the last 2 weeks (positive = improving)
    lenses: dict[str, dict[str, Any]]   # per-lens breakdown for the popover


_WEIGHTS = {
    "gate_performance":   0.30,
    "mapping_quality":    0.25,
    "reconciliation":     0.15,
    "completeness":       0.20,
    "issue_resolution":   0.10,
}


def _gate_performance(db: Session, project: Project) -> tuple[float, dict[str, Any]]:
    results = evaluate_safeguards(db, project)
    rate = safeguard_pass_rate(results)
    return rate, {
        "label": "Gate Performance",
        "value": rate,
        "value_pct": int(rate * 100),
        "weight": _WEIGHTS["gate_performance"],
        "details": {"safeguards": [r.code + ":" + r.status for r in results]},
    }


def _mapping_quality(db: Session, project: Project) -> tuple[float, dict[str, Any]]:
    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    cids = [c.id for c in convs]
    if not cids:
        return 0.0, {
            "label": "Mapping Quality",
            "value": 0.0, "value_pct": 0,
            "weight": _WEIGHTS["mapping_quality"],
            "details": {"reason": "No conversions yet"},
        }
    rows = (
        db.query(MappingSuggestion)
        .filter(MappingSuggestion.conversion_id.in_(cids))
        .all()
    )
    if not rows:
        return 0.0, {
            "label": "Mapping Quality",
            "value": 0.0, "value_pct": 0,
            "weight": _WEIGHTS["mapping_quality"],
            "details": {"reason": "No mappings yet — run AI Mapping first"},
        }
    approved = sum(1 for m in rows if m.status in ("approved", "overridden"))
    # Required-field coverage = of all (conversion, target field where required=True)
    # how many have a non-null source. Computed from the rows we have.
    required_total = sum(
        1 for m in rows
        if m.target_field and getattr(m.target_field, "required", False)
    )
    required_covered = sum(
        1 for m in rows
        if m.target_field and getattr(m.target_field, "required", False)
        and m.source_column
    )
    coverage = (required_covered / required_total) if required_total else 1.0
    approval_rate = approved / len(rows)
    # 60% approval, 40% required coverage — coverage matters slightly more
    # at cutover because missing required fields are blockers.
    score = round(0.6 * approval_rate + 0.4 * coverage, 3)
    return score, {
        "label": "Mapping Quality",
        "value": score, "value_pct": int(score * 100),
        "weight": _WEIGHTS["mapping_quality"],
        "details": {
            "total_mappings": len(rows),
            "approved": approved,
            "required_total": required_total,
            "required_covered": required_covered,
        },
    }


def _reconciliation(db: Session, project: Project) -> tuple[float, dict[str, Any]]:
    cids = [c.id for c in db.query(Conversion).filter(Conversion.project_id == project.id).all()]
    if not cids:
        return 0.0, {
            "label": "Reconciliation",
            "value": 0.0, "value_pct": 0,
            "weight": _WEIGHTS["reconciliation"],
            "details": {"reason": "No conversions yet"},
        }
    checks = db.query(ReconciliationCheck).filter(
        ReconciliationCheck.conversion_id.in_(cids),
    ).all()
    if not checks:
        return 0.0, {
            "label": "Reconciliation",
            "value": 0.0, "value_pct": 0,
            "weight": _WEIGHTS["reconciliation"],
            "details": {"reason": "No reconciliation checks run yet"},
        }
    passed = sum(1 for c in checks if c.status == "pass")
    rate = passed / len(checks)
    return rate, {
        "label": "Reconciliation",
        "value": rate, "value_pct": int(rate * 100),
        "weight": _WEIGHTS["reconciliation"],
        "details": {"total": len(checks), "passed": passed},
    }


def _completeness(db: Session, project: Project) -> tuple[float, dict[str, Any]]:
    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    if not convs:
        return 0.0, {
            "label": "Completeness",
            "value": 0.0, "value_pct": 0,
            "weight": _WEIGHTS["completeness"],
            "details": {"reason": "No conversions yet"},
        }
    advanced = sum(
        1 for c in convs
        if c.status in ("validated", "output_generated", "loaded")
    )
    status_score = advanced / len(convs)
    # Blend in the per-conversion Data Quality Score (persisted on
    # ``Conversion.data_quality_score``). Each conversion's DQ is 0..100;
    # average across conversions and convert to 0..1.
    dq_values = [
        (c.data_quality_score or 0.0) for c in convs
    ]
    avg_dq = (sum(dq_values) / len(dq_values) / 100.0) if dq_values else 0.0
    # 60% status progression + 40% DQ score — status climbs as the team
    # advances, DQ keeps it honest about how clean the underlying data
    # actually is.
    score = round(0.6 * status_score + 0.4 * avg_dq, 3)
    return score, {
        "label": "Completeness",
        "value": score, "value_pct": int(score * 100),
        "weight": _WEIGHTS["completeness"],
        "details": {
            "total_conversions": len(convs),
            "advanced": advanced,
            "loaded": sum(1 for c in convs if c.status == "loaded"),
            "avg_data_quality": int(avg_dq * 100),
        },
    }


def _issue_resolution(db: Session, project: Project) -> tuple[float, dict[str, Any]]:
    issues = db.query(Issue).filter(Issue.project_id == project.id).all()
    if not issues:
        return 1.0, {
            "label": "Issue Resolution",
            "value": 1.0, "value_pct": 100,
            "weight": _WEIGHTS["issue_resolution"],
            "details": {"open": 0, "critical_open": 0},
        }
    open_issues = [i for i in issues if i.status not in ("resolved", "wont_fix")]
    critical_open = [i for i in open_issues if i.severity == "critical"]
    high_open = [i for i in open_issues if i.severity == "high"]
    # Score: every critical drops the score 0.2, every high drops 0.05.
    # No way to recover below 0.
    score = max(0.0, 1.0 - 0.2 * len(critical_open) - 0.05 * len(high_open))
    return score, {
        "label": "Issue Resolution",
        "value": score, "value_pct": int(score * 100),
        "weight": _WEIGHTS["issue_resolution"],
        "details": {
            "open": len(open_issues),
            "critical_open": len(critical_open),
            "high_open": len(high_open),
        },
    }


def compute_readiness(db: Session, project: Project) -> ReadinessScore:
    lenses_compute = {
        "gate_performance": _gate_performance(db, project),
        "mapping_quality":  _mapping_quality(db, project),
        "reconciliation":   _reconciliation(db, project),
        "completeness":     _completeness(db, project),
        "issue_resolution": _issue_resolution(db, project),
    }
    weighted = 0.0
    lens_payload: dict[str, dict[str, Any]] = {}
    for code, (value, payload) in lenses_compute.items():
        weighted += value * _WEIGHTS[code]
        lens_payload[code] = payload
    score_5 = round(weighted * 5.0, 1)
    score_100 = int(round(weighted * 100))

    # Delta vs. 2 weeks ago — for v1 we can't reconstruct historic state,
    # so we surface 0.0 honestly. (A later slice persists daily snapshots
    # of the score for trend reporting.)
    delta = 0.0

    return ReadinessScore(
        total=score_5,
        total_pct=score_100,
        delta_2w=delta,
        lenses=lens_payload,
    )

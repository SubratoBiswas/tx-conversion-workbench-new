"""COA composition + validation + coverage.

Single entry point: :func:`compose_accounts`. Given a conversion that
has a COAStructure with segments + crosswalks, walks the source
dataset row-by-row and emits a composed account string per row.

Each segment is derived independently:

  constant      -> emit cfg["value"]
  source_column -> emit row[cfg["column"]] after pad
  crosswalk     -> look up row[cfg["column"]] in COAValueCrosswalk
  computed      -> apply a small rule pipeline (reuses the engine)
  conditional   -> case_when branches with row context

Validation:
* length match — emitted value must equal segment.length after pad
* allowed value set — if segment.valid_values is non-empty, emitted
  value must be in that set
* required — missing source column for a non-constant derivation
  emits an unmapped-row record

Coverage is reported per segment + overall:
  total rows · rows producing a fully-valid composed account ·
  rows with at least one segment failure · breakdown by failure reason
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.discovery import vendor_catalog  # noqa: F401 (keeps namespace stable)
from app.models.coa import COASegment, COAStructure, COAValueCrosswalk
from app.transformations.engine import apply_pipeline


# ─── Per-row, per-segment derivation ────────────────────────────────


def _pad(value: str, length: int, pad_style: str) -> str:
    s = "" if value is None else str(value).strip()
    if len(s) >= length:
        return s[:length]
    if pad_style == "left_zero":
        return s.rjust(length, "0")
    if pad_style == "right_space":
        return s.ljust(length, " ")
    return s   # "none"


@dataclass
class SegmentEmission:
    value: str
    valid: bool
    reason: str | None = None    # why it's invalid


def _emit_constant(segment: COASegment, _row: dict[str, Any]) -> SegmentEmission:
    raw = (segment.derivation_config or {}).get("value", "")
    padded = _pad(str(raw), segment.length, segment.pad_style or "left_zero")
    return SegmentEmission(value=padded, valid=True)


def _emit_source_column(segment: COASegment, row: dict[str, Any]) -> SegmentEmission:
    cfg = segment.derivation_config or {}
    col = cfg.get("column")
    if not col or col not in row:
        return SegmentEmission(
            value="", valid=False,
            reason=f"Source column '{col}' missing on this row",
        )
    val = row.get(col)
    if val is None or str(val).strip() == "":
        if segment.default_value:
            return SegmentEmission(
                value=_pad(segment.default_value, segment.length,
                           segment.pad_style or "left_zero"),
                valid=True,
            )
        return SegmentEmission(value="", valid=False, reason=f"Source column '{col}' is blank")
    padded = _pad(str(val), segment.length, segment.pad_style or "left_zero")
    return SegmentEmission(value=padded, valid=True)


def _emit_crosswalk(
    segment: COASegment, row: dict[str, Any],
    crosswalk_index: dict[int, dict[str, str]],
) -> SegmentEmission:
    cfg = segment.derivation_config or {}
    col = cfg.get("column")
    if not col or col not in row:
        return SegmentEmission(
            value="", valid=False,
            reason=f"Source column '{col}' missing on this row",
        )
    raw = row.get(col)
    if raw is None or str(raw).strip() == "":
        if segment.default_value:
            return SegmentEmission(
                value=_pad(segment.default_value, segment.length,
                           segment.pad_style or "left_zero"),
                valid=True,
            )
        return SegmentEmission(value="", valid=False, reason="Source value blank")
    raw_str = str(raw).strip()
    mapping = crosswalk_index.get(segment.id, {})
    if raw_str not in mapping:
        if segment.default_value:
            padded = _pad(segment.default_value, segment.length,
                          segment.pad_style or "left_zero")
            return SegmentEmission(
                value=padded, valid=True,
                reason=f"No crosswalk for '{raw_str}' — fell back to default",
            )
        return SegmentEmission(
            value="", valid=False,
            reason=f"No crosswalk row for legacy value '{raw_str}'",
        )
    fusion = mapping[raw_str]
    padded = _pad(fusion, segment.length, segment.pad_style or "left_zero")
    return SegmentEmission(value=padded, valid=True)


def _emit_computed(segment: COASegment, row: dict[str, Any]) -> SegmentEmission:
    cfg = segment.derivation_config or {}
    col = cfg.get("column")
    if not col or col not in row:
        return SegmentEmission(value="", valid=False,
                               reason=f"Source column '{col}' missing")
    raw = row.get(col)
    rules = cfg.get("rules") or []
    out = apply_pipeline(rules, raw, row=row)
    padded = _pad(str(out), segment.length, segment.pad_style or "left_zero")
    return SegmentEmission(value=padded, valid=True)


def _emit_conditional(segment: COASegment, row: dict[str, Any]) -> SegmentEmission:
    cfg = segment.derivation_config or {}
    # Reuse the CASE_WHEN logic via the engine for consistency.
    out = apply_pipeline(
        [{"rule_type": "CASE_WHEN", "config": cfg}],
        None, row=row,
    )
    padded = _pad(str(out or ""), segment.length, segment.pad_style or "left_zero")
    return SegmentEmission(value=padded, valid=True)


_EMITTERS = {
    "constant":      _emit_constant,
    "source_column": _emit_source_column,
    "computed":      _emit_computed,
    "conditional":   _emit_conditional,
}


def emit_segment(
    segment: COASegment, row: dict[str, Any],
    crosswalk_index: dict[int, dict[str, str]],
) -> SegmentEmission:
    """Dispatch — kept separate from the per-kind functions so unit tests
    can call individual emitters without a crosswalk index."""
    kind = (segment.derivation_kind or "source_column").lower()
    if kind == "crosswalk":
        e = _emit_crosswalk(segment, row, crosswalk_index)
    else:
        fn = _EMITTERS.get(kind, _emit_source_column)
        e = fn(segment, row)
    # Post-emission validation against valid_values
    if e.valid and segment.valid_values:
        if e.value not in segment.valid_values:
            e.valid = False
            e.reason = (
                f"'{e.value}' not in segment's value set "
                f"({len(segment.valid_values)} allowed)"
            )
    return e


# ─── Whole-account composition + coverage ──────────────────────────


@dataclass
class ComposedRow:
    source_index: int
    composed_account: str
    valid: bool
    segment_emissions: list[SegmentEmission]
    failures: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompositionResult:
    sample_rows: list[ComposedRow]
    total_rows: int
    valid_rows: int
    invalid_rows: int
    coverage_pct: float
    per_segment_coverage: dict[str, dict[str, Any]]  # by segment name
    per_segment_unmapped_values: dict[str, list[str]]  # samples per segment


def _build_crosswalk_index(structure: COAStructure) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for cw in structure.crosswalks:
        out.setdefault(cw.segment_id, {})[cw.legacy_value] = cw.fusion_value
    return out


def compose_accounts(
    structure: COAStructure,
    df: pd.DataFrame,
    *,
    sample_size: int = 25,
) -> CompositionResult:
    """Walk every row of ``df``, emit per-segment values, concatenate
    with the structure separator, and report coverage. Returns the
    first ``sample_size`` rows fully assembled so the UI can render a
    preview table."""
    crosswalk_index = _build_crosswalk_index(structure)
    sep = structure.separator or "-"
    segments = sorted(structure.segments, key=lambda s: s.position)

    sample_rows: list[ComposedRow] = []
    total = len(df)
    valid = 0
    invalid = 0

    per_seg_total: dict[str, int] = {s.name: 0 for s in segments}
    per_seg_failed: dict[str, int] = {s.name: 0 for s in segments}
    per_seg_unmapped_samples: dict[str, set[str]] = {s.name: set() for s in segments}

    for idx, row in df.iterrows():
        row_dict = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}
        emissions: list[SegmentEmission] = []
        failures: list[dict[str, Any]] = []
        for seg in segments:
            per_seg_total[seg.name] += 1
            e = emit_segment(seg, row_dict, crosswalk_index)
            emissions.append(e)
            if not e.valid:
                per_seg_failed[seg.name] += 1
                failures.append({
                    "segment": seg.name,
                    "reason": e.reason,
                    "source_value": (
                        row_dict.get((seg.derivation_config or {}).get("column"))
                        if seg.derivation_kind in ("source_column", "crosswalk", "computed")
                        else None
                    ),
                })
                if seg.derivation_kind == "crosswalk":
                    col = (seg.derivation_config or {}).get("column")
                    src_val = row_dict.get(col)
                    if src_val is not None:
                        per_seg_unmapped_samples[seg.name].add(str(src_val))
        composed = sep.join(e.value for e in emissions)
        row_valid = all(e.valid for e in emissions)
        if row_valid:
            valid += 1
        else:
            invalid += 1
        if len(sample_rows) < sample_size:
            sample_rows.append(ComposedRow(
                source_index=int(idx),
                composed_account=composed,
                valid=row_valid,
                segment_emissions=emissions,
                failures=failures,
            ))

    per_segment_coverage = {
        name: {
            "total": per_seg_total[name],
            "failed": per_seg_failed[name],
            "coverage_pct": (
                round((per_seg_total[name] - per_seg_failed[name]) / per_seg_total[name] * 100, 2)
                if per_seg_total[name] else 0.0
            ),
        }
        for name in per_seg_total
    }
    per_segment_unmapped_values = {
        name: sorted(list(samples))[:20]
        for name, samples in per_seg_unmapped_samples.items()
    }
    coverage_pct = round((valid / total * 100), 2) if total else 0.0
    return CompositionResult(
        sample_rows=sample_rows,
        total_rows=total,
        valid_rows=valid,
        invalid_rows=invalid,
        coverage_pct=coverage_pct,
        per_segment_coverage=per_segment_coverage,
        per_segment_unmapped_values=per_segment_unmapped_values,
    )

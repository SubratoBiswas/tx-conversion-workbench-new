"""Mapping suggestion schemas."""
from datetime import datetime
from typing import Any
from pydantic import BaseModel


class MappingOut(BaseModel):
    id: int
    conversion_id: int
    target_field_id: int
    target_field_name: str | None = None
    target_required: bool = False
    target_data_type: str | None = None
    target_max_length: int | None = None
    source_column: str | None = None
    confidence: float = 0.0
    reason: str | None = None
    suggested_transformation: dict[str, Any] | None = None
    review_required: int = 1
    status: str
    default_value: str | None = None
    comment: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    # P6 — dual-cert state surfaces on the Mapping Inspector so analysts
    # can see who already signed off and that a *second* (different) user
    # is required before the row is marked ``approved``.
    requires_dual_approval: int = 0
    second_approver_email: str | None = None
    second_approved_at: datetime | None = None
    sample_source_values: list[Any] = []
    sample_converted_values: list[Any] = []
    # Cross-source Mapping Knowledge Bank provenance. When ``kb_source`` is
    # set the row was pre-populated from a prior project on the same
    # source ERP — the UI renders a "🧠 from {Source} KB" badge.
    kb_source: str | None = None
    kb_origin_project_id: int | None = None
    kb_times_reused: int | None = 0

    class Config:
        from_attributes = True


class MappingUpdate(BaseModel):
    source_column: str | None = None
    suggested_transformation: dict[str, Any] | None = None
    default_value: str | None = None
    comment: str | None = None
    status: str | None = None  # approved | rejected | overridden | not_applicable | suggested

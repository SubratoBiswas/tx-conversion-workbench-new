"""Pydantic schemas for the learned-mappings registry."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class LearnedMappingBase(BaseModel):
    kind: str
    category: str
    original_value: str
    resolved_value: str
    target_object: Optional[str] = None
    target_field: Optional[str] = None
    rule_type: Optional[str] = None
    rule_config: Optional[Any] = None
    project_id: Optional[int] = None
    captured_from: Optional[str] = None
    confidence_boost: Optional[float] = 0.26
    records_auto_fixed: Optional[int] = 0
    # Cross-project Mapping Knowledge Bank fields (Slice 2). Surfacing them
    # here lets the Learning Center filter/inspect by source ERP and lets
    # the reuse-stat fields drive the "🧠 from {Source} KB · 47 prior
    # reuses" badge in the Mapping Review inspector.
    source_system: Optional[str] = None
    originated_in_project_id: Optional[int] = None
    times_reused: Optional[int] = 0
    last_reused_at: Optional[datetime] = None
    last_reused_in_project_id: Optional[int] = None


class LearnedMappingCreate(LearnedMappingBase):
    pass


class LearnedMappingOut(LearnedMappingBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    captured_by: Optional[str] = None
    captured_at: datetime


class LearningStats(BaseModel):
    total: int
    avg_confidence_boost: float
    records_auto_fixed: int
    analyst_minutes_saved: int
    by_category: list[dict[str, Any]]

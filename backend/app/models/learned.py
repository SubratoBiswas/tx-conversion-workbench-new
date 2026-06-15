"""Learned mappings registry — captures human-approved rules so future cycles
can auto-apply them. Drives the Learning Center, Rule Library, and Crosswalk
Library pages.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String

from app.database import Base


class LearnedMapping(Base):
    __tablename__ = "learned_mappings"

    id = Column(Integer, primary_key=True)
    # Coarse type for filtering: "column_mapping" | "value_translation" |
    # "rule" | "crosswalk"
    kind = Column(String, nullable=False, index=True)
    # Sub-category drives the Learning Center category cards. Examples:
    #   "Column Mapping Alias", "SKU Format Alias", "UOM Conversion Rule",
    #   "Status Value Mapping", "Date Format Rule", "Customer Alias",
    #   "Supplier Alias", "Currency Mapping", "Organization Code Mapping",
    #   "Branch Code Mapping"
    category = Column(String, nullable=False, index=True)

    # The thing learned: original (legacy) value/column → resolved (canonical) value/column
    original_value = Column(String, nullable=False)
    resolved_value = Column(String, nullable=False)

    # Optional context
    target_object = Column(String)   # e.g. "Item", "Customer", "Sales Order"
    target_field = Column(String)    # FBDI field name when applicable
    rule_type = Column(String)       # e.g. "REMOVE_HYPHEN", "VALUE_MAP"
    rule_config = Column(JSON)       # JSON config the engine can re-apply

    # Provenance — which project the mapping was first taught in.
    project_id = Column(Integer, ForeignKey("projects.id"))
    originated_in_project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    captured_from = Column(String)   # human-readable origin label
    captured_by = Column(String)     # approver email
    captured_at = Column(DateTime, default=datetime.utcnow)

    # Cross-project Mapping Knowledge Base key: the source system this
    # mapping was taught against. Denormalized from Project.source_system so
    # cross-project lookups don't need a join. All mappings with the same
    # ``source_system`` form the per-source Knowledge Base — pre-populated on
    # suggest-mapping for any future project with the same source.
    source_system = Column(String(50), nullable=True, index=True)

    # Reuse stats — drive the "🧠 from EBS-KB · 47 prior reuses" badge in
    # the Mapping Review UI and a confidence-decay rule for stale mappings.
    times_reused = Column(Integer, default=0)
    last_reused_at = Column(DateTime, nullable=True)
    last_reused_in_project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)

    # Impact metrics
    confidence_boost = Column(Float, default=0.26)
    records_auto_fixed = Column(Integer, default=0)

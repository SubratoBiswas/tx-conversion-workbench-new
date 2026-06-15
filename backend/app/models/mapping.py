"""AI / rule-based mapping suggestions for a project."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float, Text, JSON
from sqlalchemy.orm import relationship
from app.database import Base


MAPPING_STATUSES = ("suggested", "approved", "rejected", "overridden", "not_applicable")


class MappingSuggestion(Base):
    __tablename__ = "mapping_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    conversion_id = Column(Integer, ForeignKey("conversions.id", ondelete="CASCADE"), nullable=False)
    target_field_id = Column(Integer, ForeignKey("fbdi_fields.id"), nullable=False)
    source_column = Column(String(255), nullable=True)
    confidence = Column(Float, default=0.0)  # 0..1
    reason = Column(Text)
    suggested_transformation = Column(JSON, nullable=True)  # {rule_type, config}
    review_required = Column(Integer, default=1)  # 0/1 flag
    status = Column(String(50), default="suggested")
    default_value = Column(String(500), nullable=True)
    comment = Column(Text, nullable=True)
    approved_by = Column(String(150), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Cross-source-system Mapping Knowledge Base provenance.
    #
    # ``kb_source`` is the source-system code of the LearnedMapping that
    # pre-populated this suggestion (e.g. "netsuite", "oracle_ebs"). When
    # set, the UI renders a "🧠 from NetSuite KB" badge and the run-AI
    # toast counts it toward "N pre-filled from Knowledge Bank". null
    # means the suggestion came from the AI engine (or was authored
    # manually). The denormalized ``kb_origin_project_id`` lets the
    # inspector show "captured in {ProjectName}" without a join.
    kb_source = Column(String(50), nullable=True)
    kb_origin_project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    kb_times_reused = Column(Integer, default=0)

    # Slice 6 — Dual certification for sensitive mappings (COA segments,
    # customer banking, etc.). When ``requires_dual_approval = 1`` the
    # mapping needs two distinct approvers; the second sign-off lands on
    # ``second_approver_email`` + ``second_approved_at``. The Dual Cert
    # safeguard checks every flagged row has both.
    requires_dual_approval = Column(Integer, default=0)
    second_approver_email = Column(String(200), nullable=True)
    second_approved_at = Column(DateTime, nullable=True)

    conversion = relationship("Conversion", back_populates="mappings")
    target_field = relationship("FBDIField")

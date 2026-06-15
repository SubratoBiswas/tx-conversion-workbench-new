"""Dataset and column-profile models."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Text, Float
from sqlalchemy.orm import relationship
from app.database import Base


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    file_name = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_type = Column(String(20), nullable=False)  # csv | xlsx
    row_count = Column(Integer, default=0)
    column_count = Column(Integer, default=0)
    status = Column(String(50), default="profiled")  # uploaded | profiling | profiled | error
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    # Source system this extract came from. Inherited from the conversion's
    # project when the dataset is uploaded; can be set explicitly via the
    # upload payload. Drives the source-aware column-name normalizer used
    # by the cross-project Mapping Knowledge Base.
    source_system = Column(String(50), nullable=True, index=True)
    # Free-text source label visible to analysts ("Apr 30 monthly extract",
    # "v2024.2.1 production snapshot"). Not used for matching.
    source_label = Column(String(255), nullable=True)

    columns = relationship(
        "DatasetColumnProfile", back_populates="dataset", cascade="all, delete-orphan"
    )


class DatasetColumnProfile(Base):
    __tablename__ = "dataset_column_profiles"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    column_name = Column(String(255), nullable=False)
    position = Column(Integer, default=0)
    inferred_type = Column(String(50))  # string | integer | float | date | boolean
    null_count = Column(Integer, default=0)
    null_percent = Column(Float, default=0.0)
    distinct_count = Column(Integer, default=0)
    sample_values = Column(JSON, default=list)
    min_value = Column(String(255), nullable=True)
    max_value = Column(String(255), nullable=True)
    pattern_summary = Column(String(500), nullable=True)

    # Slice 6 — PII / sensitivity flag. Drives the GDPR + SOX safeguards
    # and the Mapping Review's "🔒 PII" badge so analysts know which
    # columns need pseudonymisation / restricted handling.
    contains_pii = Column(Integer, default=0)
    # Category: "PII" (personal), "PHI" (health), "PCI" (cardholder),
    # "FIN" (financial), "GOVT" (government identifier). null when not
    # sensitive.
    pii_category = Column(String(50), nullable=True)

    dataset = relationship("Dataset", back_populates="columns")

"""Chart-of-Accounts (COA) Engine models.

COA mapping is the single most painful conversion in an Oracle Fusion
Finance migration. Real engagements spend weeks composing the
N-segment account string from legacy data while crosswalking individual
segment values against new value sets.

Three concepts:

* **COAStructure** — per-conversion definition of the target Fusion COA:
  ordered list of segments, separator, total length, lock-down flag.
  One structure per conversion (the GL Coding Combinations
  conversion).
* **COASegment** — one segment in the structure: name (Company /
  CostCenter / NaturalAccount / SubAccount / Product etc.), position,
  length, derivation_kind (constant / source_column / conditional /
  computed). A segment may reference one or more source columns and an
  optional default value for unmapped rows.
* **COAValueCrosswalk** — per-segment value mapping table. Each row
  rewrites a legacy value to a Fusion value with optional notes and
  a sign-off-by stamp for audit. Bulk-upload via CSV is the typical
  authoring path.

Composition runs the derivation per segment for each source row,
concatenates with the structure's separator, and reports coverage:
% of source rows producing a fully-valid composed account.

Production-grade notes:

* Audit-friendly — every crosswalk row carries ``created_by`` +
  ``approved_by`` so the immutable ledger can show "Customer Acct 4001
  signed off by CFO on 2026-08-12".
* Idempotent CSV uploads — rows are upserted by (segment_id,
  legacy_value); re-uploading the same file refreshes rather than
  duplicates.
* Lock-down — once ``COAStructure.locked = 1`` the structure can't be
  edited (only by an admin with audit reason). Crosswalk rows can
  still be added.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


COA_DERIVATION_KINDS = (
    "constant",        # segment always emits a fixed value
    "source_column",   # segment value = source row's column
    "crosswalk",       # source_column value translated via COAValueCrosswalk
    "computed",        # source_column transformed (uppercase / pad / substring)
    "conditional",     # multi-branch CASE_WHEN on row context
)


class COAStructure(Base):
    __tablename__ = "coa_structures"

    id = Column(Integer, primary_key=True, index=True)
    conversion_id = Column(
        Integer, ForeignKey("conversions.id", ondelete="CASCADE"),
        nullable=False, index=True, unique=True,
    )
    name = Column(String(150), default="Fusion COA Structure")
    separator = Column(String(4), default="-")
    target_ledger = Column(String(120), nullable=True)  # "USCOA" / "INTL_COA"
    description = Column(Text, nullable=True)
    locked = Column(Boolean, default=False)
    locked_at = Column(DateTime, nullable=True)
    locked_by = Column(String(150), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    segments = relationship(
        "COASegment", back_populates="structure",
        cascade="all, delete-orphan", order_by="COASegment.position",
    )
    crosswalks = relationship(
        "COAValueCrosswalk", back_populates="structure",
        cascade="all, delete-orphan",
    )


class COASegment(Base):
    __tablename__ = "coa_segments"

    id = Column(Integer, primary_key=True, index=True)
    structure_id = Column(
        Integer, ForeignKey("coa_structures.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    position = Column(Integer, nullable=False)        # 1..N — drives concat order
    name = Column(String(120), nullable=False)        # "Company" / "CostCenter" / ...
    length = Column(Integer, nullable=False)          # expected character length
    derivation_kind = Column(String(32), nullable=False, default="source_column")
    # Source-side hooks. Schema depends on derivation_kind:
    #   constant      — {"value": "01"}
    #   source_column — {"column": "DEPT_CODE"}
    #   crosswalk     — {"column": "DEPT_CODE"}   (value gets looked up)
    #   computed      — {"column": "DEPT_CODE", "rules": [{rule_type, config}, ...]}
    #   conditional   — {"branches": [{conditions, then}], "default": "..."}
    derivation_config = Column(JSON, default=dict)

    # Default emitted when the derivation yields blank — typical for
    # optional segments like SubAccount that fall back to "0000".
    default_value = Column(String(120), nullable=True)
    # Allowed value set — when populated, a composed value not in this
    # set is reported as a coverage gap. List of strings.
    valid_values = Column(JSON, default=list)
    # Pad / fill style for short values: "left_zero" / "right_space" / "none".
    pad_style = Column(String(16), default="left_zero")
    description = Column(Text, nullable=True)

    structure = relationship("COAStructure", back_populates="segments")


class COAValueCrosswalk(Base):
    __tablename__ = "coa_value_crosswalks"

    id = Column(Integer, primary_key=True, index=True)
    structure_id = Column(
        Integer, ForeignKey("coa_structures.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    segment_id = Column(
        Integer, ForeignKey("coa_segments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    legacy_value = Column(String(255), nullable=False, index=True)
    fusion_value = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    # Sign-off provenance — drives the "12 / 1247 accounts dual-signed"
    # KPI in the COA Sign-off card.
    approved_by = Column(String(150), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_by = Column(String(150), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    structure = relationship("COAStructure", back_populates="crosswalks")
    segment = relationship("COASegment")

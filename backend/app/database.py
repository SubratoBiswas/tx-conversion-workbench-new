"""SQLAlchemy database setup."""
import os
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import settings

# For SQLite paths like sqlite:////app/data/workbench.db, ensure the parent dir exists.
if settings.DATABASE_URL.startswith("sqlite"):
    parsed = urlparse(settings.DATABASE_URL)
    db_path = parsed.path  # e.g. "/app/data/workbench.db" or "./workbench.db"
    if db_path and db_path != "/:memory:":
        # SQLAlchemy treats sqlite:///./x.db as relative; sqlite:////x.db as absolute.
        parent = os.path.dirname(db_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, echo=False, future=True)

# NOTE: SQLite ships with foreign-key enforcement OFF by default and we
# deliberately keep it off. Several models hold "advisory" FKs (audit
# events, learned-mapping provenance) that need to outlive their
# referent project — we'd otherwise have to add SET NULL clauses on
# every column. The cascade contract is enforced at the SQLAlchemy
# relationship layer instead (cascade="all, delete-orphan").
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db() -> Session:
    """FastAPI dependency yielding a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Import all models so they register on Base.metadata
    from app.models import (  # noqa: F401
        user, dataset, fbdi, project, conversion, environment, mapping,
        transformation, validation as validation_model, output, load,
        workflow, dependency, learned, source_connection, audit, discovery,
        cutover, reconciliation, coa,
    )
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """Best-effort additive ALTER TABLEs for columns added after a release.

    SQLAlchemy ``create_all`` only adds new tables — it doesn't ALTER existing
    ones. For the few cases where we add a nullable column to a table that
    already exists in a dev database, we apply the change here. SQLite happily
    accepts adding nullable columns and ignores duplicates with try/except.

    Keep this list in sync with the ORM model. Order does not matter; each
    ALTER is independent and failures are swallowed (covers re-runs and
    fresh-create-all where the column was already shipped in the new table).
    """
    from sqlalchemy import text
    additions: list[tuple[str, str, str]] = [
        # (table, column, type)
        ("load_errors",       "reference_value",            "VARCHAR(255)"),
        # Slice 1 — source-system foundation
        ("projects",          "source_system",              "VARCHAR(50)"),
        ("projects",          "phase",                      "VARCHAR(20) DEFAULT 'blueprint'"),
        ("datasets",          "source_system",              "VARCHAR(50)"),
        ("datasets",          "source_label",               "VARCHAR(255)"),
        ("learned_mappings",  "source_system",              "VARCHAR(50)"),
        ("learned_mappings",  "originated_in_project_id",   "INTEGER"),
        ("learned_mappings",  "times_reused",               "INTEGER DEFAULT 0"),
        ("learned_mappings",  "last_reused_at",             "DATETIME"),
        ("learned_mappings",  "last_reused_in_project_id",  "INTEGER"),
        # Slice 2 — Mapping Knowledge Base provenance on suggestions
        ("mapping_suggestions", "kb_source",                "VARCHAR(50)"),
        ("mapping_suggestions", "kb_origin_project_id",     "INTEGER"),
        ("mapping_suggestions", "kb_times_reused",          "INTEGER DEFAULT 0"),
        # Slice 6 — Cutover & Exec layer
        ("mapping_suggestions",        "requires_dual_approval",  "INTEGER DEFAULT 0"),
        ("mapping_suggestions",        "second_approver_email",   "VARCHAR(200)"),
        ("mapping_suggestions",        "second_approved_at",      "DATETIME"),
        ("load_runs",                  "environment",             "VARCHAR(20) DEFAULT 'DEV'"),
        ("load_runs",                  "environment_sequence",    "INTEGER DEFAULT 1"),
        ("conversions",                "data_quality_score",      "FLOAT DEFAULT 0.0"),
        ("conversions",                "estimated_row_count",     "INTEGER"),
        ("conversions",                "actual_row_count",        "INTEGER"),
        ("conversions",                "throughput_rows_per_min", "FLOAT"),
        ("dataset_column_profiles",    "contains_pii",            "INTEGER DEFAULT 0"),
        ("dataset_column_profiles",    "pii_category",            "VARCHAR(50)"),
        ("projects",                   "dress_rehearsal_count",   "INTEGER DEFAULT 0"),
        ("projects",                   "current_environment",     "VARCHAR(20) DEFAULT 'DEV'"),
        ("projects",                   "selected_modules",        "TEXT"),
    ]
    with engine.begin() as conn:
        for table, column, coltype in additions:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
            except Exception:
                # Column already exists, or table doesn't yet — both are safe to skip.
                pass

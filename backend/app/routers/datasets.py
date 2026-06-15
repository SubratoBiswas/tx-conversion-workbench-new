"""Dataset endpoints."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.dataset import Dataset, DatasetColumnProfile
from app.models.user import User
from app.schemas.dataset import DatasetDetailOut, DatasetOut, DatasetPreviewOut
from app.services.audit_service import record_event
from app.services.auth_service import get_current_user
from app.services.dataset_service import create_dataset_from_upload, get_dataset_preview


def _client_ip(req: Request | None) -> str | None:
    if req is None: return None
    return (req.headers.get("x-forwarded-for") or req.client.host) if req.client else None


def _ua(req: Request | None) -> str | None:
    return req.headers.get("user-agent") if req else None

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.post("/upload", response_model=DatasetDetailOut)
def upload_dataset(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    try:
        ds = create_dataset_from_upload(db, file, name, description)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return ds


@router.get("", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Dataset).order_by(Dataset.uploaded_at.desc()).all()


@router.get("/{dataset_id}", response_model=DatasetDetailOut)
def get_dataset(dataset_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds


@router.get("/{dataset_id}/preview", response_model=DatasetPreviewOut)
def preview_dataset(
    dataset_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return get_dataset_preview(ds, limit=limit)


@router.get("/{dataset_id}/profile", response_model=DatasetDetailOut)
def get_profile(dataset_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds


# ─── P3: PII / sensitivity flag on column profile ────────────────────


class ColumnPIIUpdate(BaseModel):
    contains_pii: bool
    pii_category: str | None = None   # "PII" | "PHI" | "PCI" | "FIN" | "GOVT"


_VALID_PII_CATEGORIES = {"PII", "PHI", "PCI", "FIN", "GOVT"}


@router.patch("/columns/{column_id}/pii", response_model=DatasetDetailOut)
def update_column_pii(
    column_id: int,
    payload: ColumnPIIUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Flag a dataset column as carrying sensitive data. Drives the
    Mapping Review's 🔒 PII badge so analysts know which source columns
    must be pseudonymised / restricted before they flow to Fusion.

    Every toggle is audited — auditors require an immutable trail of who
    classified what, when, for SOX / HIPAA / GDPR review."""
    col = (
        db.query(DatasetColumnProfile)
        .filter(DatasetColumnProfile.id == column_id)
        .first()
    )
    if not col:
        raise HTTPException(404, "Column profile not found")
    if payload.contains_pii and payload.pii_category and payload.pii_category not in _VALID_PII_CATEGORIES:
        raise HTTPException(
            400,
            f"Unknown pii_category. Valid: {sorted(_VALID_PII_CATEGORIES)}",
        )
    prior_flag = int(col.contains_pii or 0)
    prior_cat  = col.pii_category
    col.contains_pii = 1 if payload.contains_pii else 0
    col.pii_category = payload.pii_category if payload.contains_pii else None
    db.commit()
    db.refresh(col)
    # Audit — every PII classification change is a compliance-relevant
    # event. We capture before/after so an auditor can answer "who
    # marked field X as PHI on date Y and why" without diff-mining the
    # row history.
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="dataset_column",
        target_id=col.id,
        project_id=None,
        summary=(
            f"PII flag {'set' if col.contains_pii else 'cleared'} on "
            f"'{col.dataset.name}.{col.column_name}'"
            + (f" ({col.pii_category})" if col.pii_category else "")
        ),
        details={
            "dataset_id":   col.dataset_id,
            "dataset_name": col.dataset.name,
            "column_name":  col.column_name,
            "from": {"contains_pii": prior_flag, "pii_category": prior_cat},
            "to":   {"contains_pii": int(col.contains_pii or 0), "pii_category": col.pii_category},
        },
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    # Return the parent dataset so the UI can refresh its column list
    # in one hop.
    return col.dataset

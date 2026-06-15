"""Projects (engagement) router.

A Project is the implementation engagement (e.g. "Acme SCM Phase 1") that
contains many Conversion objects. Each project pins a single ``source_system``
(NetSuite / Oracle EBS / ...) — set at creation via the Setup Wizard — which
keys the cross-project Mapping Knowledge Base lookup and drives which
Discovery scanner runs against the project's SourceConnection.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.conversion import Conversion
from app.models.project import Project
from app.models.source_connection import SourceConnection
from app.models.user import User
from app.schemas.conversion import ConversionOut
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.schemas.source_connection import SourceConnectionCreate
from app.services.audit_service import record_event
from app.services.auth_service import get_current_user
from app.services.connection_service import create_connection
from app.source_systems import VALID_CODES, normalize_code

router = APIRouter(prefix="/api/projects", tags=["projects"])


VALID_PHASES = ("blueprint", "own", "lift", "thrive")


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _hydrate(db: Session, p: Project) -> ProjectOut:
    """Compute conversion + connection roll-ups for the engagement card view."""
    convs = db.query(Conversion).filter(Conversion.project_id == p.id).all()
    in_progress = sum(
        1 for c in convs
        if c.status in ("draft", "mapping_suggested", "awaiting_approval", "validated", "output_generated")
    )
    loaded = sum(1 for c in convs if c.status == "loaded")
    failed = sum(1 for c in convs if c.status == "failed")
    connections = (
        db.query(SourceConnection).filter(SourceConnection.project_id == p.id).all()
    )
    has_active = any(c.status in ("ok", "degraded") for c in connections)

    out = ProjectOut.model_validate(p)
    out.conversion_count = len(convs)
    out.in_progress_count = in_progress
    out.loaded_count = loaded
    out.failed_count = failed
    out.source_connection_count = len(connections)
    out.has_active_source_connection = has_active
    return out


def _validate_source_system(value: str | None) -> str | None:
    if value is None:
        return None
    code = normalize_code(value)
    if not code:
        raise HTTPException(
            400, f"Unknown source_system '{value}'. Valid codes: {sorted(VALID_CODES)}",
        )
    return code


def _validate_phase(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in VALID_PHASES:
        raise HTTPException(400, f"Unknown phase '{value}'. Valid: {VALID_PHASES}")
    return value


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [_hydrate(db, p) for p in db.query(Project).order_by(Project.id.desc()).all()]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    return _hydrate(db, p)


@router.post("", response_model=ProjectOut)
def create_project(
    request: Request,
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump(
        exclude_unset=True,
        exclude={"initial_connection"},
    )
    data["source_system"] = _validate_source_system(data.get("source_system"))
    data["phase"] = _validate_phase(data.get("phase")) or "blueprint"
    if not data.get("owner"):
        data["owner"] = user.email
    # Persist the picked modules on the row so Discovery, Migration
    # Monitor, Output Preview, etc. can scope downstream surfaces by
    # the same scope the user committed to at setup time.
    data["selected_modules"] = list(payload.selected_modules or [])

    p = Project(**data)
    db.add(p)
    db.commit()
    db.refresh(p)

    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.created",
        target_type="project",
        target_id=p.id,
        project_id=p.id,
        summary=f"Created project '{p.name}' (source: {p.source_system or 'unset'})",
        details={
            "name": p.name,
            "client": p.client,
            "source_system": p.source_system,
            "phase": p.phase,
        },
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )

    # Auto-create planned-status Conversion rows for each module's
    # canonical Fusion target objects. This is what makes the Setup
    # Wizard's "Implementation Scope" step do real work — the customer
    # picks Financials + SCM and immediately has 10–15 conversion
    # objects pre-populated with planned_load_order and FBDI hints.
    if payload.selected_modules:
        from app.fusion_modules import all_objects_for_modules
        from app.models.conversion import Conversion as ConversionModel
        from app.models.fbdi import FBDITemplate

        existing_objects = {
            (c.target_object or "").lower()
            for c in db.query(ConversionModel)
            .filter(ConversionModel.project_id == p.id)
            .all()
        }
        # Pre-index every FBDI template by business_object so the auto-
        # link below is O(1) per conversion. Where multiple templates
        # match the same business_object (rare), we deterministically
        # pick the first by id so re-creating the same project yields
        # the same bindings.
        all_templates = (
            db.query(FBDITemplate)
            .filter(FBDITemplate.business_object.isnot(None))
            .order_by(FBDITemplate.id)
            .all()
        )
        templates_by_object: dict[str, FBDITemplate] = {}
        for t in all_templates:
            key = (t.business_object or "").strip().lower()
            if key and key not in templates_by_object:
                templates_by_object[key] = t

        objects = all_objects_for_modules(payload.selected_modules)
        added = 0
        linked_templates = 0
        for obj in objects:
            if (obj.target_object or "").lower() in existing_objects:
                continue
            # FBDI auto-link: match the conversion's target_object
            # against a seeded template's business_object. If hit, bind
            # the template_id so the analyst doesn't have to pick later.
            matched_template = templates_by_object.get(
                (obj.target_object or "").strip().lower()
            )
            conv = ConversionModel(
                project_id=p.id,
                name=obj.label,
                description=(
                    f"Auto-created from Setup Wizard scope. "
                    f"Source extract hint: "
                    f"{obj.source_extracts.get(p.source_system or '', '—')}"
                ),
                target_object=obj.target_object,
                planned_load_order=obj.planned_load_order,
                created_by=user.email,
                status="planning",
                template_id=matched_template.id if matched_template else None,
            )
            db.add(conv)
            added += 1
            if matched_template:
                linked_templates += 1
        if added:
            db.commit()
            db.refresh(p)
            record_event(
                db,
                actor_email=user.email,
                actor_user_id=user.id,
                action="project.updated",
                target_type="project",
                target_id=p.id,
                project_id=p.id,
                summary=(
                    f"Scope set: {len(payload.selected_modules)} module(s) "
                    f"→ auto-created {added} planned conversions "
                    f"({linked_templates} pre-linked to FBDI templates)"
                ),
                details={
                    "modules": payload.selected_modules,
                    "conversions_created": added,
                    "templates_linked": linked_templates,
                },
                source_ip=_client_ip(request),
                user_agent=_ua(request),
            )

    # Bundled connection — created in the same transaction so the Setup
    # Wizard's "Project Details → Source System" steps are atomic.
    if payload.initial_connection is not None:
        ic = payload.initial_connection
        ic_source = _validate_source_system(ic.source_system) or ic.source_system
        # If the wizard didn't set source_system at the project level, fall
        # back to the connection's source. They must agree if both are set.
        if not p.source_system:
            p.source_system = ic_source
            db.commit()
            db.refresh(p)
        elif p.source_system != ic_source:
            raise HTTPException(
                409,
                f"Project source_system '{p.source_system}' conflicts with "
                f"initial_connection source_system '{ic_source}'.",
            )
        create_connection(
            db,
            SourceConnectionCreate(
                project_id=p.id,
                source_system=ic_source,
                display_name=ic.display_name,
                endpoint=ic.endpoint,
                auth_type=ic.auth_type,
                connection_metadata=ic.connection_metadata or {},
                credentials=ic.credentials,
                mock_mode=ic.mock_mode,
            ),
            actor_email=user.email,
            actor_user_id=user.id,
            source_ip=_client_ip(request),
            user_agent=_ua(request),
        )
        db.refresh(p)

    return _hydrate(db, p)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    request: Request,
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    data = payload.model_dump(exclude_unset=True)
    if "source_system" in data:
        new_code = _validate_source_system(data["source_system"])
        # Once any conversion or connection is attached, source_system is
        # immutable — changing it would silently invalidate every learned
        # mapping referenced through this project.
        existing_anchors = (
            db.query(Conversion).filter(Conversion.project_id == p.id).count()
            + db.query(SourceConnection)
            .filter(SourceConnection.project_id == p.id)
            .count()
        )
        if existing_anchors and p.source_system and new_code != p.source_system:
            raise HTTPException(
                409,
                f"Cannot change source_system on a project with existing "
                f"conversions or connections. Detach them first.",
            )
        data["source_system"] = new_code
    if "phase" in data:
        data["phase"] = _validate_phase(data["phase"])
        if data["phase"] and data["phase"] != p.phase:
            record_event(
                db,
                actor_email=user.email,
                actor_user_id=user.id,
                action="project.phase_changed",
                target_type="project",
                target_id=p.id,
                project_id=p.id,
                summary=f"Phase: {p.phase or '—'} → {data['phase']}",
                details={"old_phase": p.phase, "new_phase": data["phase"]},
                source_ip=_client_ip(request),
                user_agent=_ua(request),
            )

    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)

    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.updated",
        target_type="project",
        target_id=p.id,
        project_id=p.id,
        summary=f"Updated project '{p.name}'",
        details={"fields_changed": list(data.keys())},
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return _hydrate(db, p)


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    name = p.name
    db.delete(p)
    db.commit()
    record_event(
        db,
        actor_email=user.email,
        actor_user_id=user.id,
        action="project.deleted",
        target_type="project",
        target_id=project_id,
        summary=f"Deleted project '{name}'",
        source_ip=_client_ip(request),
        user_agent=_ua(request),
    )
    return {"deleted": project_id}


@router.get("/{project_id}/conversions", response_model=list[ConversionOut])
def list_conversions_for_project(
    project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    """List all conversions inside an engagement, ordered by planned load order."""
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")

    out: list[ConversionOut] = []
    for c in (
        db.query(Conversion)
        .filter(Conversion.project_id == project_id)
        .order_by(Conversion.planned_load_order, Conversion.id)
        .all()
    ):
        co = ConversionOut.model_validate(c)
        co.dataset_name = c.dataset.name if c.dataset else None
        co.template_name = c.template.name if c.template else None
        co.project_name = p.name
        out.append(co)
    return out

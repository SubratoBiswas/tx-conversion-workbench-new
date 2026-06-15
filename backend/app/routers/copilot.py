"""AI Copilot chat endpoint.

Stateless conversation — each request includes the full message
history. The server attaches project context and forwards to Claude.

The floating Copilot widget hides itself when this endpoint 503s.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.project import Project
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.copilot import (
    CopilotError, CopilotMessage, CopilotResponse, CopilotUnavailable, chat,
)


router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class CopilotMessageIn(BaseModel):
    role: str
    content: str


class CopilotAskRequest(BaseModel):
    project_id: int
    messages: list[CopilotMessageIn]


class CopilotAnswer(BaseModel):
    answer: str
    citations: list[str] = []


@router.post(
    "/ask",
    response_model=CopilotAnswer,
    responses={503: {"description": "Copilot unavailable (no API key)."}},
)
def ask(
    payload: CopilotAskRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    project = (
        db.query(Project).filter(Project.id == payload.project_id).first()
    )
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        resp: CopilotResponse = chat(
            db=db,
            project=project,
            messages=[
                CopilotMessage(role=m.role, content=m.content)
                for m in payload.messages
            ],
        )
    except CopilotUnavailable as e:
        raise HTTPException(503, str(e))
    except CopilotError as e:
        raise HTTPException(502, str(e))
    return CopilotAnswer(answer=resp.answer, citations=resp.citations)

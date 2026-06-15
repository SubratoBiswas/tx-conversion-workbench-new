"""AI Copilot — natural-language Q&A over the project metadata.

The Copilot is a Claude Sonnet 4.6 conversation that has read-only
context for one specific project: its safeguards, score breakdown,
discovery rollup, top issues, top risks, and the latest reconciliation
status. The system prompt is structured so Claude answers grounded in
the context, not from training memory.

Production-grade properties:

* **No data persistence beyond audit.** The Copilot does not store chat
  history server-side for v1 — every call is stateless. (A later slice
  adds opt-in transcripts with explicit retention controls.)
* **No tool-use that mutates state.** Read-only for v1. Even hypothetical
  "create issue from copilot" actions require a confirmation round-trip
  through the normal Issue API so audit attribution stays correct.
* **Prompt caching** on the project context block — the cache key is
  ``project_id + last_updated_at`` so a multi-turn session shares the
  cached state until something material changes.
* **Graceful 503** when no Anthropic key is configured — the floating
  Copilot widget hides itself.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.conversion import Conversion
from app.models.cutover import Issue, Risk
from app.models.discovery import DiscoveryRun
from app.models.project import Project
from app.services.readiness_score import compute_readiness
from app.services.safeguards import evaluate_safeguards


log = logging.getLogger("trinamix.copilot")


class CopilotUnavailable(Exception):
    """Raised when no API key is configured."""


class CopilotError(Exception):
    """Wraps any other failure with a UI-safe message."""


@dataclass
class CopilotMessage:
    role: str       # "user" | "assistant"
    content: str


@dataclass
class CopilotResponse:
    answer: str
    citations: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = """\
You are the Trinamix Conversion Workbench Copilot. You answer questions \
about a specific Oracle Fusion implementation engagement.

Rules of engagement:
- Ground every answer in the project context block. If the answer isn't \
  in the context, say so — never invent a fact about the customer's \
  data, customisations, or schedule.
- Cite specific signals you used ("Gate Performance is 5/7 because \
  FX Rates is warning and Recon has 2 fails").
- For "is this risky?" questions, name the riskiest open issues, \
  top risks (by probability × impact), and any failing reconciliation \
  checks.
- For "what should I do next?" questions, look at the runbook progress, \
  unresolved blockers, and the lowest-scoring readiness lens — \
  recommend in that order.
- Keep answers tight: 3-6 sentences, plus bullet points if a list is \
  the cleanest response. Skip preamble like "Great question".

You cannot mutate state, send emails, or run jobs. If asked to do \
something action-y, recommend the in-product path ("open the Issue \
on the Migration Monitor and assign it to the data owner").
"""


def _build_project_context(db: Session, project: Project) -> str:
    """Compact JSON snapshot of everything the Copilot can ground on."""
    safeguards = evaluate_safeguards(db, project)
    score = compute_readiness(db, project)
    convs = db.query(Conversion).filter(Conversion.project_id == project.id).all()
    issues = (
        db.query(Issue)
        .filter(
            Issue.project_id == project.id,
            Issue.status.in_(("open", "in_progress", "blocked")),
        )
        .order_by(Issue.severity.desc(), Issue.id.desc())
        .limit(20)
        .all()
    )
    risks = (
        db.query(Risk)
        .filter(Risk.project_id == project.id, Risk.status != "closed")
        .order_by(Risk.score.desc())
        .limit(10)
        .all()
    )
    latest_discovery = (
        db.query(DiscoveryRun)
        .filter(
            DiscoveryRun.project_id == project.id,
            DiscoveryRun.status == "completed",
        )
        .order_by(DiscoveryRun.completed_at.desc())
        .first()
    )

    ctx = {
        "project": {
            "id": project.id,
            "name": project.name,
            "client": project.client,
            "source_system": project.source_system,
            "phase": project.phase,
            "go_live_date": project.go_live_date.isoformat() if project.go_live_date else None,
            "current_environment": project.current_environment,
            "dress_rehearsal_count": project.dress_rehearsal_count,
        },
        "readiness_score": {
            "total": score.total,
            "total_pct": score.total_pct,
            "lenses": {
                k: {"value_pct": v["value_pct"], "details": v.get("details")}
                for k, v in score.lenses.items()
            },
        },
        "safeguards": [
            {"code": s.code, "status": s.status, "message": s.message}
            for s in safeguards
        ],
        "conversions": [
            {
                "id": c.id, "name": c.name, "target_object": c.target_object,
                "status": c.status,
                "data_quality_score": c.data_quality_score,
            }
            for c in convs[:30]
        ],
        "open_issues": [
            {
                "id": i.id, "title": i.title, "severity": i.severity,
                "owner": i.owner_email, "due_date": i.due_date.isoformat() if i.due_date else None,
                "status": i.status,
            }
            for i in issues
        ],
        "top_risks": [
            {
                "id": r.id, "title": r.title, "score": r.score,
                "probability": r.probability, "impact": r.impact,
                "owner": r.owner_email, "status": r.status,
            }
            for r in risks
        ],
        "discovery_rollup": (
            {
                "total_objects": latest_discovery.total_objects,
                "complexity_score": latest_discovery.complexity_score,
                "pillar_counts": latest_discovery.pillar_counts,
                "integration_health": latest_discovery.integration_health,
            } if latest_discovery else None
        ),
    }
    return json.dumps(ctx, default=str, separators=(",", ":"))


def chat(
    *,
    db: Session,
    project: Project,
    messages: list[CopilotMessage],
) -> CopilotResponse:
    if not settings.ANTHROPIC_API_KEY:
        raise CopilotUnavailable(
            "ANTHROPIC_API_KEY is not configured. AI Copilot requires an "
            "API key."
        )
    if not messages or messages[-1].role != "user":
        raise CopilotError("The last message must be a user message.")
    try:
        from anthropic import Anthropic
    except ImportError as e:  # pragma: no cover
        raise CopilotUnavailable("anthropic SDK is not installed.") from e

    context_json = _build_project_context(db, project)
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=settings.ANTHROPIC_MODEL or "claude-sonnet-4-6",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"PROJECT_CONTEXT (JSON):\n{context_json}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
    except Exception as exc:
        log.warning("copilot API call failed: %s", exc)
        raise CopilotError(f"Anthropic API call failed: {exc}") from exc

    text_blocks = [
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ]
    answer = "\n".join(text_blocks).strip() or "(no answer)"
    return CopilotResponse(answer=answer, citations=[])

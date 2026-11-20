"""Context routes: what fills a session's context window, and Compact (see
``docs/api.md``).

Errors are ``{"detail": {"code": ..., "message": ...}}``, so clients branch
on ``code``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from nerve.agent.engine import CompactError, NotAcceptingTurnsError
from nerve.gateway.auth import require_auth
from nerve.gateway.routes._deps import get_deps
from nerve.identity import Actor

router = APIRouter()

def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


class CompactRequest(BaseModel):
    """Optional body of ``POST /api/sessions/{id}/compact``."""

    instructions: str | None = None


async def _context_view(deps: Any, session_id: str) -> dict:
    view = await deps.engine.context_view(session_id)
    if view is None:
        raise _error(404, "session_not_found", f"Session {session_id} not found")
    return view


@router.get("/api/sessions/{session_id}/context")
async def get_context(session_id: str, actor: Actor = Depends(require_auth)):
    """The context window by category from the live runtime, or the totals
    of the last turn when there is no live runtime that can tell them."""
    return await _context_view(get_deps(), session_id)


@router.post("/api/sessions/{session_id}/compact")
async def compact(
    session_id: str, body: CompactRequest | None = None,
    actor: Actor = Depends(require_auth),
):
    """Compact the conversation as a turn, then return the context again,
    with ``compacted``: the runtime's report (``pre_tokens``,
    ``post_tokens``). A session without a live runtime gets one first."""
    deps = get_deps()
    if not await deps.db.get_session(session_id):
        raise _error(404, "session_not_found", f"Session {session_id} not found")
    try:
        report = await deps.engine.compact_session(
            session_id, actor=actor,
            instructions=body.instructions if body else None,
        )
    except CompactError as e:
        status = {"failed": 502, "invalid": 422}.get(e.code, 409)
        raise _error(status, e.code, str(e)) from e
    except NotAcceptingTurnsError as e:
        raise _error(503, "not_accepting_turns", str(e)) from e
    return {**await _context_view(deps, session_id), "compacted": report}

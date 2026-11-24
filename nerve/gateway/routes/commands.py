"""The chat commands' routes (see ``nerve/commands.py`` and ``docs/api.md``):
``GET /api/commands`` lists a surface's commands, ``GET
/api/commands/{name}/text`` gives the answer of ``/status``, ``/cost`` and
``/diag`` as the web composer shows it, and ``POST
/api/sessions/{id}/btw`` answers a side question."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from nerve.agent.side_question import MAX_MESSAGES as SIDE_MAX_MESSAGES
from nerve.agent.side_question import SideQuestionError, ask_side_question
from nerve.channels import command_data
from nerve.channels.command_text import cost_text, diag_text, period_cost_text, status_text
from nerve.commands import commands_for
from nerve.config import get_config
from nerve.gateway.auth import require_auth
from nerve.gateway.routes._deps import get_deps
from nerve.identity import Actor

router = APIRouter()


@router.get("/api/commands")
async def list_commands(surface: str = "web", actor: Actor = Depends(require_auth)):
    """The commands a surface offers, in menu order: name, args
    (``<required>`` / ``[optional]``) and a line of help."""
    try:
        commands = commands_for(surface)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"surface": surface, "commands": [c.to_dict() for c in commands]}


# The commands whose answer is text the server builds (the rest run in the client).
TEXT_COMMANDS = ("status", "cost", "diag")


@router.get("/api/commands/{name}/text")
async def command_text(
    name: str, session_id: str = "", args: str = "",
    actor: Actor = Depends(require_auth),
):
    """The answer of ``/status``, ``/cost [today|week|month]`` or ``/diag``
    as text, the same as on Telegram. ``session_id`` names the chat (not
    needed for ``/diag`` and ``/cost <period>``). 404: unknown command or
    session; 400: a bad period."""
    if name not in TEXT_COMMANDS:
        raise HTTPException(status_code=404, detail=f"/{name} has no text answer")
    engine = get_deps().engine
    tz = get_config().timezone
    period = args.strip().lower()
    if name == "diag":
        return {"text": diag_text(await command_data.diag_info(engine, tz), tz)}
    if name == "cost" and period:
        if period not in command_data.PERIODS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown period \"{period}\". Use today, week or month.",
            )
        return {"text": period_cost_text(await command_data.period_cost_info(engine, period, tz))}
    info = (
        await command_data.status_info(engine, session_id) if name == "status"
        else await command_data.cost_info(engine, session_id)
    ) if session_id else None
    if info is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"text": status_text(info, tz) if name == "status" else cost_text(info)}


@router.post("/api/sessions/{session_id}/btw")
async def side_question(
    session_id: str, question: str = Body(..., embed=True),
    actor: Actor = Depends(require_auth),
):
    """``/btw``: answer *question* about the session now, from its recent
    transcript, without adding to it or waiting for its turn. Returns
    ``{answer, model, input_tokens, output_tokens, cost_usd}``. 404: unknown
    session; 400: the question can't be asked here; 502: the model call
    failed."""
    deps = get_deps()
    session = await deps.db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = await deps.db.get_messages(session_id, limit=SIDE_MAX_MESSAGES)
    try:
        return await ask_side_question(get_config(), session, messages, question)
    except SideQuestionError as e:
        status = 502 if str(e).startswith(("The model", "No answer")) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e

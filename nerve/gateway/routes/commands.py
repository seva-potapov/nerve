"""``GET /api/commands``: the chat commands of a surface (see
``nerve/commands.py`` and ``docs/api.md``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nerve.commands import commands_for
from nerve.gateway.auth import require_auth
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

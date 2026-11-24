"""The chat commands, one list for every surface.

Telegram registers its ``/`` menu from it (``setMyCommands``), the web
composer shows its command menu from ``GET /api/commands``. Each surface
runs its commands itself; this module only says which commands exist where,
with their arguments and a line of help.

``args`` follows the usual notation: ``""`` takes no argument, ``"<x>"`` a
required one, ``"[x]"`` an optional one.
"""

from __future__ import annotations

from dataclasses import dataclass

TELEGRAM = "telegram"
WEB = "web"
SURFACES = (TELEGRAM, WEB)


@dataclass(frozen=True)
class ChatCommand:
    name: str
    args: str
    help: str
    surfaces: tuple[str, ...]

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "args": self.args, "help": self.help}


_BOTH = (TELEGRAM, WEB)

# Order is the menu order: the everyday commands first.
COMMANDS: tuple[ChatCommand, ...] = (
    ChatCommand("new", "[title]", "Start a new session", _BOTH),
    ChatCommand("stop", "", "Stop the running turn", _BOTH),
    ChatCommand("status", "", "Show the session: model, effort, agents, context, cost", _BOTH),
    ChatCommand("btw", "<question>", "Ask a side question now, outside the session", _BOTH),
    ChatCommand(
        "compact", "[instructions]",
        "Summarize the conversation to free context", _BOTH,
    ),
    ChatCommand("context", "", "Show what fills the context window", _BOTH),
    ChatCommand("cost", "[period]", "This session's cost, or all of today, week or month", _BOTH),
    ChatCommand("sessions", "", "Switch to another session", _BOTH),
    ChatCommand("session", "[id]", "Show the current session or switch to one", (TELEGRAM,)),
    ChatCommand("rename", "<title>", "Rename this session", _BOTH),
    ChatCommand("fork", "[title]", "Fork this chat into a new session; as a reply, from there", _BOTH),
    ChatCommand("retry", "", "Send your last message again", _BOTH),
    ChatCommand("model", "[name]", "Show or set this session's model", _BOTH),
    ChatCommand("effort", "[level]", "Show or set this session's reasoning effort", _BOTH),
    ChatCommand("ps", "", "List and stop this session's agents and processes", _BOTH),
    ChatCommand("files", "", "Get the files this session wrote", (TELEGRAM,)),
    ChatCommand("file", "[path]", "Get a workspace file, or browse a folder", (TELEGRAM,)),
    ChatCommand("transcript", "", "Get this session's conversation as a file", (TELEGRAM,)),
    ChatCommand("star", "", "Keep this session from closing when idle", _BOTH),
    ChatCommand("unstar", "", "Let this session close when idle again", _BOTH),
    ChatCommand("plans", "", "Review pending plans", _BOTH),
    ChatCommand("reply", "<answer>", "Answer the latest pending question", (TELEGRAM,)),
    ChatCommand("diag", "", "Show how Nerve is running right now", _BOTH),
    ChatCommand("doctor", "", "Check the configuration", _BOTH),
    ChatCommand("restart", "", "Restart Nerve (every session)", (TELEGRAM,)),
    ChatCommand("help", "", "List the commands and shortcuts", (WEB,)),
    ChatCommand("start", "", "Check the connection", (TELEGRAM,)),
    ChatCommand("pair", "<code>", "Pair this Telegram account", (TELEGRAM,)),
)


def commands_for(surface: str) -> list[ChatCommand]:
    """The commands of one surface, in menu order."""
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r} (known: {', '.join(SURFACES)})")
    return [c for c in COMMANDS if surface in c.surfaces]


def telegram_menu() -> list[tuple[str, str]]:
    """``(command, description)`` pairs for Telegram's ``setMyCommands``.
    Telegram shows no argument hints, so they go into the description."""
    menu = []
    for c in commands_for(TELEGRAM):
        description = f"{c.help} {c.args}".strip() if c.args else c.help
        menu.append((c.name, description[:256]))
    return menu

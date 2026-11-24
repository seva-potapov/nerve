"""The data behind the chat commands ``/status``, ``/cost``, ``/diag``,
``/files`` and ``/ps``.

Each function reads the engine and its database and returns the dict that
:mod:`nerve.channels.command_text` turns into the answer. Telegram calls them
through :class:`~nerve.channels.router.ChannelRouter`, the web composer
through ``GET /api/commands/{name}/text``, so both say the same thing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from nerve.agent.agents.model import LIVE_STATUSES
from nerve.agent.effort import session_effort
from nerve.channels.command_text import parse_time

if TYPE_CHECKING:
    from nerve.agent.engine import AgentEngine

logger = logging.getLogger(__name__)

# /cost <period>: the windows it knows, and how each is labelled.
PERIODS: dict[str, str] = {"today": "today", "week": "last 7 days", "month": "last 30 days"}
# The process roles /ps lists: the session's own work, not its runtime.
PS_ROLES = ("call", "background", "orphan")


class CommandRefused(Exception):
    """A command's change was refused (the session runs agents, a bad model,
    …); the message says why, for the person."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def period_start(period: str, tz: str | None, now: datetime | None = None) -> datetime:
    """Where a ``/cost`` period starts: local midnight for ``today``, 7 or
    30 days back for ``week`` and ``month``. Raises ``ValueError`` for any
    other name."""
    now = now or datetime.now(timezone.utc)
    if period == "today":
        try:
            zone = ZoneInfo(tz) if tz else timezone.utc
        except Exception:  # noqa: BLE001 — an unknown zone counts in UTC
            zone = timezone.utc
        local = now.astimezone(zone)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    raise ValueError(f"unknown period {period!r} (one of: {', '.join(PERIODS)})")


async def status_info(engine: AgentEngine, session_id: str) -> dict[str, Any] | None:
    """``/status``: the session, its model and effort, what runs in it, its
    context and its cost so far. None: no such session."""
    db = engine.db
    session = await db.get_session(session_id)
    if not session:
        return None
    config = engine.config
    effort = session_effort(session)
    try:
        context = await engine.context_view(session_id)
    except Exception as e:  # noqa: BLE001 — the rest of the status still helps
        logger.debug("context view of %s failed: %s", session_id, e)
        context = None
    queue = engine.queue_view(session_id)
    totals = await db.get_session_usage_totals(session_id)
    process_count = getattr(engine, "process_count", None)
    return {
        "session_id": session_id,
        "title": session.get("title") or "",
        "source": session.get("source") or "",
        "backend": session.get("backend") or "",
        "model": session.get("model") or config.agent.model,
        "effort": effort,
        "effort_default": None if effort else config.agent.effort,
        "running": engine.is_session_running(session_id),
        "queued": sum(1 for q in queue if not q.get("internal")),
        "agents": engine.background_agent_count(session_id),
        "processes": process_count(session_id) if callable(process_count) else None,
        "context": context,
        "cost_usd": await db.get_session_effective_cost(session_id),
        "turns": int(totals.get("turns") or 0),
        "created_at": session.get("created_at"),
        "last_activity_at": session.get("updated_at"),
    }


async def cost_info(engine: AgentEngine, session_id: str) -> dict[str, Any] | None:
    """``/cost``: the session's spend, per turn model, and its tokens."""
    db = engine.db
    if not await db.get_session(session_id):
        return None
    totals = await db.get_session_usage_totals(session_id)
    return {
        "session_id": session_id,
        "turns": int(totals.get("turns") or 0),
        "cost_usd": await db.get_session_effective_cost(session_id),
        "by_model": await db.get_session_usage_by_model(session_id),
        "input": int(totals.get("total_input") or 0),
        "output": int(totals.get("total_output") or 0),
        "cache_read": int(totals.get("total_cache_read") or 0),
        "cache_write": int(totals.get("total_cache_creation") or 0),
    }


async def period_cost_info(
    engine: AgentEngine, period: str, tz: str | None = None,
) -> dict[str, Any]:
    """``/cost today|week|month``: every session's spend in the window."""
    start = period_start(period, tz)
    usage = await engine.db.get_usage_since(_iso(start))
    return {"period": period, "label": PERIODS[period], "since": _iso(start), **usage}


def _uptime_seconds() -> float | None:
    """How long this process has run (Linux ``/proc``), or None."""
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        with open("/proc/uptime") as f:
            boot_seconds = float(f.read().split()[0])
        return max(0.0, boot_seconds - start_ticks / os.sysconf("SC_CLK_TCK"))
    except Exception:  # noqa: BLE001 — not Linux, or /proc unreadable
        return None


def _mem_available_gb() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None
    return None


def _route_defaults(route: Any) -> dict[str, Any]:
    """The default of each FastAPI query parameter of *route*: called as a
    plain function, a ``Query(7)`` default would arrive as the Query object."""
    import inspect

    out: dict[str, Any] = {}
    for name, param in inspect.signature(route).parameters.items():
        default = param.default
        if default is not inspect.Parameter.empty and hasattr(default, "default") and not isinstance(
            default, (int, float, str, bool, type(None)),
        ):
            out[name] = default.default
    return out


async def diag_info(engine: AgentEngine, tz: str | None = None) -> dict[str, Any]:
    """``/diag``: the live state of the running Nerve (``/doctor`` checks the
    configuration instead). The channel adds what only it knows, such as
    Telegram's pending updates."""
    from nerve.gateway.routes.diagnostics import diagnostics

    db = engine.db
    now = datetime.now(timezone.utc)
    day_ago = _iso(now - timedelta(days=1))
    try:
        diag = await diagnostics(**_route_defaults(diagnostics))
    except Exception as e:  # noqa: BLE001 — report what can be read
        logger.warning("diagnostics failed for /diag: %s", e)
        diag = {}
    system = diag.get("system") or {}
    try:
        load = list(os.getloadavg())
    except OSError:
        load = None
    process_counts = getattr(engine, "_process_counts", None) or {}
    memo = diag.get("memorization") or {}
    sync = [
        {"name": name, "last_run": state.get("last_run"), "error": state.get("error")}
        for name, state in sorted((diag.get("sync") or {}).items())
        if isinstance(state, dict)
    ]
    today = await db.get_usage_since(_iso(period_start("today", tz, now)), top=0)
    week = await db.get_usage_since(_iso(now - timedelta(days=7)), top=0)
    agents = getattr(engine, "agents", None)
    live_agents = getattr(agents, "live_total", None)
    return {
        "commit": system.get("commit"),
        "uptime_s": _uptime_seconds(),
        "rss_mb": system.get("memory_mb"),
        "load": load,
        "mem_available_gb": _mem_available_gb(),
        "disk_free_gb": system.get("disk_free_gb"),
        "sessions_running": engine.sessions.running_count(),
        "agents_running": live_agents() if callable(live_agents) else None,
        "processes": sum(process_counts.values()) if hasattr(engine, "process_count") else None,
        "sync": sync,
        "cron_failures": await db.get_cron_failures_since(day_ago),
        "mcp": await db.get_mcp_usage_since(day_ago),
        "memorization": {
            "pending": memo.get("sessions_pending", 0),
            "errors": memo.get("total_errors", 0),
        },
        "spend": {"today": today["cost_usd"], "week": week["cost_usd"]},
    }


def _display_path(path: str, workspace: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(workspace.resolve()))
    except (ValueError, OSError):
        return path


async def written_files(
    engine: AgentEngine, session_id: str, limit: int = 20,
) -> list[dict[str, Any]]:
    """``/files``: what the session's Write/Edit calls wrote, newest first.
    ``path`` is workspace-relative when the file is in the workspace;
    ``abs_path`` is what to send."""
    workspace = Path(engine.config.workspace)
    out = []
    for entry in await engine.db.get_session_written_files(session_id, limit=limit):
        abs_path = entry["path"]
        try:
            st = os.stat(abs_path)
            exists, size = True, st.st_size
        except OSError:
            exists, size = False, None
        out.append({
            **entry,
            "abs_path": abs_path,
            "path": _display_path(abs_path, workspace),
            "exists": exists,
            "size": size,
        })
    return out


async def ps_info(engine: AgentEngine, session_id: str) -> dict[str, Any]:
    """``/ps``: the session's live agents and its own processes."""
    snap = await engine.agent_snapshot(session_id)
    now = datetime.now(timezone.utc)
    agents = []
    for view in snap.get("agents") or []:
        if view.get("status") not in LIVE_STATUSES or view.get("ambient"):
            continue
        started = parse_time(view.get("started_at") or view.get("created_at"))
        agents.append({
            "id": view.get("id"),
            "label": view.get("label") or view.get("description") or view.get("kind"),
            "kind": view.get("kind"),
            "status": view.get("status"),
            "elapsed_s": (now - started).total_seconds() if started else None,
        })
    processes: list[dict[str, Any]] = []
    supported = True
    session_processes = getattr(engine, "session_processes", None)
    if callable(session_processes):
        view = await session_processes(session_id, fresh=True)
        supported = view.get("supported", True)
        for row in view.get("processes") or []:
            if row.get("role") not in PS_ROLES:
                continue
            started = parse_time(row.get("started_at"))
            processes.append({
                "pid": row.get("pid"),
                "role": row.get("role"),
                "cmd": row.get("command") or row.get("name"),
                "start_ticks": row.get("start_ticks"),
                "elapsed_s": (now - started).total_seconds() if started else None,
            })
    return {"agents": agents, "processes": processes, "processes_supported": supported}

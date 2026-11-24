"""Plain-text answers for the chat commands ``/status``, ``/cost``, ``/diag``,
``/files`` and ``/ps``.

Built from the dicts :class:`~nerve.channels.router.ChannelRouter` gathers
(``status_info``, ``cost_info``, ``period_cost_info``, ``diag_info``,
``written_files``, ``ps_info``), so Telegram and the web composer say the same
thing. Amounts are US dollars, as Nerve records them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nerve.channels.context_text import format_tokens

# How many sessions /cost <period> names, most expensive first.
TOP_SESSIONS = 5
# How many characters of a process command line /ps shows.
COMMAND_CHARS = 60


def format_usd(amount: float | int | None) -> str:
    """``$1,234.50``; ``<$0.01`` for a positive amount that rounds to zero."""
    value = float(amount or 0)
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:,.2f}"


def format_duration(seconds: float | int | None) -> str:
    """``45 s``, ``12 min``, ``3 h 05 min``, ``2 d 4 h``."""
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def format_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def parse_time(value: Any) -> datetime | None:
    """A stored timestamp as an aware UTC datetime. Naive values are UTC
    (SQLite's ``datetime('now')``)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_local(value: Any, tz: str | None, now: datetime | None = None) -> str:
    """``20:39`` for today, ``23 Nov 17:31`` before, in the config timezone."""
    dt = parse_time(value)
    if dt is None:
        return "?"
    try:
        zone = ZoneInfo(tz) if tz else timezone.utc
    except Exception:  # noqa: BLE001 — an unknown zone falls back to UTC
        zone = timezone.utc
    local = dt.astimezone(zone)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    if local.date() == today:
        return local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b %H:%M')}"


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}es" if word.endswith("s") else f"{n} {word}s"


def status_text(info: dict[str, Any], tz: str | None = None) -> str:
    """The ``/status`` answer: what the session is, runs on and is doing."""
    sid = info.get("session_id", "?")
    title = (info.get("title") or "").strip()
    lines = [f"Session {sid}" + (f": {title}" if title else "")]

    model = info.get("model") or "default model"
    effort = info.get("effort")
    if effort:
        model += f" · effort {effort}"
    elif info.get("effort_default"):
        model += f" · effort {info['effort_default']} (default)"
    lines.append(f"Model: {model}")

    if info.get("running"):
        now = "running a turn"
    else:
        now = "idle"
    queued = int(info.get("queued") or 0)
    if queued:
        now += f", {_plural(queued, 'message')} queued"
    lines.append(f"Now: {now}")

    processes = info.get("processes")
    line = f"Agents: {int(info.get('agents') or 0)} running"
    if processes is not None:
        line += f" · processes: {int(processes)}"
    lines.append(line)

    context = info.get("context") or {}
    total, window = int(context.get("total_tokens") or 0), int(context.get("max_tokens") or 0)
    if total and window:
        lines.append(
            f"Context: {format_tokens(total)} of {format_tokens(window)} tokens "
            f"({round(100 * total / window)}%)"
        )
    elif total:
        lines.append(f"Context: {format_tokens(total)} tokens")

    turns = int(info.get("turns") or 0)
    lines.append(f"Cost so far: {format_usd(info.get('cost_usd'))} over {_plural(turns, 'turn')}")

    started = info.get("created_at")
    last = info.get("last_activity_at")
    when = []
    if started:
        when.append(f"started {format_local(started, tz)}")
    if last:
        when.append(f"last active {format_local(last, tz)}")
    if when:
        joined = " · ".join(when)
        lines.append(joined[0].upper() + joined[1:])
    return "\n".join(lines)


def cost_text(info: dict[str, Any]) -> str:
    """The ``/cost`` answer for one session."""
    turns = int(info.get("turns") or 0)
    if not turns:
        return "This session has no recorded cost yet.\n/cost today, /cost week or /cost month cover every session."
    lines = [f"Cost of this session: {format_usd(info.get('cost_usd'))} ({_plural(turns, 'turn')})"]
    by_model = [m for m in info.get("by_model") or [] if float(m.get("cost_usd") or 0) > 0]
    if len(by_model) > 1 or (by_model and info.get("per_model_exact")):
        for m in by_model:
            lines.append(f"• {m.get('model') or 'unknown'}: {format_usd(m.get('cost_usd'))}")
    read = int(info.get("cache_read") or 0)
    written = int(info.get("cache_write") or 0)
    fresh = int(info.get("input") or 0)
    parts = [
        f"{format_tokens(fresh)} in",
        f"{format_tokens(info.get('output'))} out",
        f"{format_tokens(read)} cache reads",
        f"{format_tokens(written)} cache writes",
    ]
    lines.append("Tokens: " + ", ".join(parts))
    prompt = read + written + fresh
    if prompt:
        lines.append(f"Read from cache: {round(100 * read / prompt)}% of prompt tokens")
    lines.append("/cost today, /cost week or /cost month cover every session.")
    return "\n".join(lines)


def period_cost_text(info: dict[str, Any]) -> str:
    """The ``/cost <period>`` answer: every session in the window."""
    label = info.get("label") or "the period"
    turns, sessions = int(info.get("turns") or 0), int(info.get("sessions") or 0)
    lines = [
        f"Cost, {label}: {format_usd(info.get('cost_usd'))} "
        f"({_plural(turns, 'turn')}, {_plural(sessions, 'session')})"
    ]
    for row in info.get("by_source") or []:
        lines.append(f"• {row.get('source') or 'other'}: {format_usd(row.get('cost_usd'))}")
    top = info.get("top_sessions") or []
    if top:
        lines.append("Most expensive sessions:")
        for row in top[:TOP_SESSIONS]:
            title = (row.get("title") or "").strip() or "(untitled)"
            if len(title) > 50:
                title = title[:49] + "…"
            lines.append(f"• {format_usd(row.get('cost_usd'))} {title} ({row.get('session_id')})")
    return "\n".join(lines)


def diag_text(info: dict[str, Any], tz: str | None = None) -> str:
    """The ``/diag`` answer: how the running Nerve is doing right now.

    ``/doctor`` checks the configuration; this reports the live state.
    """
    head = f"Nerve {str(info.get('commit') or '?')[:8]}"
    if info.get("uptime_s") is not None:
        head += f" · up {format_duration(info['uptime_s'])}"
    if info.get("rss_mb") is not None:
        head += f" · {round(float(info['rss_mb']))} MB"
    lines = [head]

    box = []
    load = info.get("load")
    if load:
        box.append("load " + " / ".join(f"{x:.1f}" for x in load))
    if info.get("mem_available_gb") is not None:
        box.append(f"{float(info['mem_available_gb']):.0f} GB RAM free")
    if info.get("disk_free_gb") is not None:
        box.append(f"disk {float(info['disk_free_gb']):.0f} GB free")
    if box:
        lines.append("Box: " + " · ".join(box))

    lines.append(
        f"Running: {_plural(int(info.get('sessions_running') or 0), 'session')}, "
        f"{_plural(int(info.get('agents_running') or 0), 'agent')}"
        + (
            f", {_plural(int(info['processes']), 'process')}"
            if info.get("processes") is not None else ""
        )
    )

    telegram = info.get("telegram")
    if telegram:
        state = "live" if telegram.get("live") else "NOT live"
        pending = telegram.get("pending")
        lines.append(f"Telegram: {state}" + (f", {_plural(int(pending), 'update')} pending" if pending is not None else ""))

    sync = info.get("sync") or []
    if sync:
        parts = []
        for s in sync:
            name = s.get("name", "?")
            if s.get("error"):
                parts.append(f"{name} FAILED ({str(s['error'])[:60]})")
            elif s.get("last_run"):
                parts.append(f"{name} ok {format_local(s['last_run'], tz)}")
            else:
                parts.append(f"{name} never ran")
        lines.append("Sync: " + " · ".join(parts))

    failures = info.get("cron_failures") or []
    if failures:
        first = failures[0]
        lines.append(
            f"Cron, last 24 h: {len(failures)} failed, last {first.get('job_id')} "
            f"{format_local(first.get('started_at'), tz)}"
            + (f": {str(first.get('error'))[:80]}" if first.get("error") else "")
        )
    else:
        lines.append("Cron, last 24 h: no failures")

    mcp = info.get("mcp") or []
    failing = [m for m in mcp if int(m.get("failures") or 0)]
    if mcp:
        if failing:
            lines.append("MCP, last 24 h: " + ", ".join(
                f"{m['server']} {m['failures']} of {m['calls']} calls failed" for m in failing
            ))
        else:
            lines.append(f"MCP, last 24 h: {sum(int(m.get('calls') or 0) for m in mcp)} calls, none failed")

    memo = info.get("memorization")
    if memo:
        lines.append(
            f"Memory indexing: {_plural(int(memo.get('pending') or 0), 'session')} waiting, "
            f"{_plural(int(memo.get('errors') or 0), 'error')}"
        )

    spend = info.get("spend")
    if spend:
        lines.append(f"Spend: {format_usd(spend.get('today'))} today · {format_usd(spend.get('week'))} last 7 days")
    return "\n".join(lines)


def files_text(files: list[dict[str, Any]], tz: str | None = None) -> str:
    """The ``/files`` answer: the files this session wrote, newest first."""
    if not files:
        return "This session hasn't written any files with Write or Edit.\n/file <path> sends any workspace file."
    lines = ["Files this session wrote, newest first:"]
    for i, f in enumerate(files, 1):
        detail = []
        if f.get("exists") is False:
            detail.append("deleted")
        elif f.get("size") is not None:
            detail.append(format_bytes(f["size"]))
        if f.get("at"):
            detail.append(format_local(f["at"], tz))
        lines.append(f"{i}. {f.get('path')}" + (f" ({', '.join(detail)})" if detail else ""))
    lines.append("Tap one to get it. /file <path> sends any workspace file.")
    return "\n".join(lines)


def ps_text(info: dict[str, Any]) -> str:
    """The ``/ps`` answer: the session's running agents and processes."""
    agents = info.get("agents") or []
    processes = info.get("processes") or []
    if not agents and not processes:
        return "Nothing of this session is running."
    lines: list[str] = []
    n = 0
    if agents:
        lines.append(f"Agents ({len(agents)} running):")
        for a in agents:
            n += 1
            label = (a.get("label") or a.get("kind") or "agent").strip()
            age = f" · {format_duration(a['elapsed_s'])}" if a.get("elapsed_s") is not None else ""
            lines.append(f"{n}. {label[:70]}{age}")
    if processes:
        lines.append(f"Processes ({len(processes)}):")
        for p in processes:
            n += 1
            cmd = " ".join(str(p.get("cmd") or "").split())
            if len(cmd) > COMMAND_CHARS:
                cmd = cmd[: COMMAND_CHARS - 1] + "…"
            age = f" · {format_duration(p['elapsed_s'])}" if p.get("elapsed_s") is not None else ""
            lines.append(f"{n}. pid {p.get('pid')} ({p.get('role')}) {cmd}{age}")
    if not info.get("processes_supported", True):
        lines.append("Processes can't be listed here.")
    lines.append("Tap ⏹ to stop one.")
    return "\n".join(lines)

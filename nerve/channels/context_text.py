"""Plain-text answers for the chat commands ``/compact`` and ``/context``
(Telegram), built from the engine's context view and compact report."""

from __future__ import annotations

from typing import Any

# The /context answer lists at most this many categories, biggest first.
MAX_CATEGORIES = 8


def format_tokens(n: int | float | None) -> str:
    """Token counts the way the context bar says them: 950, 14.5K, 998K, 1M."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 100_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    if n < 1_000_000:
        return f"{round(n / 1000)}K"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def compact_done_text(report: dict[str, Any] | None, view: dict[str, Any] | None) -> str:
    """``Compacted: 14.5K → 2.8K tokens (of 1M).`` from the runtime's report
    (``pre_tokens``/``post_tokens``) and the context view after the compact."""
    report = report or {}
    window = int((view or {}).get("max_tokens") or 0)
    of = f" (of {format_tokens(window)})" if window else ""
    pre, post = report.get("pre_tokens"), report.get("post_tokens")
    if isinstance(pre, (int, float)) and isinstance(post, (int, float)):
        return f"Compacted: {format_tokens(pre)} → {format_tokens(post)} tokens{of}."
    if view and view.get("total_tokens"):
        return f"Compacted. Context now: {format_tokens(view['total_tokens'])} tokens{of}."
    return "Compacted."


def context_text(view: dict[str, Any]) -> str:
    """The ``/context`` answer: the total against the window, the
    auto-compact threshold and the biggest categories."""
    total = int(view.get("total_tokens") or 0)
    window = int(view.get("max_tokens") or 0)
    if window:
        head = f"Context: {format_tokens(total)} of {format_tokens(window)} tokens ({round(100 * total / window)}%)"
    else:
        head = f"Context: {format_tokens(total)} tokens"
    lines = [head]
    threshold = view.get("auto_compact_threshold")
    if threshold:
        lines.append(f"Auto-compact at {format_tokens(threshold)}")
    categories = sorted(
        (c for c in view.get("categories") or [] if int(c.get("tokens") or 0) > 0),
        key=lambda c: -int(c.get("tokens") or 0),
    )
    for c in categories[:MAX_CATEGORIES]:
        lines.append(f"• {c.get('name', '?')}: {format_tokens(c.get('tokens'))}")
    if not view.get("live"):
        lines.append("Estimate from the last turn: the session's runtime isn't running.")
    if view.get("can_compact"):
        lines.append("/compact [instructions] summarizes the conversation.")
    return "\n".join(lines)

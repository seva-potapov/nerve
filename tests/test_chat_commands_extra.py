"""The chat commands added with ``/status``: their text, their data, the
Telegram handlers and buttons, and the web routes (``nerve/commands.py``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from nerve.agent import side_question as sq
from nerve.channels import command_data
from nerve.channels.command_data import CommandRefused, period_start
from nerve.channels.command_text import (
    cost_text,
    diag_text,
    files_text,
    format_usd,
    period_cost_text,
    ps_text,
    status_text,
)
from nerve.channels.telegram import TelegramChannel
from nerve.channels.telegram_commands import match_model

# --------------------------------------------------------------------------- #
#  Text                                                                        #
# --------------------------------------------------------------------------- #

STATUS = {
    "session_id": "s-1", "title": "Nerve commands", "model": "claude-opus-5-5",
    "effort": "max", "effort_default": None, "running": True, "queued": 2,
    "agents": 3, "processes": 1, "context": {"total_tokens": 412_000, "max_tokens": 1_000_000},
    "cost_usd": 47.123, "turns": 38,
    "created_at": "2026-11-23 06:31:00", "last_activity_at": "2026-11-24T09:39:00+00:00",
}


def test_usd_reads_like_money():
    assert format_usd(1234.501) == "$1,234.50"
    assert format_usd(0) == "$0.00"
    assert format_usd(0.001) == "<$0.01"
    assert format_usd(None) == "$0.00"


def test_status_says_model_effort_work_context_and_cost():
    text = status_text(STATUS, "UTC")
    assert text.splitlines()[0] == "Session s-1: Nerve commands"
    assert "Model: claude-opus-5-5 · effort max" in text
    assert "Now: running a turn, 2 messages queued" in text
    assert "Agents: 3 running · processes: 1" in text
    assert "Context: 412K of 1M tokens (41%)" in text
    assert "Cost so far: $47.12 over 38 turns" in text
    assert "Started 23 Nov 06:31" in text


def test_status_names_the_default_effort_and_leaves_out_unknown_processes():
    text = status_text({**STATUS, "effort": None, "effort_default": "high",
                        "processes": None, "running": False, "queued": 0}, "UTC")
    assert "effort high (default)" in text
    assert "Now: idle" in text
    assert "processes" not in text


def test_cost_lists_models_only_when_there_are_several():
    one = {"turns": 2, "cost_usd": 1.5, "by_model": [{"model": "m1", "cost_usd": 1.5}],
           "input": 10, "output": 20, "cache_read": 900, "cache_write": 90}
    assert "• m1" not in cost_text(one)
    assert "Read from cache: 90% of prompt tokens" in cost_text(one)
    two = {**one, "by_model": [{"model": "m1", "cost_usd": 1.0}, {"model": "m2", "cost_usd": 0.5}]}
    assert "• m1: $1.00" in cost_text(two) and "• m2: $0.50" in cost_text(two)
    assert cost_text({"turns": 0}).startswith("This session has no recorded cost yet.")


def test_period_cost_names_sources_and_the_top_sessions():
    text = period_cost_text({
        "label": "last 7 days", "cost_usd": 100, "turns": 10, "sessions": 2,
        "by_source": [{"source": "web", "cost_usd": 80}, {"source": "cron", "cost_usd": 20}],
        "top_sessions": [{"session_id": "a1", "title": "x" * 60, "cost_usd": 80}],
    })
    assert text.splitlines()[0] == "Cost, last 7 days: $100.00 (10 turns, 2 sessions)"
    assert "• web: $80.00" in text
    assert "(a1)" in text and "…" in text


def test_diag_reports_failures_and_says_when_there_are_none():
    base = {
        "commit": "abc12345de", "uptime_s": 3700, "rss_mb": 512.4, "load": [0.4, 0.3, 0.2],
        "mem_available_gb": 8.2, "disk_free_gb": 50.4, "sessions_running": 2,
        "agents_running": 3, "processes": 5, "telegram": {"live": True, "pending": 0},
        "sync": [{"name": "github", "last_run": None, "error": "boom"}],
        "cron_failures": [], "mcp": [{"server": "notion", "calls": 3, "failures": 0}],
        "memorization": {"pending": 5, "errors": 0}, "spend": {"today": 4.5, "week": 120},
    }
    text = diag_text(base, "UTC")
    assert text.splitlines()[0] == "Nerve abc12345 · up 1 h 01 min · 512 MB"
    assert "Running: 2 sessions, 3 agents, 5 processes" in text
    assert "Telegram: live, 0 updates pending" in text
    assert "github FAILED (boom)" in text
    assert "Cron, last 24 h: no failures" in text
    assert "MCP, last 24 h: 3 calls, none failed" in text
    failing = diag_text({**base, "cron_failures": [{"job_id": "nightly-report", "started_at": None, "error": "x"}],
                         "mcp": [{"server": "clickplane", "calls": 46, "failures": 2}]}, "UTC")
    assert "Cron, last 24 h: 1 failed, last nightly-report" in failing
    assert "clickplane 2 of 46 calls failed" in failing


def test_files_and_ps_text():
    assert "hasn't written any files" in files_text([])
    text = files_text([{"path": "drafts/a.md", "size": 2048, "exists": True, "at": None},
                       {"path": "gone.txt", "exists": False}])
    assert "1. drafts/a.md (2.0 KB)" in text and "2. gone.txt (deleted)" in text
    assert ps_text({"agents": [], "processes": []}) == "Nothing of this session is running."
    ps = ps_text({"agents": [{"label": "Explore: find it", "elapsed_s": 180}],
                  "processes": [{"pid": 7, "role": "call", "cmd": "bash x " * 30, "elapsed_s": 5}]})
    assert "1. Explore: find it · 3 min" in ps
    assert "2. pid 7 (call)" in ps and "…" in ps


# --------------------------------------------------------------------------- #
#  Data                                                                        #
# --------------------------------------------------------------------------- #


def test_today_starts_at_local_midnight():
    now = datetime(2026, 11, 24, 9, 30, tzinfo=timezone.utc)  # 20:30 in Sydney
    start = period_start("today", "Australia/Sydney", now)
    assert start.astimezone(timezone.utc) == datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)
    assert period_start("week", None, now) == now - timedelta(days=7)
    with pytest.raises(ValueError):
        period_start("year", None, now)


async def _assistant(db, sid: str, blocks=None, turn="t1", content="a"):
    await db.add_message(sid, "assistant", content, blocks=blocks, native_turn_id=turn, actor=None)


@pytest.mark.asyncio
async def test_written_files_are_newest_first_and_unique(db):
    await db.create_session("w1", source="web", actor=None)
    call = lambda tool, path: {"type": "tool_call", "tool": tool, "input": {"file_path": path}}  # noqa: E731
    await _assistant(db, "w1", [call("Write", "/ws/a.md"), call("Read", "/ws/r.md")])
    await _assistant(db, "w1", [call("Edit", "/ws/b.py"), call("Write", "/ws/a.md")], turn="t2")
    files = await db.get_session_written_files("w1")
    assert [f["path"] for f in files] == ["/ws/a.md", "/ws/b.py"]
    assert files[0]["tool"] == "Write"


@pytest.mark.asyncio
async def test_latest_answer_at_finds_the_answer_a_message_showed(db):
    await db.create_session("f1", source="telegram", actor=None)
    await db.add_message("f1", "user", "q1", actor=None)
    await _assistant(db, "f1", turn="t1")
    first = await db.latest_answer_at("f1")
    await db._write("UPDATE messages SET created_at = '2026-11-24 09:00:00' WHERE id = ?", (first,))
    await _assistant(db, "f1", turn="t2")
    second = await db.latest_answer_at("f1")
    await db._write("UPDATE messages SET created_at = '2026-11-24 09:05:00' WHERE id = ?", (second,))
    assert await db.latest_answer_at("f1", "2026-11-24T09:00:00.500000+00:00") == first
    assert await db.latest_answer_at("f1", "2026-11-24T09:06:00+00:00") == second
    assert await db.latest_answer_at("f1", "2026-11-24T08:00:00+00:00") is None


@pytest.mark.asyncio
async def test_last_user_message_and_route_entry(db):
    await db.create_session("r1", source="telegram", actor=None)
    assert await db.last_user_message("r1") is None
    await db.add_message("r1", "user", "first", actor=None)
    await db.add_message("r1", "user", "again", blocks=[{"type": "image", "path": "x"}], actor=None)
    last = await db.last_user_message("r1")
    assert last["content"] == "again" and last["has_attachments"] is True
    await db.record_reply_route("telegram", "42", "7", "r1")
    entry = await db.lookup_reply_route_entry("telegram", "42", "7")
    assert entry["session_id"] == "r1" and entry["created_at"]
    assert await db.lookup_reply_route_entry("telegram", "42", "8") is None


@pytest.mark.asyncio
async def test_usage_since_counts_sources_and_top_sessions(db):
    await db.create_session("u1", source="web", actor=None)
    await db.create_session("u2", source="telegram", actor=None)
    await db.record_turn_usage("u1", 1, 1, 1, 1, 100, cost_usd=2.0, model="m1")
    await db.record_turn_usage("u1", 1, 1, 1, 1, 100, cost_usd=1.0, model="m2")
    await db.record_turn_usage("u2", 1, 1, 1, 1, 100, cost_usd=0.5, model="m1")
    usage = await db.get_usage_since("2000-01-01T00:00:00+00:00")
    assert usage["turns"] == 3 and usage["sessions"] == 2
    assert round(usage["cost_usd"], 2) == 3.5
    assert [r["source"] for r in usage["by_source"]] == ["web", "telegram"]
    assert usage["top_sessions"][0]["session_id"] == "u1"
    assert [m["model"] for m in await db.get_session_usage_by_model("u1")] == ["m1", "m2"]
    assert (await db.get_usage_since("2999-01-01T00:00:00+00:00"))["turns"] == 0


@pytest.mark.asyncio
async def test_status_info_reads_the_engine(db):
    await db.create_session("st", source="telegram", actor=None)
    engine = SimpleNamespace(
        db=db, config=SimpleNamespace(agent=SimpleNamespace(model="claude-x", effort="max")),
        context_view=AsyncMock(return_value={"total_tokens": 5, "max_tokens": 10}),
        queue_view=lambda sid: [{"internal": False}, {"internal": True}],
        is_session_running=lambda sid: True,
        background_agent_count=lambda sid: 2,
        process_count=lambda sid: 4,
    )
    info = await command_data.status_info(engine, "st")
    assert info["model"] == "claude-x" and info["effort_default"] == "max"
    assert (info["queued"], info["agents"], info["processes"]) == (1, 2, 4)
    assert info["context"]["max_tokens"] == 10
    assert await command_data.status_info(engine, "nope") is None


def test_match_model_like_the_web():
    models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-4-6"]
    assert match_model("CLAUDE-HAIKU-5-5", models) == ("claude-haiku-5-5", [])
    assert match_model("haiku", models) == ("claude-haiku-5-5", [])
    assert match_model("opus-5.5", models) == ("claude-opus-5-5", [])
    match, candidates = match_model("opus", models)
    assert match is None and set(candidates) == {"claude-opus-5-5", "claude-opus-4-6"}
    assert match_model("gpt", models) == (None, [])


# --------------------------------------------------------------------------- #
#  Telegram                                                                    #
# --------------------------------------------------------------------------- #


def _config(tmp_path, **extra):
    return SimpleNamespace(
        timezone="UTC", workspace=str(tmp_path),
        agent=SimpleNamespace(model="claude-opus-5-5", effort="max"),
        files=None, **extra,
    )


def _channel(tmp_path, **router):
    ch = TelegramChannel.__new__(TelegramChannel)
    ch._touch = lambda: None
    ch._is_authorized = lambda _uid: True
    ch._config = lambda: _config(tmp_path)
    ch._app = SimpleNamespace(bot=SimpleNamespace(
        send_document=AsyncMock(), edit_forum_topic=AsyncMock(),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(pending_update_count=0)),
    ))
    ch.router = SimpleNamespace(**{
        "get_last_session": AsyncMock(return_value="s-1"),
        "get_session": AsyncMock(return_value={"id": "s-1", "title": "T", "model": "claude-opus-5-5"}),
        "is_running": lambda sid: False,
        **router,
    })
    return ch


def _upd(text: str = "/x", reply_to=None, chat_type="private"):
    message = SimpleNamespace(
        text=text, reply_text=AsyncMock(), reply_to_message=reply_to, message_id=99,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42, type=chat_type, is_forum=False),
        message=message,
    )
    return update, message


def _ctx(*args):
    return SimpleNamespace(args=list(args))


@pytest.mark.asyncio
async def test_telegram_status_and_no_session(tmp_path):
    ch = _channel(tmp_path, status_info=AsyncMock(return_value=STATUS))
    update, message = _upd("/status")
    await ch._handle_status(update, _ctx())
    ch.router.status_info.assert_awaited_once_with("s-1")
    assert message.reply_text.await_args.args[0].startswith("Session s-1: Nerve commands")
    ch.router.get_last_session = AsyncMock(return_value=None)
    update, message = _upd("/status")
    await ch._handle_status(update, _ctx())
    assert message.reply_text.await_args.args == ("No active session.",)


@pytest.mark.asyncio
async def test_telegram_cost_for_the_session_and_a_period(tmp_path):
    ch = _channel(
        tmp_path,
        cost_info=AsyncMock(return_value={"turns": 1, "cost_usd": 0.5, "by_model": []}),
        period_cost_info=AsyncMock(return_value={"label": "today", "cost_usd": 3, "turns": 2, "sessions": 1}),
    )
    update, message = _upd("/cost")
    await ch._handle_cost(update, _ctx())
    assert "Cost of this session: $0.50 (1 turn)" in message.reply_text.await_args.args[0]
    update, message = _upd("/cost week")
    await ch._handle_cost(update, _ctx("WEEK"))
    ch.router.period_cost_info.assert_awaited_once_with("week", "UTC")
    update, message = _upd("/cost year")
    await ch._handle_cost(update, _ctx("year"))
    assert "Unknown period" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_diag_adds_pending_updates(tmp_path):
    ch = _channel(tmp_path, diag_info=AsyncMock(return_value={"commit": "abc12345"}))
    update, message = _upd("/diag")
    await ch._handle_diag(update, _ctx())
    assert "Telegram: live, 0 updates pending" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_rename_sets_the_title_and_the_topics(tmp_path):
    ch = _channel(
        tmp_path, update_session=AsyncMock(return_value={}),
        channel_keys_for_session=AsyncMock(return_value=["telegram:42:7", "telegram:42"]),
    )
    update, message = _upd("/rename  New   name")
    await ch._handle_rename(update, _ctx("New", "name"))
    ch.router.update_session.assert_awaited_once_with("s-1", {"title": "New name"})
    ch._app.bot.edit_forum_topic.assert_awaited_once()
    assert ch._app.bot.edit_forum_topic.await_args.kwargs["message_thread_id"] == 7
    assert message.reply_text.await_args.args == ("Renamed to “New name”.",)
    ch.router.update_session = AsyncMock(side_effect=CommandRefused("no"))
    update, message = _upd("/rename x")
    await ch._handle_rename(update, _ctx("x"))
    assert message.reply_text.await_args.args == ("no",)


@pytest.mark.asyncio
async def test_telegram_fork_switches_a_chat_without_topics(tmp_path):
    ch = _channel(
        tmp_path, fork_session=AsyncMock(return_value={"id": "fork-1", "title": "Fork of T"}),
        switch_session=AsyncMock(),
    )
    ch._chat_may_have_topics = lambda chat: False
    update, message = _upd("/fork")
    await ch._handle_fork(update, _ctx())
    ch.router.fork_session.assert_awaited_once_with("s-1", at_message_id=None, title=None, source="telegram")
    ch.router.switch_session.assert_awaited_once_with("telegram:42", "fork-1")
    assert "this chat is now on fork-1" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_fork_during_a_turn_starts_before_it(tmp_path):
    ch = _channel(
        tmp_path, fork_session=AsyncMock(return_value={"id": "fork-2"}), switch_session=AsyncMock(),
        latest_answer_at=AsyncMock(return_value=55), is_running=lambda sid: True,
    )
    ch._chat_may_have_topics = lambda chat: False
    update, message = _upd("/fork Side")
    await ch._handle_fork(update, _ctx("Side"))
    ch.router.fork_session.assert_awaited_once_with("s-1", at_message_id=55, title="Side", source="telegram")
    assert "from before the running turn" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_fork_as_a_reply_forks_at_that_message_in_a_new_topic(tmp_path):
    ch = _channel(
        tmp_path, fork_session=AsyncMock(return_value={"id": "fork-3", "title": "F"}),
        reply_route_entry=AsyncMock(return_value={"session_id": "s-9", "created_at": "2026-11-24T09:00:00+00:00"}),
        latest_answer_at=AsyncMock(return_value=77),
    )
    ch._chat_may_have_topics = lambda chat: True
    ch.open_session_topic = AsyncMock(return_value=SimpleNamespace(error=None))
    update, message = _upd("/fork", reply_to=SimpleNamespace(message_id=31))
    await ch._handle_fork(update, _ctx())
    ch.router.reply_route_entry.assert_awaited_once_with("telegram", 42, 31)
    ch.router.latest_answer_at.assert_awaited_once_with("s-9", "2026-11-24T09:00:00+00:00")
    ch.router.fork_session.assert_awaited_once_with("s-9", at_message_id=77, title=None, source="telegram")
    ch.open_session_topic.assert_awaited_once_with("fork-3", chat_id=42)
    assert message.reply_text.await_args.args == ("Forked from that message into a new topic: F",)


@pytest.mark.asyncio
async def test_telegram_fork_as_a_reply_to_an_unknown_message_says_so(tmp_path):
    ch = _channel(tmp_path, reply_route_entry=AsyncMock(return_value=None), fork_session=AsyncMock())
    update, message = _upd("/fork", reply_to=SimpleNamespace(message_id=31))
    await ch._handle_fork(update, _ctx())
    ch.router.fork_session.assert_not_awaited()
    assert "no record of that message" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_retry_resends_the_last_message(tmp_path):
    ch = _channel(tmp_path, last_user_message=AsyncMock(
        return_value={"content": "do the thing", "has_attachments": True}))
    ch._deliver_message = AsyncMock()
    update, message = _upd("/retry")
    await ch._handle_retry(update, _ctx())
    ch._deliver_message.assert_awaited_once_with(update, "do the thing", session_id="s-1")
    assert "Attachments aren't sent again." in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_retry_waits_for_a_running_turn(tmp_path):
    ch = _channel(tmp_path, is_running=lambda sid: True, last_user_message=AsyncMock())
    ch._deliver_message = AsyncMock()
    update, message = _upd("/retry")
    await ch._handle_retry(update, _ctx())
    ch._deliver_message.assert_not_awaited()
    assert "A turn is running" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_btw_sends_the_answer_with_a_footer(tmp_path):
    ch = _channel(tmp_path, side_question=AsyncMock(return_value={
        "answer": "It is at step 3.", "model": "claude-opus-5-5", "cost_usd": 0.04,
    }))
    ch.send = AsyncMock()
    ch.format_response = lambda text: text
    ch._keep_typing = AsyncMock()
    update, message = _upd("/btw where are\nwe?")
    await ch._handle_btw(update, _ctx("where", "are", "we?"))
    ch.router.side_question.assert_awaited_once_with("s-1", "where are\nwe?")
    sent = ch.send.await_args.args[0]
    assert sent.text.startswith("It is at step 3.") and "not added to the session" in sent.text
    assert sent.target == "42"


@pytest.mark.asyncio
async def test_telegram_btw_explains_a_failure(tmp_path):
    ch = _channel(tmp_path, side_question=AsyncMock(side_effect=sq.SideQuestionError("no key")))
    ch._keep_typing = AsyncMock()
    update, message = _upd("/btw x")
    await ch._handle_btw(update, _ctx("x"))
    assert message.reply_text.await_args.args == ("no key",)
    update, message = _upd("/btw")
    await ch._handle_btw(update, _ctx())
    assert message.reply_text.await_args.args == ("Usage: /btw <question>",)


MODELS = {"options": [{"id": "claude", "models": ["claude-opus-5-5", "claude-haiku-5-5"]}],
          "effort_levels": {"claude-opus-5-5": ["low", "high", "max"]}}


@pytest.mark.asyncio
async def test_telegram_model_buttons_and_switch(tmp_path):
    ch = _channel(tmp_path, list_models=AsyncMock(return_value=MODELS), update_session=AsyncMock())
    update, message = _upd("/model")
    await ch._handle_model(update, _ctx())
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels == ["✓ opus-5-5", "haiku-5-5"]
    update, message = _upd("/model haiku")
    await ch._handle_model(update, _ctx("haiku"))
    ch.router.update_session.assert_awaited_once_with("s-1", {"model": "claude-haiku-5-5"})
    update, message = _upd("/model gpt")
    await ch._handle_model(update, _ctx("gpt"))
    assert "is not a model here" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_model_button_switches(tmp_path):
    ch = _channel(tmp_path, list_models=AsyncMock(return_value=MODELS), update_session=AsyncMock())
    update, message = _upd("/model")
    await ch._handle_model(update, _ctx())
    data = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[1][0].callback_data
    assert len(data.encode()) <= 64
    ch._safe_edit = AsyncMock()
    query = SimpleNamespace(data=data, message=None, answer=AsyncMock())
    await ch._handle_command_button(query)
    ch.router.update_session.assert_awaited_once_with("s-1", {"model": "claude-haiku-5-5"})


@pytest.mark.asyncio
async def test_telegram_effort_levels_default_and_refusal(tmp_path):
    ch = _channel(tmp_path, list_models=AsyncMock(return_value=MODELS), update_session=AsyncMock())
    update, message = _upd("/effort")
    await ch._handle_effort(update, _ctx())
    rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [b.text for b in rows[0]] == ["low", "high", "max"]
    assert rows[1][0].text == "✓ default (max)"
    update, message = _upd("/effort HIGH")
    await ch._handle_effort(update, _ctx("HIGH"))
    ch.router.update_session.assert_awaited_once_with("s-1", {"effort": "high"})
    update, message = _upd("/effort medium")
    await ch._handle_effort(update, _ctx("medium"))
    assert "Unknown effort" in message.reply_text.await_args.args[0]
    ch.router.update_session = AsyncMock(side_effect=CommandRefused("2 background agent(s) are still running"))
    update, message = _upd("/effort low")
    await ch._handle_effort(update, _ctx("low"))
    assert message.reply_text.await_args.args == ("2 background agent(s) are still running",)


@pytest.mark.asyncio
async def test_telegram_ps_buttons_stop_agents_and_processes(tmp_path):
    info = {"agents": [{"id": "agent-" + "x" * 40, "label": "Explore"}],
            "processes": [{"pid": 7, "role": "call", "cmd": "sleep 9", "start_ticks": 123}]}
    ch = _channel(tmp_path, ps_info=AsyncMock(return_value=info),
                  stop_agent=AsyncMock(return_value=["a"]), signal_process=AsyncMock(return_value={}))
    update, message = _upd("/ps")
    await ch._handle_ps(update, _ctx())
    rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == ["⏹ 1. Explore", "⏹ 2. pid 7", "↻ Refresh"]
    assert all(len(r[0].callback_data.encode()) <= 64 for r in rows)
    ch._safe_edit = AsyncMock()
    for row, check in ((rows[0], ch.router.stop_agent), (rows[1], ch.router.signal_process)):
        query = SimpleNamespace(data=row[0].callback_data, message=None, answer=AsyncMock())
        await ch._handle_command_button(query)
    ch.router.stop_agent.assert_awaited_once_with("s-1", "agent-" + "x" * 40)
    ch.router.signal_process.assert_awaited_once_with("s-1", 7, 123)


@pytest.mark.asyncio
async def test_telegram_files_sends_a_written_file(tmp_path):
    f = tmp_path / "report.md"
    f.write_text("hi")
    ch = _channel(tmp_path, written_files=AsyncMock(return_value=[
        {"path": "report.md", "abs_path": str(f), "exists": True, "size": 2, "at": None},
        {"path": "gone.md", "abs_path": str(tmp_path / "gone.md"), "exists": False},
    ]))
    update, message = _upd("/files")
    await ch._handle_files(update, _ctx())
    rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == ["📄 1. report.md"]
    ch._address_of = lambda chat_id, msg: SimpleNamespace(chat_id=42, thread_id=None)
    query = SimpleNamespace(data=rows[0][0].callback_data, answer=AsyncMock(),
                            message=SimpleNamespace(chat=SimpleNamespace(id=42)))
    await ch._handle_command_button(query)
    assert ch._app.bot.send_document.await_args.kwargs["filename"] == "report.md"


@pytest.mark.asyncio
async def test_telegram_file_lists_folders_sends_files_and_keeps_to_the_workspace(tmp_path):
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "a.md").write_text("a")
    (tmp_path / "top.txt").write_text("t")
    ch = _channel(tmp_path)
    update, message = _upd("/file")
    await ch._handle_file(update, _ctx())
    rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == ["📁 drafts", "📄 top.txt (1 B)"]
    update, message = _upd("/file drafts/a.md")
    await ch._handle_file(update, _ctx("drafts/a.md"))
    assert ch._app.bot.send_document.await_args.kwargs["filename"] == "a.md"
    update, message = _upd("/file ../../etc/passwd")
    await ch._handle_file(update, _ctx("../../etc/passwd"))
    assert message.reply_text.await_args.args == ("That path is outside the workspace, or hidden.",)
    ch._safe_edit = AsyncMock()
    query = SimpleNamespace(data=rows[0][0].callback_data, answer=AsyncMock(), message=None)
    await ch._handle_command_button(query)
    text, markup = ch._safe_edit.await_args.args[1:3]
    assert text.startswith("drafts/") and markup.inline_keyboard[0][0].text == "⬆ .."


@pytest.mark.asyncio
async def test_telegram_file_honours_files_exclude(tmp_path):
    (tmp_path / "secret.env").write_text("x")
    ch = _channel(tmp_path)
    ch._config = lambda: _config(tmp_path) if False else SimpleNamespace(
        **{**vars(_config(tmp_path)), "files": SimpleNamespace(exclude=["*.env"])})
    update, message = _upd("/file secret.env")
    await ch._handle_file(update, _ctx("secret.env"))
    assert message.reply_text.await_args.args == ("That path is outside the workspace, or hidden.",)


@pytest.mark.asyncio
async def test_an_expired_file_button_says_so(tmp_path):
    ch = _channel(tmp_path)
    query = SimpleNamespace(data="cmdfile:000000000000", answer=AsyncMock(), message=None)
    await ch._handle_command_button(query)
    assert "expired" in query.answer.await_args.args[0]


# --------------------------------------------------------------------------- #
#  /btw                                                                        #
# --------------------------------------------------------------------------- #


def test_the_side_transcript_keeps_the_newest_messages():
    messages = [{"role": "user", "content": "x" * 30_000}, {"role": "assistant", "content": "y" * 40_000},
                {"role": "user", "content": "last"}]
    text = sq._transcript(messages)
    assert text.endswith("Person: last")
    assert len(text) <= sq.MAX_CONTEXT_CHARS + 50
    assert "[…]" in text  # a long message keeps its start and its end


@pytest.mark.asyncio
async def test_ask_side_question_needs_api_access_and_a_question(monkeypatch):
    config = MagicMock()
    with pytest.raises(sq.SideQuestionError, match="Ask a question"):
        await sq.ask_side_question(config, {}, [], "  ")
    monkeypatch.setattr(sq, "_api_client_usable", lambda c: False)
    with pytest.raises(sq.SideQuestionError, match="API access"):
        await sq.ask_side_question(config, {}, [], "why?")


@pytest.mark.asyncio
async def test_ask_side_question_answers_with_the_sessions_model(monkeypatch):
    monkeypatch.setattr(sq, "_api_client_usable", lambda c: True)
    seen = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def messages(self):
            return self

        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Step 3.")],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=10), stop_reason="end_turn",
            )

    config = SimpleNamespace(agent=SimpleNamespace(model="claude-fallback"),
                             create_async_anthropic_client=lambda **kw: Client())
    result = await sq.ask_side_question(
        config, {"id": "s", "model": "claude-opus-5-5"},
        [{"role": "user", "content": "build it"}], "where are we?",
    )
    assert result["answer"] == "Step 3." and result["model"] == "claude-opus-5-5"
    assert "<question>\nwhere are we?\n</question>" in seen["messages"][0]["content"]
    assert result["cost_usd"] > 0
    assert sq.side_model(config, {"model": "gpt-6.2"}) == "claude-fallback"


# --------------------------------------------------------------------------- #
#  Web routes                                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture
def routes_app(bypass_auth, monkeypatch, db):
    from nerve.gateway.auth import require_auth
    from nerve.gateway.routes import commands as routes

    engine = SimpleNamespace(db=db)
    monkeypatch.setattr(routes, "get_deps", lambda: SimpleNamespace(engine=engine, db=db))
    monkeypatch.setattr(routes, "get_config", lambda: SimpleNamespace(timezone="UTC"))
    app = FastAPI()
    app.include_router(routes.router)
    bypass_auth(app)
    app.dependency_overrides[require_auth] = lambda: None
    return app, routes


@pytest.mark.asyncio
async def test_the_text_route_answers_status_cost_and_diag(routes_app, monkeypatch):
    app, routes = routes_app
    monkeypatch.setattr(routes.command_data, "status_info", AsyncMock(return_value=STATUS))
    monkeypatch.setattr(routes.command_data, "cost_info", AsyncMock(return_value=None))
    monkeypatch.setattr(routes.command_data, "diag_info", AsyncMock(return_value={"commit": "c0ffee00"}))
    monkeypatch.setattr(routes.command_data, "period_cost_info", AsyncMock(
        return_value={"label": "today", "cost_usd": 1, "turns": 1, "sessions": 1}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/commands/status/text", params={"session_id": "s-1"})
        assert r.status_code == 200 and r.json()["text"].startswith("Session s-1")
        assert (await c.get("/api/commands/cost/text", params={"session_id": "gone"})).status_code == 404
        assert "today" in (await c.get("/api/commands/cost/text", params={"args": "today"})).json()["text"]
        assert (await c.get("/api/commands/cost/text", params={"args": "year"})).status_code == 400
        assert (await c.get("/api/commands/diag/text")).json()["text"].startswith("Nerve c0ffee00")
        assert (await c.get("/api/commands/model/text")).status_code == 404


@pytest.mark.asyncio
async def test_the_btw_route(routes_app, monkeypatch, db):
    app, routes = routes_app
    await db.create_session("b1", source="web", actor=None)
    ask = AsyncMock(return_value={"answer": "ok", "model": "m", "input_tokens": 1,
                                  "output_tokens": 1, "cost_usd": 0.0})
    monkeypatch.setattr(routes, "ask_side_question", ask)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/sessions/nope/btw", json={"question": "q"})).status_code == 404
        r = await c.post("/api/sessions/b1/btw", json={"question": "q"})
        assert r.status_code == 200 and r.json()["answer"] == "ok"
        ask.side_effect = sq.SideQuestionError("The model call failed: X.")
        assert (await c.post("/api/sessions/b1/btw", json={"question": "q"})).status_code == 502
        ask.side_effect = sq.SideQuestionError("needs API access")
        assert (await c.post("/api/sessions/b1/btw", json={"question": "q"})).status_code == 400


def test_diag_resolves_the_routes_query_defaults():
    from fastapi import Query

    async def route(usage_days: int = Query(7, ge=1), plain: int = 3):
        return usage_days

    assert command_data._route_defaults(route) == {"usage_days": 7}


def test_the_fallback_exclusion_matches_like_the_files_page():
    from nerve.channels.telegram_commands import _is_excluded

    assert _is_excluded(("tmp", "a", "b.txt"), False, ["tmp"])
    assert _is_excluded(("x", "node_modules"), True, ["node_modules"])
    assert not _is_excluded(("build",), False, ["build/"])
    assert _is_excluded(("repos", "big"), True, ["repos/big"])
    assert not _is_excluded(("drafts", "a.md"), False, ["*.env"])

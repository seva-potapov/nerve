"""The chat commands: one list for Telegram's menu and the web composer
(``nerve/commands.py``, ``GET /api/commands``), and Telegram's ``/compact``
and ``/context``."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from telegram.ext import CommandHandler

from nerve.agent.engine import CompactError, NotAcceptingTurnsError
from nerve.channels.context_text import compact_done_text, context_text, format_tokens
from nerve.channels.router import ChannelRouter
from nerve.channels.telegram import TelegramChannel
from nerve.commands import COMMANDS, commands_for, telegram_menu

VIEW = {
    "session_id": "s-1", "live": True, "can_compact": True,
    "total_tokens": 33005, "max_tokens": 1_000_000, "auto_compact_threshold": 967_000,
    "categories": [
        {"name": "System prompt", "tokens": 170},
        {"name": "Messages", "tokens": 18000},
        {"name": "System tools", "tokens": 15149},
        {"name": "MCP tools", "tokens": 0},
    ],
}
REPORT = {"trigger": "manual", "pre_tokens": 14548, "post_tokens": 2750}


# --------------------------------------------------------------------------- #
#  The list                                                                    #
# --------------------------------------------------------------------------- #


def test_names_are_unique_and_valid_telegram_commands():
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names))
    for c in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.name), c.name
        assert c.args == "" or re.fullmatch(r"<[a-z ]+>|\[[a-z ]+\]", c.args), c.args
        assert c.help and len(c.help) <= 120


def test_each_surface_has_its_commands():
    telegram = {c.name for c in commands_for("telegram")}
    web = {c.name for c in commands_for("web")}
    assert {"compact", "context", "new", "stop", "star", "unstar", "sessions", "plans", "doctor"} <= telegram & web
    assert {"restart", "pair", "start", "reply", "session"} <= telegram - web
    assert {"fork", "model", "help"} <= web - telegram
    with pytest.raises(ValueError):
        commands_for("slack")


def test_the_telegram_menu_has_argument_hints_in_the_description():
    menu = dict(telegram_menu())
    assert menu["compact"] == "Summarize the conversation to free context [instructions]"
    assert menu["stop"] == "Stop the running turn"
    assert all(1 <= len(d) <= 256 for d in menu.values())


def test_every_menu_command_has_a_telegram_handler_and_back():
    ch = TelegramChannel.__new__(TelegramChannel)
    ch._config = lambda: SimpleNamespace(telegram=SimpleNamespace(bot_token="123456:TEST-token"))
    app = ch._build_application()
    handled = {
        name
        for group in app.handlers.values()
        for h in group if isinstance(h, CommandHandler)
        for name in h.commands
    }
    assert handled == {c.name for c in commands_for("telegram")}


@pytest.mark.asyncio
async def test_the_menu_is_registered_and_a_failure_is_only_logged(caplog):
    ch = TelegramChannel.__new__(TelegramChannel)
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    ch._app = SimpleNamespace(bot=bot)
    await ch._register_command_menu()
    (commands,) = bot.set_my_commands.await_args.args
    assert [(c.command, c.description) for c in commands] == telegram_menu()

    bot.set_my_commands = AsyncMock(side_effect=RuntimeError("Telegram down"))
    await ch._register_command_menu()
    assert "Could not register the Telegram command menu" in caplog.text


@pytest.mark.asyncio
async def test_the_commands_route_lists_a_surface(bypass_auth):
    from nerve.gateway.auth import require_auth
    from nerve.gateway.routes.commands import router

    app = FastAPI()
    app.include_router(router)
    bypass_auth(app)
    app.dependency_overrides[require_auth] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nerve-test",
    ) as client:
        web = (await client.get("/api/commands")).json()
        assert web["surface"] == "web"
        assert web["commands"][2] == {
            "name": "compact", "args": "[instructions]",
            "help": "Summarize the conversation to free context",
        }
        assert [c["name"] for c in web["commands"]] == [c.name for c in commands_for("web")]
        telegram = (await client.get("/api/commands?surface=telegram")).json()
        assert "restart" in {c["name"] for c in telegram["commands"]}
        assert (await client.get("/api/commands?surface=fax")).status_code == 400


# --------------------------------------------------------------------------- #
#  The answers                                                                 #
# --------------------------------------------------------------------------- #


def test_token_counts_read_like_the_context_bar():
    assert [format_tokens(n) for n in (0, 950, 1000, 2750, 14548, 99_999, 100_000, 998_415, 1_000_000, 1_260_000)] == [
        "0", "950", "1K", "2.8K", "14.5K", "100K", "100K", "998K", "1M", "1.3M",
    ]


def test_a_compact_says_before_and_after():
    assert compact_done_text(REPORT, VIEW) == "Compacted: 14.5K → 2.8K tokens (of 1M)."
    assert compact_done_text({}, VIEW) == "Compacted. Context now: 33K tokens (of 1M)."
    assert compact_done_text(None, None) == "Compacted."


def test_context_lists_the_biggest_categories():
    assert context_text(VIEW).splitlines() == [
        "Context: 33K of 1M tokens (3%)",
        "Auto-compact at 967K",
        "• Messages: 18K",
        "• System tools: 15.1K",
        "• System prompt: 170",
        "/compact [instructions] summarizes the conversation.",
    ]
    idle = {**VIEW, "live": False, "categories": [], "auto_compact_threshold": None}
    assert context_text(idle).splitlines()[1:] == [
        "Estimate from the last turn: the session's runtime isn't running.",
        "/compact [instructions] summarizes the conversation.",
    ]


# --------------------------------------------------------------------------- #
#  Telegram /compact and /context                                              #
# --------------------------------------------------------------------------- #


def _telegram(**router):
    ch = TelegramChannel.__new__(TelegramChannel)
    ch._touch = lambda: None
    ch._is_authorized = lambda _uid: True
    ch._app = None  # send_typing is a no-op
    ch.router = SimpleNamespace(**{
        "get_last_session": AsyncMock(return_value="s-1"),
        "compact_session": AsyncMock(return_value=REPORT),
        "session_context": AsyncMock(return_value=VIEW),
        **router,
    })
    return ch


def _update():
    note = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=note))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=4242),
        effective_chat=SimpleNamespace(id=4242),
        message=message,
    )
    return update, message, note


@pytest.mark.asyncio
async def test_telegram_compact_edits_its_note_with_the_result():
    ch = _telegram()
    update, message, note = _update()
    await ch._handle_compact(update, SimpleNamespace(args=["keep", "PR", "numbers"]))
    assert message.reply_text.await_args.args == ("Compacting…",)
    ch.router.get_last_session.assert_awaited_once_with("telegram:4242")
    ch.router.compact_session.assert_awaited_once_with("s-1", "keep PR numbers")
    note.edit_text.assert_awaited_once_with("Compacted: 14.5K → 2.8K tokens (of 1M).")


@pytest.mark.asyncio
async def test_telegram_compact_without_instructions_sends_none():
    ch = _telegram()
    update, _message, _note = _update()
    await ch._handle_compact(update, SimpleNamespace(args=[]))
    ch.router.compact_session.assert_awaited_once_with("s-1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CompactError("busy", "The agent is busy. Try again when the turn ends."),
    CompactError("nothing_to_compact", "Nothing to compact yet."),
    CompactError("unavailable", "Compact needs a Claude session with a conversation."),
    CompactError("failed", "Compact failed: boom"),
    NotAcceptingTurnsError(),
])
async def test_telegram_compact_says_why_it_did_not_compact(error):
    ch = _telegram(compact_session=AsyncMock(side_effect=error))
    update, _message, note = _update()
    await ch._handle_compact(update, SimpleNamespace(args=[]))
    note.edit_text.assert_awaited_once_with(str(error))
    ch.router.session_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_compact_replies_when_the_note_cannot_be_edited():
    ch = _telegram()
    update, message, note = _update()
    note.edit_text.side_effect = RuntimeError("message to edit not found")
    await ch._handle_compact(update, SimpleNamespace(args=[]))
    assert message.reply_text.await_args.args == ("Compacted: 14.5K → 2.8K tokens (of 1M).",)


@pytest.mark.asyncio
async def test_telegram_compact_and_context_without_a_session():
    ch = _telegram(get_last_session=AsyncMock(return_value=None))
    for handler in (ch._handle_compact, ch._handle_context):
        update, message, _note = _update()
        await handler(update, SimpleNamespace(args=[]))
        message.reply_text.assert_awaited_once_with("No active session.")
    ch.router.compact_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_context_answers_with_the_view():
    ch = _telegram()
    update, message, _note = _update()
    await ch._handle_context(update, SimpleNamespace(args=[]))
    ch.router.session_context.assert_awaited_once_with("s-1")
    message.reply_text.assert_awaited_once_with(context_text(VIEW))


@pytest.mark.asyncio
async def test_the_router_compacts_without_an_actor():
    engine = SimpleNamespace(
        compact_session=AsyncMock(return_value=REPORT),
        context_view=AsyncMock(return_value=VIEW),
    )
    router = ChannelRouter(engine)
    assert await router.compact_session("s-1", "keep it") == REPORT
    engine.compact_session.assert_awaited_once_with("s-1", actor=None, instructions="keep it")
    assert await router.session_context("s-1") == VIEW

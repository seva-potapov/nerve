"""Tests for live locations on Telegram (nerve.channels.live_locations).

A live location arrives once as a message, then each new position as an edit
of it. The channel keeps the positions without starting a turn, and the
chat's next message carries the latest ones.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram import Location, Update, User

from nerve.channels.live_locations import LINGER, SHOWN, STALE, LiveLocations
from nerve.channels.telegram import TelegramChannel
from nerve.config import NerveConfig

_T0 = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
_KEY = "telegram:42"
_UTC = ZoneInfo("UTC")


class _Clock:
    def __init__(self, now: datetime = _T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _live(lat=1.0, lon=2.0, *, period=900, accuracy=None) -> Location:
    return Location(
        latitude=lat, longitude=lon, live_period=period, horizontal_accuracy=accuracy,
    )


def _message(location, message_id=7, date=_T0) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, date=date, location=location)


def _tracked(clock: _Clock | None = None, *, period=900) -> tuple[LiveLocations, _Clock]:
    """A live location shared at _T0 from (1, 2), as its first message."""
    clock = clock or _Clock()
    live = LiveLocations(clock=clock)
    live.record(_KEY, _message(_live(period=period)))
    return live, clock


def _edit(live: LiveLocations, seconds: int, lat: float, *, period=900, accuracy=None):
    live.record(
        _KEY, _message(_live(lat, 2.0, period=period, accuracy=accuracy)),
        at=_T0 + timedelta(seconds=seconds),
    )


# --------------------------------------------------------------------------- #
#  What the next message says                                                  #
# --------------------------------------------------------------------------- #

def test_the_next_message_lists_the_positions_in_order():
    live, clock = _tracked()
    _edit(live, 30, 1.0001)
    _edit(live, 60, 1.0002, accuracy=5)
    clock.now = _T0 + timedelta(seconds=70)

    assert live.describe(_KEY, _UTC) == (
        "[Live location since 10:00:00 UTC, shared until 10:15:00 (3 positions, latest last):\n"
        "10:00:00 1.000000, 2.000000\n"
        "10:00:30 1.000100, 2.000000\n"
        "10:01:00 1.000200, 2.000000 (±5 m)]"
    )


def test_times_are_local_to_the_given_zone():
    live, _clock = _tracked()

    assert live.describe(_KEY, ZoneInfo("Australia/Sydney")).startswith(
        "[Live location since 21:00:00 AEDT, shared until 21:15:00 (1 position, latest last):\n"
        "21:00:00 1.000000, 2.000000",
    )


def test_it_is_said_again_with_every_message_while_shared():
    live, clock = _tracked()
    first = live.describe(_KEY, _UTC)
    _edit(live, 30, 1.0001)
    clock.now = _T0 + timedelta(seconds=40)

    assert first.endswith("10:00:00 1.000000, 2.000000]")
    assert live.describe(_KEY, _UTC).endswith("10:00:30 1.000100, 2.000000]")


def test_only_the_latest_positions_are_listed_after_the_first():
    live, clock = _tracked()
    for n in range(1, SHOWN + 6):
        _edit(live, n, 1.0 + n / 10_000)
    clock.now = _T0 + timedelta(seconds=SHOWN + 10)

    lines = live.describe(_KEY, _UTC).split("\n")

    assert f"({SHOWN + 6} positions, latest last):" in lines[0]
    assert lines[1] == "10:00:00 1.000000, 2.000000"
    assert lines[2] == "… 5 more …"
    assert len(lines) == 3 + SHOWN
    assert lines[3].startswith("10:00:06 1.000600")
    assert lines[-1] == f"10:00:{SHOWN + 5:02d} {1.0 + (SHOWN + 5) / 10_000:.6f}, 2.000000]"


def test_shared_until_stopped_has_no_end():
    live, clock = _tracked(period=0x7FFFFFFF)
    clock.now = _T0 + timedelta(hours=5)
    _edit(live, 5 * 3600 - 10, 1.5, period=0x7FFFFFFF)

    assert "shared until stopped (2 positions" in live.describe(_KEY, _UTC)


def test_a_period_given_as_a_timedelta_is_read():
    # PTB reports durations as timedelta when PTB_TIMEDELTA is set.
    live = LiveLocations(clock=_Clock())
    location = SimpleNamespace(latitude=1.0, longitude=2.0, live_period=timedelta(minutes=15))
    live.record(_KEY, _message(location))

    assert "shared until 10:15:00" in live.describe(_KEY, _UTC)


# --------------------------------------------------------------------------- #
#  The end of the sharing                                                      #
# --------------------------------------------------------------------------- #

def test_a_stop_is_the_last_position_and_is_said_once():
    # Telegram drops live_period from the edit once the sharing stops.
    live, clock = _tracked()
    _edit(live, 30, 1.0001)
    stop = Location(latitude=1.0002, longitude=2.0)
    live.record(_KEY, _message(stop), at=_T0 + timedelta(seconds=45))
    clock.now = _T0 + timedelta(seconds=50)

    told = live.describe(_KEY, _UTC)

    assert told.startswith(
        "[Live location since 10:00:00 UTC, stopped at 10:00:45 (3 positions, latest last):",
    )
    assert told.endswith("10:00:45 1.000200, 2.000000]")
    assert live.describe(_KEY, _UTC) == ""


def test_an_expired_live_location_is_said_once():
    live, clock = _tracked()
    clock.now = _T0 + timedelta(minutes=16)

    assert "ended at 10:15:00 (1 position" in live.describe(_KEY, _UTC)
    assert live.describe(_KEY, _UTC) == ""


def test_an_ended_live_location_nobody_asked_about_is_forgotten():
    live, clock = _tracked()
    clock.now = _T0 + timedelta(minutes=15) + LINGER + timedelta(seconds=1)

    assert live.describe(_KEY, _UTC) == ""


def test_a_silent_live_location_is_forgotten():
    live, clock = _tracked(period=0x7FFFFFFF)
    clock.now = _T0 + STALE + timedelta(seconds=1)

    assert live.describe(_KEY, _UTC) == ""


# --------------------------------------------------------------------------- #
#  What is kept                                                                #
# --------------------------------------------------------------------------- #

def test_a_static_location_is_not_kept():
    live = LiveLocations(clock=_Clock())
    live.record(_KEY, _message(Location(latitude=1.0, longitude=2.0)))

    assert live.describe(_KEY, _UTC) == ""


def test_an_edit_of_a_message_not_seen_starts_the_track():
    # After a restart the first message is gone; the edit still says when
    # the sharing started (its date) and for how long.
    clock = _Clock(_T0 + timedelta(seconds=130))
    live = LiveLocations(clock=clock)
    live.record(_KEY, _message(_live(1.5, 2.5)), at=_T0 + timedelta(seconds=120))

    assert live.describe(_KEY, _UTC) == (
        "[Live location since 10:00:00 UTC, shared until 10:15:00 (1 position, latest last):\n"
        "10:02:00 1.500000, 2.500000]"
    )


def test_the_live_location_being_delivered_is_skipped():
    live, _clock = _tracked()

    assert live.describe(_KEY, _UTC, skip=7) == ""
    assert live.describe(_KEY, _UTC, skip=8) != ""


def test_chats_are_kept_apart():
    live, _clock = _tracked()

    assert live.describe("telegram:43", _UTC) == ""
    assert live.describe(_KEY, _UTC) != ""


def test_stand_ins_without_coordinates_or_an_id_are_ignored():
    # The other Telegram tests build messages from MagicMock; every attribute
    # is a stand-in there, and nothing may be kept for them.
    live = LiveLocations(clock=_Clock())
    live.record(_KEY, MagicMock())
    live.record(_KEY, _message(_live(), message_id=MagicMock()))
    live.record(_KEY, _message(None))

    assert live.describe(_KEY, _UTC) == ""


# --------------------------------------------------------------------------- #
#  Through the channel                                                         #
# --------------------------------------------------------------------------- #

_USER = 999
_CHAT = 42


def _channel(db, clock: _Clock) -> TelegramChannel:
    cfg = NerveConfig()
    cfg.timezone = "UTC"
    cfg.telegram.allowed_users = [_USER]
    cfg.telegram.reply_routes_to_origin_session = False
    router = MagicMock()
    router.engine.db = db
    router.observe = AsyncMock(return_value=True)
    router.handle_message = AsyncMock()
    router.get_session = AsyncMock(return_value=None)
    router.get_active_session = AsyncMock(return_value="active1")
    ch = TelegramChannel(lambda: cfg, router)
    ch._extract_image = AsyncMock(return_value=None)
    ch._extract_sticker = AsyncMock(return_value=(None, None))
    ch._extract_document = AsyncMock(return_value=([], None))
    ch._live_locations = LiveLocations(clock=clock)
    return ch


def _update(*, message_id, text=None, location=None, user=_USER):
    update = MagicMock()
    update.effective_chat.id = _CHAT
    update.effective_chat.type = "private"
    update.effective_user.id = user
    message = update.message
    message.text = text
    message.caption = None
    message.message_id = message_id
    message.date = _T0
    message.media_group_id = None
    message.forward_origin = None
    message.quote = None
    message.location = location
    message.venue = None
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    return update


def _edit_update(location, *, seconds, message_id=900, user=_USER):
    update = MagicMock()
    update.effective_chat.id = _CHAT
    update.effective_user.id = user
    update.message = None
    update.edited_message = SimpleNamespace(
        message_id=message_id, date=_T0, location=location,
        edit_date=_T0 + timedelta(seconds=seconds),
    )
    return update


def _texts(ch) -> list[str]:
    return [call.args[0].text for call in ch.router.handle_message.await_args_list]


@pytest.mark.asyncio
async def test_edits_start_no_turn_and_the_next_message_carries_them(db):
    clock = _Clock()
    ch = _channel(db, clock)

    await ch._handle_message(_update(message_id=900, location=_live(1.5, 2.5)), None)
    await ch._handle_message(_edit_update(_live(1.5001, 2.5), seconds=20), None)
    await ch._handle_message(_edit_update(_live(1.5002, 2.5), seconds=40), None)
    clock.now = _T0 + timedelta(seconds=50)
    await ch._handle_message(_update(message_id=901, text="how fast am I going?"), None)

    assert _texts(ch) == [
        "[Live location: 1.500000, 2.500000, shared for 15 min; "
        "later positions come with the user's next message]",
        "[Live location since 10:00:00 UTC, shared until 10:15:00 (3 positions, latest last):\n"
        "10:00:00 1.500000, 2.500000\n"
        "10:00:20 1.500100, 2.500000\n"
        "10:00:40 1.500200, 2.500000]\n\n"
        "how fast am I going?",
    ]


@pytest.mark.asyncio
async def test_a_stranger_s_edit_is_not_kept(db):
    clock = _Clock()
    ch = _channel(db, clock)

    await ch._handle_message(_edit_update(_live(), seconds=20, user=1234), None)
    await ch._handle_message(_update(message_id=901, text="hi"), None)

    assert _texts(ch) == ["hi"]


@pytest.mark.asyncio
async def test_an_edited_text_changes_nothing(db):
    clock = _Clock()
    ch = _channel(db, clock)
    edited_text = _edit_update(None, seconds=20)
    edited_text.edited_message.text = "typo fixed"

    await ch._handle_message(edited_text, None)
    await ch._handle_message(_update(message_id=901, text="hi"), None)

    assert _texts(ch) == ["hi"]


def _application(ch: TelegramChannel):
    """The real PTB dispatch table (as in test_telegram_location.py)."""
    ch.config.telegram.bot_token = "123:ABC"
    app = ch._build_application()
    app._initialized = True
    app.bot._bot_user = User(id=1000, first_name="Nerve", is_bot=True, username="nerve_bot")
    return app


def _raw_update(app, kind: str, update_id: int, **fields):
    message = {
        "message_id": 7,
        "date": int(_T0.timestamp()),
        "chat": {"id": _CHAT, "type": "private", "first_name": "Alice"},
        "from": {"id": _USER, "is_bot": False, "first_name": "Alice"},
        **fields,
    }
    return Update.de_json({"update_id": update_id, kind: message}, app.bot)


@pytest.mark.asyncio
async def test_the_handler_table_keeps_the_edits_for_the_next_message(db):
    clock = _Clock(_T0 + timedelta(seconds=90))
    ch = _channel(db, clock)
    app = _application(ch)
    first = {"latitude": 1.5, "longitude": 2.5, "live_period": 900}
    moved = {"latitude": 1.5003, "longitude": 2.5, "live_period": 900, "horizontal_accuracy": 8}

    await app.process_update(_raw_update(app, "message", 1, location=first))
    await app.process_update(_raw_update(
        app, "edited_message", 2, location=moved, edit_date=int(_T0.timestamp()) + 60,
    ))
    await app.process_update(_raw_update(app, "message", 3, message_id=8, text="where am I?"))

    assert ch.router.handle_message.await_count == 2
    assert _texts(ch)[1] == (
        "[Live location since 10:00:00 UTC, shared until 10:15:00 (2 positions, latest last):\n"
        "10:00:00 1.500000, 2.500000\n"
        "10:01:00 1.500300, 2.500000 (±8 m)]\n\n"
        "where am I?"
    )

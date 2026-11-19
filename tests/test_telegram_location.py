"""Tests for shared locations and venues on Telegram (nerve.channels.telegram).

Telegram sends a location as coordinates on the message, not as a file. The
generic message handler takes it, and the agent gets a ``[Location: …]``
line: on the message itself, in the reply context of a message that answers
one, and in the source inbox's record of an observed group message. A live
location's later positions arrive as edited messages, which start no turn;
tests/test_telegram_live_locations.py covers how the next message carries
them.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Location, Update, User, Venue

from nerve.channels.telegram import (
    TelegramChannel,
    _describe_telegram_attachment,
    _format_location,
    _format_reply_context,
)
from nerve.config import NerveConfig

_USER = 999
_CHAT = 42

_TOWER = Location(latitude=48.85837, longitude=2.294481, horizontal_accuracy=15)
_TOWER_TEXT = "48.858370, 2.294481 (±15 m)"


def _message(**fields) -> SimpleNamespace:
    """A message carrying only ``fields``: what a real one reports as None."""
    base = dict(location=None, venue=None, sticker=None, document=None, photo=None)
    base.update(fields)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
#  Formatting                                                                  #
# --------------------------------------------------------------------------- #

def test_a_location_gives_its_coordinates_and_accuracy():
    assert _format_location(_message(location=_TOWER)) == f"[Location: {_TOWER_TEXT}]"


def test_accuracy_is_left_out_when_telegram_sends_none():
    plain = Location(latitude=48.85837, longitude=2.294481)
    assert _format_location(_message(location=plain)) == "[Location: 48.858370, 2.294481]"


def test_zero_coordinates_are_still_a_location():
    # 0 is falsy; a truthiness check would drop a point on the equator.
    origin = Location(latitude=0, longitude=0)
    assert _format_location(_message(location=origin)) == "[Location: 0.000000, 0.000000]"


def test_a_live_location_says_how_long_and_that_later_positions_are_not_passed_on():
    live = Location(
        latitude=48.85837, longitude=2.294481, horizontal_accuracy=15,
        live_period=900, heading=90,
    )
    assert _format_location(_message(location=live)) == (
        f"[Live location: {_TOWER_TEXT}, shared for 15 min, heading 90°; "
        "later positions are not passed on]"
    )


@pytest.mark.parametrize(("seconds", "shared"), [
    (3600, "for 1 h"),
    (5400, "for 1 h 30 min"),
    (28800, "for 8 h"),
    (0x7FFFFFFF, "until stopped"),
])
def test_live_period_wording(seconds, shared):
    live = Location(latitude=1.5, longitude=2.5, live_period=seconds)
    assert f"shared {shared};" in _format_location(_message(location=live))


def test_live_period_as_a_timedelta():
    # PTB reports durations as timedelta when PTB_TIMEDELTA is set.
    live = SimpleNamespace(
        latitude=1.5, longitude=2.5, live_period=timedelta(minutes=15),
    )
    assert "shared for 15 min;" in _format_location(_message(location=live))


def test_a_venue_names_the_place():
    where = Location(latitude=48.85837, longitude=2.294481)
    venue = Venue(location=where, title="Tour Eiffel", address="Champ de Mars, Paris")
    # Telegram sets ``location`` on a venue message too; the venue wins.
    assert _format_location(_message(location=where, venue=venue)) == (
        '[Venue: "Tour Eiffel", Champ de Mars, Paris at 48.858370, 2.294481]'
    )


def test_no_location_is_nothing():
    assert _format_location(_message()) == ""
    # A MagicMock hands back a stand-in for every attribute; the other
    # Telegram tests build messages that way and must stay unaffected.
    assert _format_location(MagicMock()) == ""


def test_an_observed_location_is_described_for_the_source_inbox():
    assert _describe_telegram_attachment(_message(location=_TOWER)) == (
        f"[Location: {_TOWER_TEXT}]"
    )


def test_a_reply_to_a_location_quotes_its_coordinates():
    replied = _message(
        text=None, caption=None,
        from_user=SimpleNamespace(is_bot=False, first_name="Alice"),
        location=_TOWER,
    )
    message = SimpleNamespace(reply_to_message=replied, quote=None)
    assert _format_reply_context(message) == f"[Reply to Alice's location: {_TOWER_TEXT}]"


# --------------------------------------------------------------------------- #
#  Handler                                                                     #
# --------------------------------------------------------------------------- #

def _channel(db) -> TelegramChannel:
    """A channel whose live branch runs to ``router.handle_message``.

    File extraction is stubbed out; locations need no download, so the rest
    is the production path over a real database.
    """
    cfg = NerveConfig()
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
    return ch


def _update(*, text=None, location=None, reply_to=None):
    update = MagicMock()
    update.effective_chat.id = _CHAT
    update.effective_chat.type = "private"
    update.effective_user.id = _USER
    message = update.message
    message.text = text
    message.caption = None
    message.message_id = 900
    message.media_group_id = None
    message.forward_origin = None
    message.quote = None
    message.location = location
    message.venue = None
    message.reply_to_message = reply_to
    message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_a_location_reaches_the_session(db):
    ch = _channel(db)

    await ch._handle_message(_update(location=_TOWER), None)

    ch.router.handle_message.assert_awaited_once()
    assert ch.router.handle_message.await_args.args[0].text == f"[Location: {_TOWER_TEXT}]"


@pytest.mark.asyncio
async def test_a_question_about_a_replied_to_location_carries_it(db):
    ch = _channel(db)
    replied = _message(
        message_id=500, text=None, caption=None,
        from_user=SimpleNamespace(is_bot=False, first_name="Alice"),
        location=_TOWER,
    )

    await ch._handle_message(_update(text="what is near here?", reply_to=replied), None)

    ch.router.handle_message.assert_awaited_once()
    assert ch.router.handle_message.await_args.args[0].text == (
        f"[Reply to Alice's location: {_TOWER_TEXT}]\n\nwhat is near here?"
    )


@pytest.mark.asyncio
async def test_a_live_location_update_starts_no_turn(db):
    # A moving live location is an edit of the first message: update.message
    # is None and the position is on update.edited_message. It is kept for
    # the chat's next message, not delivered on its own.
    ch = _channel(db)
    update = MagicMock()
    update.effective_chat.id = _CHAT
    update.effective_user.id = _USER
    update.message = None
    update.edited_message.location = Location(latitude=1.5, longitude=2.5, live_period=900)

    await ch._handle_message(update, None)

    ch.router.handle_message.assert_not_awaited()
    ch.router.observe.assert_not_awaited()


# --------------------------------------------------------------------------- #
#  Dispatch: the production handler table                                      #
# --------------------------------------------------------------------------- #

def _application(ch: TelegramChannel):
    """The real PTB dispatch table, with the generic callback wrapped."""
    ch.config.telegram.bot_token = "123:ABC"
    generic = AsyncMock(wraps=ch._handle_message)
    ch._handle_message = generic
    app = ch._build_application()
    # process_update only needs the initialized flag for these blocking,
    # persistence-free handlers. Setting the bot user avoids a getMe request.
    app._initialized = True
    app.bot._bot_user = User(id=1000, first_name="Nerve", is_bot=True, username="nerve_bot")
    return app, generic


def _raw_update(app, kind: str, **fields):
    message = {
        "message_id": 7,
        "date": 1_700_000_000,
        "chat": {"id": _CHAT, "type": "private", "first_name": "Alice"},
        "from": {"id": _USER, "is_bot": False, "first_name": "Alice"},
        **fields,
    }
    if kind == "edited_message":
        message["edit_date"] = 1_700_000_060
    return Update.de_json({"update_id": 1, kind: message}, app.bot)


_RAW_TOWER = {"latitude": 48.85837, "longitude": 2.294481, "horizontal_accuracy": 15}


@pytest.mark.asyncio
async def test_the_handler_table_takes_a_location(db):
    ch = _channel(db)
    app, generic = _application(ch)

    await app.process_update(_raw_update(app, "message", location=_RAW_TOWER))

    generic.assert_awaited_once()
    assert ch.router.handle_message.await_args.args[0].text == f"[Location: {_TOWER_TEXT}]"


@pytest.mark.asyncio
async def test_the_handler_table_takes_a_venue(db):
    ch = _channel(db)
    app, generic = _application(ch)

    await app.process_update(_raw_update(
        app, "message",
        location=_RAW_TOWER,
        venue={"location": _RAW_TOWER, "title": "Tour Eiffel", "address": "Champ de Mars, Paris"},
    ))

    generic.assert_awaited_once()
    assert ch.router.handle_message.await_args.args[0].text == (
        f'[Venue: "Tour Eiffel", Champ de Mars, Paris at {_TOWER_TEXT}]'
    )


@pytest.mark.asyncio
async def test_a_live_location_edit_through_the_handler_table_starts_no_turn(db):
    ch = _channel(db)
    app, _generic = _application(ch)

    await app.process_update(_raw_update(
        app, "edited_message", location={**_RAW_TOWER, "live_period": 900},
    ))

    ch.router.handle_message.assert_not_awaited()

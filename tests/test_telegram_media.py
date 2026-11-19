"""Tests for voice, audio and video messages on Telegram (nerve.channels.telegram).

Telegram's voice notes, round videos, videos and audio messages, and audio or
video sent as a file, are saved under the media directory and transcribed by
the media worker; a video also gives a few frames as images. Claude reads no
audio or video, so this is how the session gets them at all. Any other kind
of message the channel cannot read gets a short reply instead of silence.

Updates are parsed by PTB from raw Bot API JSON and dispatched through the
production handler table. The download and the worker are faked: the worker's
own tests are in test_channel_media.py.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Audio, Document, Message, Update, User, Video, VideoNote, Voice

from nerve.channels import media as channel_media
from nerve.channels.media import MediaAnalysis
from nerve.channels.telegram import (
    TelegramChannel,
    _carries_media,
    _describe_telegram_attachment,
    _telegram_media,
)
from nerve.config import NerveConfig

_USER = 999
_STRANGER = 555
_CHAT = 42

_VOICE = {"file_id": "v1", "file_unique_id": "uv1", "duration": 42,
          "mime_type": "audio/ogg", "file_size": 12_345}
_VIDEO = {"file_id": "m1", "file_unique_id": "um1", "width": 640, "height": 360,
          "duration": 20, "mime_type": "video/mp4", "file_size": 400_000}
_ROUND = {"file_id": "r1", "file_unique_id": "ur1", "length": 384, "duration": 9,
          "file_size": 90_000}
_AUDIO = {"file_id": "a1", "file_unique_id": "ua1", "duration": 185, "performer": "Band",
          "title": "Song", "file_name": "song.mp3", "mime_type": "audio/mpeg",
          "file_size": 3_000_000}


class _FakeFile:
    def __init__(self, file_path: str, data: bytes = b"media"):
        self.file_path = file_path
        self._data = data

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(self._data)
        return Path(custom_path)


class _Worker:
    """Stands in for channel_media.analyze; records what it was asked."""

    def __init__(self):
        self.calls: list[tuple[Path, bool, int]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, path, media, *, transcribe, frames):
        self.calls.append((Path(path), transcribe, frames))
        if self.gate is not None:
            await self.gate.wait()
        assert Path(path).read_bytes() == b"media"
        if frames:
            return MediaAnalysis(
                duration=20.0, has_audio=True, has_video=True, width=640, height=360,
                language="en", text="the sound",
                frames=[(t, f"jpeg{t}") for t in range(frames)],
            )
        return MediaAnalysis(duration=42.0, has_audio=True, language="en", text="hello there")


@pytest.fixture
def worker(monkeypatch):
    fake = _Worker()
    monkeypatch.setattr(channel_media, "analyze", fake)
    paths = {
        Voice: "voice/file_1.oga", Audio: "music/file_2.mp3", Video: "videos/file_3.mp4",
        VideoNote: "video_notes/file_4.mp4", Document: "documents/file_5",
    }
    for cls, file_path in paths.items():
        async def get_file(self, *args, _file_path=file_path, **kwargs):
            return _FakeFile(_file_path)
        monkeypatch.setattr(cls, "get_file", get_file)
    return fake


@pytest.fixture
def replies(monkeypatch):
    sent: list[str] = []

    async def reply_text(self, text, *args, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "reply_text", reply_text)
    return sent


def _channel(db, workspace: Path) -> TelegramChannel:
    cfg = NerveConfig(workspace=workspace)
    cfg.telegram.allowed_users = [_USER]
    cfg.telegram.reply_routes_to_origin_session = False
    cfg.telegram.bot_token = "123:ABC"
    router = MagicMock()
    router.engine.db = db
    router.observe = AsyncMock(return_value=True)
    router.handle_message = AsyncMock()
    router.get_session = AsyncMock(return_value=None)
    router.get_active_session = AsyncMock(return_value="active1")
    return TelegramChannel(lambda: cfg, router)


def _application(ch: TelegramChannel):
    app = ch._build_application()
    app._initialized = True
    app.bot._bot_user = User(id=1000, first_name="Nerve", is_bot=True, username="nerve_bot")
    return app


def _raw(app, *, message_id=7, sender=_USER, **fields) -> Update:
    message = {
        "message_id": message_id,
        "date": 1_700_000_000,
        "chat": {"id": _CHAT, "type": "private", "first_name": "Alice"},
        "from": {"id": sender, "is_bot": False, "first_name": "Alice"},
        **fields,
    }
    return Update.de_json({"update_id": message_id, "message": message}, app.bot)


def _delivered(ch) -> list:
    return [c.args[0] for c in ch.router.handle_message.await_args_list]


# --------------------------------------------------------------------------- #
#  What counts as media                                                        #
# --------------------------------------------------------------------------- #

def test_a_magicmock_message_carries_no_media():
    # The other Telegram tests build messages as MagicMocks, which answer
    # every attribute; none of them may start a download.
    assert _telegram_media(MagicMock()) is None
    assert _carries_media(MagicMock()) is False


def test_the_inbox_names_media_without_fetching_it(db, tmp_path):
    app = _application(_channel(db, tmp_path))
    assert _describe_telegram_attachment(_raw(app, voice=_VOICE).message) == "[Voice message: 0:42]"
    assert _describe_telegram_attachment(_raw(app, video=_VIDEO).message) == "[Video: 0:20]"
    assert _describe_telegram_attachment(_raw(app, video_note=_ROUND).message) == "[Video message: 0:09]"
    assert _describe_telegram_attachment(_raw(app, audio=_AUDIO).message) == "[Audio: 3:05]"


def test_audio_or_video_sent_as_a_file_carries_media(db, tmp_path):
    app = _application(_channel(db, tmp_path))
    memo = {"file_id": "d1", "file_unique_id": "ud1", "file_name": "memo.m4a",
            "mime_type": "audio/mp4", "file_size": 1000}
    pdf = {"file_id": "d2", "file_unique_id": "ud2", "file_name": "a.pdf",
           "mime_type": "application/pdf", "file_size": 1000}
    assert _carries_media(_raw(app, document=memo).message)
    assert not _carries_media(_raw(app, document=pdf).message)
    reply = {"message_id": 3, "date": 1_700_000_000,
             "chat": {"id": _CHAT, "type": "private"}, "voice": _VOICE}
    assert _carries_media(_raw(app, text="what was that?", reply_to_message=reply).message)


# --------------------------------------------------------------------------- #
#  Through the handler table                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_voice_note_reaches_the_session_as_a_transcript(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, voice=_VOICE))

    [msg] = _delivered(ch)
    [(path, transcribe, frames)] = worker.calls
    assert path.parent.parent == tmp_path / ".media" / "telegram"
    assert path.name == f"{_CHAT}_7_voice.oga"
    assert (transcribe, frames) == (True, 0)
    assert msg.text == (
        f"[Voice message: 0:42, saved to {path}]\n"
        "Transcript (automatic, en): hello there"
    )
    assert "images" not in msg.metadata


@pytest.mark.asyncio
async def test_a_video_brings_frames_and_the_sound(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, video=_VIDEO, caption="look at this"))

    [msg] = _delivered(ch)
    [(path, transcribe, frames)] = worker.calls
    assert path.name == f"{_CHAT}_7_video.mp4"
    assert (transcribe, frames) == (True, 4)
    assert msg.text == (
        f"[Video: 0:20, 640x360, 4 frames attached, saved to {path}]\n"
        "Transcript of the sound (automatic, en): the sound\n\n"
        "look at this"
    )
    assert [b["data"] for b in msg.metadata["images"]] == ["jpeg0", "jpeg1", "jpeg2", "jpeg3"]
    assert {b["media_type"] for b in msg.metadata["images"]} == {"image/jpeg"}


@pytest.mark.asyncio
async def test_a_video_gives_at_most_sixteen_frames(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    ch.config.media.video_frames = 99
    app = _application(ch)

    await app.process_update(_raw(app, video=_VIDEO))

    assert worker.calls[0][2] == channel_media.MAX_VIDEO_FRAMES == 16


@pytest.mark.asyncio
async def test_a_round_video_message(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, video_note=_ROUND))

    [msg] = _delivered(ch)
    assert msg.text.startswith("[Video message: 0:09, 640x360, 4 frames attached, saved to ")
    assert worker.calls[0][0].name == f"{_CHAT}_7_video_note.mp4"


@pytest.mark.asyncio
async def test_an_audio_message_names_its_track(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, audio=_AUDIO))

    [msg] = _delivered(ch)
    path = worker.calls[0][0]
    assert path.name == f"{_CHAT}_7_audio.mp3"
    assert msg.text == (
        f'[Audio: "Song" by Band, 3:05, saved to {path}]\n'
        "Transcript (automatic, en): hello there"
    )


@pytest.mark.asyncio
async def test_audio_sent_as_a_file_is_transcribed_too(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)
    memo = {"file_id": "d1", "file_unique_id": "ud1", "file_name": "memo.m4a",
            "mime_type": "audio/mp4", "file_size": 2_500_000}

    await app.process_update(_raw(app, document=memo))

    [msg] = _delivered(ch)
    path = worker.calls[0][0]
    assert path.name == f"{_CHAT}_7_audio.m4a"
    assert msg.text == (
        f"[Document: memo.m4a (2.5 MB, audio/mp4), 0:42, saved to {path}]\n"
        "Transcript (automatic, en): hello there"
    )


@pytest.mark.asyncio
async def test_a_file_over_the_bot_api_limit_is_described_not_fetched(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, video={**_VIDEO, "file_size": 45_100_000, "duration": 200}))

    [msg] = _delivered(ch)
    assert worker.calls == []
    assert msg.text == (
        "[Video: 45.1 MB, 3:20]\nNot downloaded: a bot can fetch files of up to 20 MB."
    )


@pytest.mark.asyncio
async def test_a_failed_download_is_said(db, tmp_path, worker, monkeypatch):
    async def broken(self, *args, **kwargs):
        raise RuntimeError("Bad Request: file is too big")

    monkeypatch.setattr(Voice, "get_file", broken)
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, voice=_VOICE))

    [msg] = _delivered(ch)
    assert worker.calls == []
    assert msg.text == "[Voice message: 0:42]\nNot downloaded: Bad Request: file is too big"


@pytest.mark.asyncio
async def test_transcription_off_still_saves_the_file(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    ch.config.media.transcription.enabled = False
    app = _application(ch)

    await app.process_update(_raw(app, voice=_VOICE))

    [(path, transcribe, frames)] = worker.calls
    assert (transcribe, frames) == (False, 0)
    assert path.exists()
    assert _delivered(ch)[0].text.endswith(
        "Transcript: transcription is off (media.transcription.enabled)"
    )


@pytest.mark.asyncio
async def test_a_question_about_a_replied_to_voice_note_carries_its_transcript(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)
    replied = {"message_id": 3, "date": 1_700_000_000,
               "chat": {"id": _CHAT, "type": "private"},
               "from": {"id": 7, "is_bot": False, "first_name": "Bob"},
               "voice": _VOICE}

    await app.process_update(_raw(app, text="what did he say?", reply_to_message=replied))

    [msg] = _delivered(ch)
    path = worker.calls[0][0]
    assert path.name == f"{_CHAT}_3_voice.oga"
    assert msg.text == (
        f"[Replied-to voice message: 0:42, saved to {path}]\n"
        "Transcript (automatic, en): hello there\n\n"
        "[Reply to Bob's message]\n\n"
        "what did he say?"
    )


@pytest.mark.asyncio
async def test_old_files_are_pruned_when_a_new_one_is_saved(db, tmp_path, worker):
    old = tmp_path / ".media" / "telegram" / "2000-01-01"
    old.mkdir(parents=True)
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, voice=_VOICE))
    assert not old.exists()

    old.mkdir(parents=True)
    ch.config.media.keep_days = 0
    await app.process_update(_raw(app, voice=_VOICE, message_id=8))
    assert old.exists()


@pytest.mark.asyncio
async def test_a_stranger_s_voice_note_is_not_downloaded(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, voice=_VOICE, sender=_STRANGER))

    assert worker.calls == []
    ch.router.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_typing_is_shown_while_a_file_is_processed(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)
    ch._app = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    worker.gate = asyncio.Event()

    task = asyncio.create_task(ch._handle_message(_raw(app, voice=_VOICE), None))
    for _ in range(20):
        await asyncio.sleep(0)
    ch._app.bot.send_chat_action.assert_awaited()
    assert ch._app.bot.send_chat_action.await_args.kwargs["chat_id"] == _CHAT
    worker.gate.set()
    await task
    assert len(_delivered(ch)) == 1


# --------------------------------------------------------------------------- #
#  Order                                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_text_sent_after_a_voice_note_waits_for_it(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)
    worker.gate = asyncio.Event()

    voice = asyncio.create_task(ch._handle_message(_raw(app, voice=_VOICE, message_id=7), None))
    text = asyncio.create_task(ch._handle_message(_raw(app, text="and also this", message_id=8), None))
    for _ in range(20):
        await asyncio.sleep(0)
    # The text is ready, but the voice note is still being transcribed.
    ch.router.handle_message.assert_not_awaited()

    worker.gate.set()
    await asyncio.gather(voice, text)
    assert [m.metadata["message_id"] for m in _delivered(ch)] == [7, 8]


@pytest.mark.asyncio
async def test_texts_alone_never_wait(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await ch._handle_message(_raw(app, text="one", message_id=7), None)
    await ch._handle_message(_raw(app, text="two", message_id=8), None)

    assert [m.text for m in _delivered(ch)] == ["one", "two"]
    assert ch._arrivals._tails == {}


@pytest.mark.asyncio
async def test_a_dropped_voice_note_does_not_hold_up_the_chat(db, tmp_path, worker, monkeypatch):
    # A voice note that turns out to be feedback on a plan is not delivered
    # to the session; whoever waits behind it must be released.
    ch = _channel(db, tmp_path)
    app = _application(ch)
    monkeypatch.setattr(ch, "_maybe_consume_plan_revision", AsyncMock(return_value=True))

    await ch._handle_message(_raw(app, voice=_VOICE, message_id=7), None)

    assert ch._arrivals._tails == {}
    ch.router.handle_message.assert_not_awaited()
    monkeypatch.setattr(ch, "_maybe_consume_plan_revision", AsyncMock(return_value=False))
    await asyncio.wait_for(ch._handle_message(_raw(app, text="next", message_id=8), None), 2)
    assert [m.text for m in _delivered(ch)] == ["next"]


# --------------------------------------------------------------------------- #
#  Albums                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_an_album_of_videos(db, tmp_path, worker):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await ch._handle_message(_raw(app, video=_VIDEO, media_group_id="g1", message_id=7,
                                  caption="two takes"), None)
    await ch._handle_message(_raw(app, video=_VIDEO, media_group_id="g1", message_id=8), None)
    await ch._media_group_tasks["g1"]

    [msg] = _delivered(ch)
    assert [c[0].name for c in worker.calls] == [f"{_CHAT}_7_video.mp4", f"{_CHAT}_8_video.mp4"]
    assert msg.text.count("[Video: 0:20, 640x360, 4 frames attached") == 2
    assert msg.text.endswith("two takes")
    assert len(msg.metadata["images"]) == 8


# --------------------------------------------------------------------------- #
#  Everything else                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_contact_gets_a_reply_instead_of_silence(db, tmp_path, replies):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, contact={"phone_number": "+100", "first_name": "Bob"}))

    assert replies == ["I can't read a contact yet, so it didn't reach the session."]
    ch.router.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dice_roll_gets_a_reply(db, tmp_path, replies):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, dice={"emoji": "🎲", "value": 3}))

    assert replies == ["I can't read a dice roll yet, so it didn't reach the session."]


@pytest.mark.asyncio
async def test_service_messages_and_strangers_get_no_reply(db, tmp_path, replies):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, new_chat_title="renamed"))
    await app.process_update(_raw(app, sender=_STRANGER,
                                  contact={"phone_number": "+100", "first_name": "Bob"}))

    assert replies == []
    ch.router.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_text_still_goes_to_the_session_not_to_the_fallback(db, tmp_path, replies):
    ch = _channel(db, tmp_path)
    app = _application(ch)

    await app.process_update(_raw(app, text="hello"))

    assert replies == []
    assert [m.text for m in _delivered(ch)] == ["hello"]

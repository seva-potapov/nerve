"""Tests for received voice, audio and video (nerve.channels.media).

The channel saves a file under the media directory and hands the worker
(``nerve/channels/media_worker.py``) one file per child process. These tests
cover the parts that run in the daemon: where files go and when they are
pruned, the worker's command line and how its output, failures and timeouts
come back, and the text the agent gets.

The worker itself needs faster-whisper and PyAV, which the test environment
does not have. Set ``NERVE_TEST_MEDIA_PYTHON`` to an interpreter that has
them (and ``NERVE_TEST_MEDIA_MODEL`` to a model name or directory) to run it
for real at the end of this file.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import wave
from datetime import date
from pathlib import Path

import pytest

from nerve.channels import media as channel_media
from nerve.channels.media import (
    MediaAnalysis,
    analyze,
    describe,
    format_duration,
    media_kind,
    media_path,
    parse_report,
    prune,
    worker_command,
)
from nerve.config import MediaConfig, NerveConfig


def _media(**transcription) -> MediaConfig:
    return MediaConfig.from_dict({"transcription": transcription})


# --------------------------------------------------------------------------- #
#  Config                                                                      #
# --------------------------------------------------------------------------- #

def test_defaults():
    cfg = NerveConfig().media
    assert cfg.save_dir is None
    assert cfg.keep_days == 30
    assert cfg.video_frames == 4
    t = cfg.transcription
    assert (t.enabled, t.python, t.model, t.compute_type, t.threads, t.language) == (
        True, None, "large-v3-turbo", "int8", 4, "",
    )
    assert t.timeout_seconds == 600


def test_config_values_are_coerced_and_clamped():
    cfg = NerveConfig.from_dict({"media": {
        "save_dir": "~/m",
        "keep_days": -1,
        "video_frames": 99,
        "transcription": {
            "enabled": "off", "python": "~/v/bin/python", "threads": "2",
            "timeout_seconds": 1, "language": "ru",
        },
    }}).media
    assert cfg.save_dir == Path("~/m").expanduser()
    assert cfg.keep_days == 0
    # Read as asked; the channel takes at most MAX_VIDEO_FRAMES.
    assert cfg.video_frames == 99
    assert cfg.transcription.enabled is False
    assert cfg.transcription.python == Path("~/v/bin/python").expanduser()
    assert cfg.transcription.threads == 2
    assert cfg.transcription.timeout_seconds == 10
    assert cfg.transcription.language == "ru"


def test_media_root_defaults_to_the_workspace(tmp_path):
    cfg = NerveConfig(workspace=tmp_path)
    assert channel_media.media_root(cfg) == tmp_path / ".media"
    cfg.media.save_dir = tmp_path / "elsewhere"
    assert channel_media.media_root(cfg) == tmp_path / "elsewhere"


# --------------------------------------------------------------------------- #
#  Files                                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("mime", "name", "kind"), [
    ("audio/mpeg", "song.mp3", "audio"),
    ("audio/ogg", "x", "audio"),
    ("application/ogg", "note.ogg", "audio"),
    ("", "memo.M4A", "audio"),
    ("video/mp4", "clip.mp4", "video"),
    ("video/quicktime", "x", "video"),
    ("application/octet-stream", "screen.mov", "video"),
    # A video container named like audio is still a video.
    ("video/webm", "a.weba", "video"),
    ("application/pdf", "doc.pdf", ""),
    ("text/plain", "notes.txt", ""),
    ("", "", ""),
])
def test_media_kind(mime, name, kind):
    assert media_kind(mime, name) == kind


def test_media_path_layout(tmp_path):
    path = media_path(tmp_path, "telegram", -100123, 77, "voice", ".OGA", today=date(2026, 1, 2))
    assert path == tmp_path / "telegram" / "2026-01-02" / "-100123_77_voice.oga"


def test_media_path_parts_cannot_leave_the_directory(tmp_path):
    path = media_path(tmp_path, "../x", "../../etc", "a/b", "voice", ".sh;rm", today=date(2026, 1, 2))
    assert path.parent.parent.parent == tmp_path
    assert "/" not in path.name and ".." not in path.parent.name
    # A suffix that is not a short alphanumeric extension is dropped.
    assert path.suffix == ""


def test_prune_removes_only_old_day_directories(tmp_path):
    base = tmp_path / "telegram"
    for name in ("2026-01-01", "2026-01-20", "2026-01-21", "2026-01-31", "notes"):
        (base / name).mkdir(parents=True)
        (base / name / "f").write_text("x")
    # Ten days back from the 31st is the 21st: that day is kept, older ones go.
    removed = prune(tmp_path, "telegram", 10, today=date(2026, 1, 31))
    assert removed == 2
    assert sorted(p.name for p in base.iterdir()) == ["2026-01-21", "2026-01-31", "notes"]


def test_prune_zero_keeps_everything(tmp_path):
    (tmp_path / "telegram" / "2000-01-01").mkdir(parents=True)
    assert prune(tmp_path, "telegram", 0, today=date(2026, 1, 31)) == 0
    assert (tmp_path / "telegram" / "2000-01-01").exists()


def test_prune_without_a_directory_is_a_no_op(tmp_path):
    assert prune(tmp_path / "missing", "telegram", 5) == 0


# --------------------------------------------------------------------------- #
#  Worker command and report                                                   #
# --------------------------------------------------------------------------- #

def test_worker_command_for_a_voice_note(tmp_path):
    media = _media(python="/v/bin/python", model="small", threads=2, language="de")
    cmd = worker_command(tmp_path / "a.oga", media, transcribe=True, frames=0)
    assert cmd[:3] == ["/v/bin/python", "-I", str(channel_media.WORKER)]
    joined = " ".join(cmd)
    assert "--transcribe" in cmd and "--model small" in joined and "--threads 2" in joined
    assert "--language de" in joined and "--frames 0" in joined
    assert "--model-dir" not in cmd


def test_worker_command_defaults_to_nerves_interpreter(tmp_path):
    cmd = worker_command(tmp_path / "v.mp4", _media(), transcribe=False, frames=4)
    assert cmd[0] == sys.executable
    assert "--transcribe" not in cmd and "--frames" in cmd and cmd[cmd.index("--frames") + 1] == "4"


def test_parse_report_reads_a_video():
    report = parse_report({
        "duration": 20.5, "has_audio": True, "has_video": True, "width": 640, "height": 360,
        "language": "en", "text": "hello",
        "frames": [{"t": 2.5, "jpeg": "QUJD"}, {"t": "bad", "jpeg": "REVG"}, {"no": "jpeg"}],
    }, transcribe=True, frames=4)
    assert (report.duration, report.width, report.height, report.text) == (20.5, 640, 360, "hello")
    assert report.frames == [(2.5, "QUJD"), (0.0, "REVG")]


def test_parse_report_without_a_transcript_says_so():
    report = parse_report({"has_audio": True}, transcribe=True, frames=0)
    assert report.text is None
    assert report.transcript_error == "the worker returned no transcript"


def test_parse_report_ignores_what_was_not_asked():
    report = parse_report({"text": "x", "transcript_error": "e", "frames_error": "f"}, transcribe=False, frames=0)
    assert (report.text, report.transcript_error, report.frames_error) == (None, "", "")


# --------------------------------------------------------------------------- #
#  analyze(): the child process                                                #
# --------------------------------------------------------------------------- #

def _fake_worker(tmp_path, monkeypatch, body: str) -> None:
    """Replace the worker script with ``body`` (run by this interpreter)."""
    script = tmp_path / "fake_worker.py"
    script.write_text(textwrap.dedent(body))
    monkeypatch.setattr(channel_media, "WORKER", script)


@pytest.mark.asyncio
async def test_analyze_returns_the_workers_report(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, """
        import json, sys
        args = sys.argv[1:]
        print(json.dumps({"has_audio": True, "duration": 3, "language": "en",
                          "text": "hi " + args[args.index("--input") + 1].rsplit("/", 1)[-1]}))
    """)
    report = await analyze(tmp_path / "a.oga", _media(), transcribe=True, frames=0)
    assert (report.text, report.duration, report.transcript_error) == ("hi a.oga", 3.0, "")


@pytest.mark.asyncio
async def test_analyze_does_nothing_when_nothing_is_asked(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, "raise SystemExit('must not run')")
    assert await analyze(tmp_path / "a", _media(), transcribe=False, frames=0) == MediaAnalysis()


@pytest.mark.asyncio
async def test_a_missing_interpreter_is_reported(tmp_path):
    media = _media(python=str(tmp_path / "no-such-python"))
    report = await analyze(tmp_path / "a.oga", media, transcribe=True, frames=2)
    assert report.transcript_error.startswith(f"cannot run {tmp_path / 'no-such-python'}")
    assert report.frames_error == report.transcript_error


@pytest.mark.asyncio
async def test_a_crashing_worker_is_reported_with_its_last_error_line(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, """
        import sys
        sys.stderr.write("warming up\\nRuntimeError: out of memory\\n")
        sys.exit(3)
    """)
    report = await analyze(tmp_path / "a.oga", _media(), transcribe=True, frames=0)
    assert report.transcript_error == "the worker failed: RuntimeError: out of memory"


@pytest.mark.asyncio
async def test_a_hung_worker_is_killed_at_the_timeout(tmp_path, monkeypatch):
    marker = tmp_path / "still-running"
    _fake_worker(tmp_path, monkeypatch, f"""
        import time, pathlib
        time.sleep(30)
        pathlib.Path({str(marker)!r}).write_text("x")
    """)
    media = _media()
    media.transcription.timeout_seconds = 1   # below the config clamp, for the test
    report = await analyze(tmp_path / "a.oga", media, transcribe=True, frames=0)
    assert report.transcript_error == "timed out after 1 s (media.transcription.timeout_seconds)"
    await asyncio.sleep(0.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_workers_run_one_at_a_time(tmp_path, monkeypatch):
    log = tmp_path / "log"
    _fake_worker(tmp_path, monkeypatch, f"""
        import json, time
        with open({str(log)!r}, "a") as f: f.write("start\\n")
        time.sleep(0.3)
        with open({str(log)!r}, "a") as f: f.write("end\\n")
        print(json.dumps({{"has_audio": True, "text": ""}}))
    """)
    await asyncio.gather(*(
        analyze(tmp_path / f"{i}.oga", _media(), transcribe=True, frames=0) for i in range(3)
    ))
    assert log.read_text().split() == ["start", "end"] * 3


# --------------------------------------------------------------------------- #
#  The text the agent gets                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("seconds", "text"), [
    (None, ""), (0, "0:00"), (42.4, "0:42"), (605, "10:05"), (3723, "1:02:03"),
])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_a_transcribed_voice_note(tmp_path):
    analysis = MediaAnalysis(duration=41.9, has_audio=True, language="en", text="Call me back.")
    assert describe(
        "Voice message", [], duration=42, analysis=analysis, saved=tmp_path / "v.oga",
    ) == (
        f"[Voice message: 0:42, saved to {tmp_path / 'v.oga'}]\n"
        "Transcript (automatic, en): Call me back."
    )


def test_a_video_with_frames_and_sound(tmp_path):
    analysis = MediaAnalysis(
        duration=65, has_audio=True, has_video=True, width=1280, height=720,
        language="ru", text="Привет", frames=[(1.0, "a"), (2.0, "b")],
    )
    assert describe("Video", [], analysis=analysis, saved=tmp_path / "v.mp4") == (
        f"[Video: 1:05, 1280x720, 2 frames attached, saved to {tmp_path / 'v.mp4'}]\n"
        "Transcript of the sound (automatic, ru): Привет"
    )
    assert channel_media.frame_blocks(analysis) == [
        {"type": "base64", "media_type": "image/jpeg", "data": "a"},
        {"type": "base64", "media_type": "image/jpeg", "data": "b"},
    ]


def test_a_silent_video_has_no_transcript_line():
    analysis = MediaAnalysis(duration=8, has_video=True, transcript_error="no sound track",
                             frames=[(1.0, "a")])
    assert describe("Video", [], analysis=analysis) == "[Video: 0:08, 1 frame attached, no sound]"


def test_no_speech():
    analysis = MediaAnalysis(duration=5, has_audio=True, text="")
    assert describe("Voice message", [], analysis=analysis) == (
        "[Voice message: 0:05]\nTranscript: no speech detected"
    )


def test_a_failed_transcription_is_explained():
    analysis = MediaAnalysis(transcript_error="faster-whisper is not installed for /usr/bin/python3")
    assert describe("Voice message", [], duration=3, analysis=analysis) == (
        "[Voice message: 0:03]\n"
        "Transcript: not available (faster-whisper is not installed for /usr/bin/python3)"
    )


def test_transcription_off():
    analysis = MediaAnalysis(duration=3, has_audio=True)
    assert describe("Voice message", [], analysis=analysis, transcription_enabled=False) == (
        "[Voice message: 0:03]\nTranscript: transcription is off (media.transcription.enabled)"
    )


def test_an_audio_file_without_a_sound_track():
    analysis = MediaAnalysis(duration=3, transcript_error="no sound track")
    assert describe("Audio", ['"Song" by Band'], analysis=analysis) == (
        '[Audio: "Song" by Band, 0:03]\nTranscript: none, the file has no sound track'
    )


def test_a_note_replaces_the_transcript():
    assert describe("Video", ["45.1 MB"], duration=200, note="Not downloaded: too big.") == (
        "[Video: 45.1 MB, 3:20]\nNot downloaded: too big."
    )


def test_frames_that_failed_are_said():
    analysis = MediaAnalysis(has_video=True, has_audio=True, text="x", frames_error="decoder broke")
    assert "no frames (decoder broke)" in describe("Video", [], analysis=analysis)


# --------------------------------------------------------------------------- #
#  The real worker (opt-in)                                                    #
# --------------------------------------------------------------------------- #

_MEDIA_PYTHON = os.environ.get("NERVE_TEST_MEDIA_PYTHON", "")
_needs_media_python = pytest.mark.skipif(
    not _MEDIA_PYTHON, reason="set NERVE_TEST_MEDIA_PYTHON to a python with faster-whisper",
)


@_needs_media_python
@pytest.mark.asyncio
async def test_the_worker_finds_no_speech_in_a_tone(tmp_path):
    tone = tmp_path / "tone.wav"
    with wave.open(str(tone), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        import math
        w.writeframes(b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True)
            for i in range(16000 * 3)
        ))
    media = _media(
        python=_MEDIA_PYTHON,
        model=os.environ.get("NERVE_TEST_MEDIA_MODEL", "tiny"),
    )
    report = await analyze(tone, media, transcribe=True, frames=0)
    assert report.transcript_error == ""
    assert report.has_audio and not report.has_video
    assert report.duration == pytest.approx(3.0, abs=0.1)
    assert report.text == ""


@_needs_media_python
def test_the_worker_turns_a_portrait_frame_upright():
    # PyAV gives the display rotation counter-clockwise; a phone's portrait
    # video says -90. A marker in the stored frame's top-left corner must end
    # up top-right once the frame is turned 90 degrees clockwise.
    snippet = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(channel_media.WORKER.parent)!r})
        import av, numpy as np
        import media_worker as w
        img = np.zeros((40, 80, 3), dtype=np.uint8)
        img[:10, :10] = 255
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")

        class Rotated:
            rotation = -90
            def to_ndarray(self, format):
                return frame.to_ndarray(format=format)

        data, width, height = w._jpeg(Rotated(), 1280)
        with av.open(__import__("io").BytesIO(data)) as c:
            out = next(c.decode(video=0)).to_ndarray(format="rgb24")
        bright = out[:, :, 0] > 200
        ys, xs = np.nonzero(bright)
        print(json.dumps([width, height, int(ys.mean()), int(xs.mean())]))
    """)
    done = subprocess.run(
        [_MEDIA_PYTHON, "-I", "-c", snippet], capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    width, height, y, x = json.loads(done.stdout)
    assert (width, height) == (40, 80)
    assert y < 15 and x > 25

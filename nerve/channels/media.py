"""Voice, audio and video a channel received: save, transcribe, sample frames.

A channel downloads the file to :func:`media_path`, calls :func:`analyze`,
and hands the agent :func:`describe`'s text plus :func:`frame_blocks` as
images. Claude reads no audio or video, so a voice note reaches it as a
transcript and a video as a transcript of its sound plus a few frames.

The work happens in a child process (:mod:`nerve.channels.media_worker`),
run with ``media.transcription.python``: faster-whisper and PyAV need not be
importable by Nerve, the model is in memory only while a file is being
transcribed, and a crash or hang there cannot take the daemon with it. One
file is processed at a time; the next waits its turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import weakref
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nerve.config import MediaConfig, NerveConfig

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("media_worker.py")

# Most frames taken from one video, whatever media.video_frames asks for:
# each is an image in the turn.
MAX_VIDEO_FRAMES = 16

# Longest edge of a frame passed on as an image. Readable text in a screen
# recording needs about this much; Claude scales larger images down anyway.
FRAME_MAX_EDGE = 1280

# File types sent as a plain file that get the same treatment as a voice
# note or a video. Telegram's own voice, audio and video messages are
# recognised by their kind, not by these.
AUDIO_EXTENSIONS = frozenset({
    ".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba", ".wma",
})
VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv",
})

_TRANSCRIPTION_OFF = "transcription is off (media.transcription.enabled)"


def media_kind(mime: str, file_name: str) -> str:
    """``"audio"``, ``"video"`` or ``""`` for a file sent as a document."""
    mime = (mime or "").lower()
    ext = Path(file_name or "").suffix.lower()
    if mime.startswith("audio/") or (not mime.startswith("video/") and ext in AUDIO_EXTENSIONS):
        return "audio"
    if mime.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    return ""


# --------------------------------------------------------------------------- #
#  Where files go                                                              #
# --------------------------------------------------------------------------- #

def media_root(config: NerveConfig) -> Path:
    """The directory received media is saved under."""
    return config.media.save_dir or (Path(config.workspace) / ".media")


def _safe_part(value: Any) -> str:
    text = str(value)
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:64] or "x"


def media_path(
    root: Path, channel: str, conversation: Any, message: Any, kind: str, suffix: str,
    *, today: date | None = None,
) -> Path:
    """``<root>/<channel>/<YYYY-MM-DD>/<conversation>_<message>_<kind><suffix>``.

    Every part is reduced to a safe file name; ``suffix`` keeps only a short
    alphanumeric extension.
    """
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    ext = suffix.lower() if suffix.startswith(".") and suffix[1:].isalnum() and len(suffix) <= 6 else ""
    name = f"{_safe_part(conversation)}_{_safe_part(message)}_{_safe_part(kind)}{ext}"
    return root / _safe_part(channel) / day / name


def prune(root: Path, channel: str, keep_days: int, *, today: date | None = None) -> int:
    """Delete the day directories of ``channel`` older than ``keep_days``.

    Returns how many were removed. ``keep_days`` 0 keeps everything. Only
    directories named as a date are touched.
    """
    if keep_days <= 0:
        return 0
    base = root / _safe_part(channel)
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=keep_days)
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            day = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if day < cutoff and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


# --------------------------------------------------------------------------- #
#  The worker                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class MediaAnalysis:
    """What the worker found in one file."""

    duration: float | None = None
    has_audio: bool = False
    has_video: bool = False
    width: int | None = None
    height: int | None = None
    language: str = ""
    text: str | None = None              # None: not transcribed
    transcript_error: str = ""
    frames: list[tuple[float, str]] = field(default_factory=list)  # (seconds, base64 JPEG)
    frames_error: str = ""

    @classmethod
    def failed(cls, reason: str, *, transcribe: bool, frames: int) -> MediaAnalysis:
        return cls(
            transcript_error=reason if transcribe else "",
            frames_error=reason if frames > 0 else "",
        )


_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _turn() -> asyncio.Semaphore:
    """One worker at a time per event loop: each holds a model in memory."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Semaphore(1)
    return lock


def worker_command(path: Path, media: MediaConfig, *, transcribe: bool, frames: int) -> list[str]:
    """The child-process command line for one file."""
    t = media.transcription
    cmd = [
        str(t.python or sys.executable), "-I", str(WORKER),
        "--input", str(path),
        "--frames", str(max(0, frames)),
        "--max-edge", str(FRAME_MAX_EDGE),
    ]
    if transcribe:
        cmd += [
            "--transcribe",
            "--model", t.model,
            "--compute-type", t.compute_type,
            "--threads", str(t.threads),
        ]
        if t.model_dir:
            cmd += ["--model-dir", str(t.model_dir)]
        if t.language:
            cmd += ["--language", t.language]
    return cmd


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    return env


def _last_line(stderr: bytes) -> str:
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def parse_report(raw: dict, *, transcribe: bool, frames: int) -> MediaAnalysis:
    """Turn the worker's JSON into a :class:`MediaAnalysis`."""
    def _num(key: str) -> float | None:
        value = raw.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    width, height = _num("width"), _num("height")
    out = MediaAnalysis(
        duration=_num("duration"),
        has_audio=bool(raw.get("has_audio")),
        has_video=bool(raw.get("has_video")),
        width=int(width) if width else None,
        height=int(height) if height else None,
        language=str(raw.get("language") or ""),
        text=raw["text"] if transcribe and isinstance(raw.get("text"), str) else None,
        transcript_error=str(raw.get("transcript_error") or "") if transcribe else "",
        frames_error=str(raw.get("frames_error") or "") if frames > 0 else "",
    )
    for item in raw.get("frames") or []:
        if isinstance(item, dict) and isinstance(item.get("jpeg"), str):
            t = item.get("t")
            out.frames.append((float(t) if isinstance(t, (int, float)) else 0.0, item["jpeg"]))
    if transcribe and out.text is None and not out.transcript_error:
        out.transcript_error = "the worker returned no transcript"
    return out


async def analyze(path: Path, media: MediaConfig, *, transcribe: bool, frames: int) -> MediaAnalysis:
    """Transcribe ``path`` and/or sample ``frames`` frames from it.

    Never raises for a worker problem: a missing interpreter, a timeout, a
    crash or bad output comes back as ``transcript_error`` / ``frames_error``.
    """
    if not transcribe and frames <= 0:
        return MediaAnalysis()
    cmd = worker_command(path, media, transcribe=transcribe, frames=frames)
    timeout = media.transcription.timeout_seconds
    async with _turn():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_worker_env(),
            )
        except OSError as exc:
            reason = f"cannot run {cmd[0]} ({exc.strerror or exc})"
            logger.warning("Media worker: %s", reason)
            return MediaAnalysis.failed(reason, transcribe=transcribe, frames=frames)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("Media worker timed out after %d s on %s", timeout, path)
            return MediaAnalysis.failed(
                f"timed out after {timeout} s (media.transcription.timeout_seconds)",
                transcribe=transcribe, frames=frames,
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
    try:
        raw = json.loads(stdout.decode("utf-8", "replace") or "null")
    except json.JSONDecodeError:
        raw = None
    if not isinstance(raw, dict):
        detail = _last_line(stderr) or f"exit status {proc.returncode}"
        logger.warning("Media worker failed on %s: %s", path, detail)
        return MediaAnalysis.failed(f"the worker failed: {detail}", transcribe=transcribe, frames=frames)
    return parse_report(raw, transcribe=transcribe, frames=frames)


# --------------------------------------------------------------------------- #
#  What the agent reads                                                        #
# --------------------------------------------------------------------------- #

def format_duration(seconds: float | None) -> str:
    """``0:42``, ``12:05`` or ``1:02:03``; ``""`` when unknown."""
    if seconds is None or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_size(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1_000_000 else f"{size / 1_000_000:.1f} MB"


def frame_blocks(analysis: MediaAnalysis) -> list[dict[str, str]]:
    """The sampled frames as image content blocks."""
    return [
        {"type": "base64", "media_type": "image/jpeg", "data": data}
        for _, data in analysis.frames
    ]


_NO_SOUND = "no sound track"


def describe(
    label: str,
    facts: list[str],
    *,
    duration: float | None = None,
    analysis: MediaAnalysis | None = None,
    saved: Path | None = None,
    transcription_enabled: bool = True,
    note: str = "",
) -> str:
    """The lines the agent gets for one file.

    ``[<label>: <facts>, <duration>, …, saved to <path>]``, then a transcript
    line. A video says how many frames are attached, and its transcript is of
    the sound. ``note`` takes the transcript line's place: a file too large
    to fetch, a failed download. ``duration`` is the channel's own figure;
    the file's is used when the channel has none.
    """
    facts = [f for f in facts if f]
    if duration is None and analysis is not None:
        duration = analysis.duration
    if duration is not None:
        facts.append(format_duration(duration))
    picture = analysis is not None and analysis.has_video
    if analysis is not None:
        if picture and analysis.width and analysis.height:
            facts.append(f"{analysis.width}x{analysis.height}")
        if analysis.frames:
            count = len(analysis.frames)
            facts.append(f"{count} frame{'s' if count != 1 else ''} attached")
        elif analysis.frames_error:
            facts.append(f"no frames ({analysis.frames_error})")
        if picture and not analysis.has_audio:
            facts.append("no sound")
    if saved is not None:
        facts.append(f"saved to {saved}")
    header = f"[{label}: {', '.join(facts)}]" if facts else f"[{label}]"
    if note:
        return f"{header}\n{note}"
    if analysis is None:
        return header
    if picture and not analysis.has_audio:
        return header
    what = "Transcript of the sound" if picture else "Transcript"
    if not transcription_enabled:
        return f"{header}\n{what}: {_TRANSCRIPTION_OFF}"
    if analysis.transcript_error == _NO_SOUND:
        return f"{header}\n{what}: none, the file has no sound track"
    if analysis.transcript_error:
        return f"{header}\n{what}: not available ({analysis.transcript_error})"
    if analysis.text is None:
        return header
    if not analysis.text:
        return f"{header}\n{what}: no speech detected"
    lang = f", {analysis.language}" if analysis.language else ""
    return f"{header}\n{what} (automatic{lang}): {analysis.text}"

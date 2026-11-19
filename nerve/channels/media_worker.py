"""Transcribe a voice, audio or video file, and sample frames from a video.

Run by :mod:`nerve.channels.media` in a child process, one file per run, with
whatever interpreter ``media.transcription.python`` names. That interpreter
needs ``faster-whisper`` (and its PyAV) but not Nerve, so this file imports
only the standard library at the top and the media libraries when a step
needs them. A run holds the model in memory only while it lasts: about
1.6 GB for ``large-v3-turbo`` at int8.

Usage::

    python -I media_worker.py --input FILE [--transcribe] [--frames N] ...

Prints one JSON object on stdout:

``duration``
    Seconds, from the container; ``null`` when it does not say.
``has_audio`` / ``has_video``
    Whether the file has a sound track / a picture.
``width`` / ``height``
    Of the picture, after rotation; absent without one.
``language`` / ``language_probability`` / ``text``
    The transcript; ``text`` is ``""`` when no speech was found.
``frames``
    ``[{"t": seconds, "jpeg": base64}]``, evenly spaced over the video.
``transcript_error`` / ``frames_error``
    Why a step that was asked for did not run or failed.

The exit status is 0 whenever the JSON was printed, so a failed step is told
apart from a crashed worker.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from fractions import Fraction


def _probe(path: str) -> dict:
    import av

    out: dict = {"duration": None, "has_audio": False, "has_video": False}
    with av.open(path, mode="r") as container:
        if container.duration:
            out["duration"] = round(container.duration / 1_000_000, 3)
        out["has_audio"] = bool(container.streams.audio)
        video = container.streams.video
        if video:
            out["has_video"] = True
            stream = video[0]
            if out["duration"] is None and stream.duration and stream.time_base:
                out["duration"] = round(float(stream.duration * stream.time_base), 3)
    return out


def _transcribe(path: str, args: argparse.Namespace) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type=args.compute_type,
        cpu_threads=args.threads,
        download_root=args.model_dir or None,
    )
    segments, info = model.transcribe(
        path,
        language=args.language or None,
        vad_filter=True,
        beam_size=5,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "text": text,
    }


def _rotation(frame) -> int:
    """The display rotation of ``frame``, counter-clockwise, in 0..359 degrees.

    PyAV reads it from the frame's display matrix (FFmpeg's
    ``av_display_rotation_get``), in -180..180; a phone's portrait video
    usually says -90. 0 when the frame carries none.
    """
    raw = getattr(frame, "rotation", 0) or 0
    try:
        return int(raw) % 360
    except (TypeError, ValueError):
        return 0


def _jpeg(frame, max_edge: int) -> tuple[bytes, int, int]:
    """Encode one decoded frame as a JPEG no larger than ``max_edge``."""
    import av
    import numpy as np

    turn = _rotation(frame)
    if turn in (90, 180, 270):
        rgb = frame.to_ndarray(format="rgb24")
        # Apply the display matrix: np.rot90 turns counter-clockwise by
        # k * 90 degrees, the same direction the rotation is given in.
        rgb = np.ascontiguousarray(np.rot90(rgb, k=turn // 90))
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
    width, height = frame.width, frame.height
    scale = min(1.0, max_edge / max(width, height))
    # yuvj420p wants even sides.
    out_w = max(2, int(width * scale) // 2 * 2)
    out_h = max(2, int(height * scale) // 2 * 2)
    small = frame.reformat(width=out_w, height=out_h, format="yuvj420p")
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width = out_w
    encoder.height = out_h
    encoder.pix_fmt = "yuvj420p"
    encoder.time_base = Fraction(1, 25)
    encoder.options = {"q:v": "4"}
    data = b"".join(bytes(p) for p in encoder.encode(small))
    data += b"".join(bytes(p) for p in encoder.encode(None))
    return data, out_w, out_h


def _frames(path: str, count: int, max_edge: int, duration: float | None) -> dict:
    import av

    out: dict = {"frames": []}
    with av.open(path, mode="r") as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        span = duration or 0.0
        # Evenly spaced, never on the first or last instant: a fade from or
        # to black is common there.
        targets = [span * (i + 0.5) / count for i in range(count)] if span > 0 else [0.0]
        for target in targets:
            if target > 0 and stream.time_base:
                container.seek(int(target / stream.time_base), stream=stream, backward=True)
            picked = None
            for frame in container.decode(stream):
                picked = frame
                if frame.time is None or frame.time >= target - 0.04:
                    break
            if picked is None:
                continue
            data, width, height = _jpeg(picked, max_edge)
            out["frames"].append({
                "t": round(float(picked.time or 0.0), 2),
                "jpeg": base64.b64encode(data).decode("ascii"),
            })
            out["width"], out["height"] = width, height
            if span <= 0:
                break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--language", default="")
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--max-edge", type=int, default=1280)
    args = parser.parse_args(argv)

    # Below the daemon and the agent: a long transcription must not slow them.
    try:
        os.nice(10)
    except OSError:
        pass

    result: dict = {}
    try:
        result.update(_probe(args.input))
    except ImportError as exc:
        result["probe_error"] = f"PyAV is not installed for {sys.executable} ({exc})"
    except Exception as exc:  # an unreadable or truncated file
        result["probe_error"] = f"{type(exc).__name__}: {exc}"

    if args.transcribe:
        if result.get("probe_error"):
            result["transcript_error"] = result["probe_error"]
        elif not result.get("has_audio"):
            result["transcript_error"] = "no sound track"
        else:
            try:
                result.update(_transcribe(args.input, args))
            except ImportError as exc:
                result["transcript_error"] = (
                    f"faster-whisper is not installed for {sys.executable} ({exc})"
                )
            except Exception as exc:
                result["transcript_error"] = f"{type(exc).__name__}: {exc}"

    if args.frames > 0:
        if result.get("probe_error"):
            result["frames_error"] = result["probe_error"]
        elif result.get("has_video"):
            try:
                result.update(_frames(
                    args.input, args.frames, args.max_edge, result.get("duration"),
                ))
            except Exception as exc:
                result["frames_error"] = f"{type(exc).__name__}: {exc}"

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

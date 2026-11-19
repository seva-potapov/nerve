"""Live locations shared in a Telegram chat, kept for the chat's next message.

Telegram sends a live location once as a message, then every new position as
an edit of that message for as long as the user shares it. A turn per edit
would flood the session, so the channel starts none. It records the positions
here instead, and the user's next message in that chat carries the latest
ones: the agent can then say where the user is, how far they went and how
fast.

Memory only. A restart forgets the positions so far, and the next edit starts
the track again from there.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

#: Telegram's ``live_period`` for a live location shared "until I stop".
LIVE_PERIOD_FOREVER = 0x7FFFFFFF

#: Latest positions listed in the next message, after the first one.
SHOWN = 20

#: How long an ended live location waits for a message before it is dropped.
LINGER = timedelta(minutes=30)

#: A live location that reported nothing for this long is dropped.
STALE = timedelta(hours=24)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def live_period_seconds(location: Any) -> float | None:
    """A live location's sharing period in seconds, or ``None`` if it is not live.

    PTB reports ``live_period`` in seconds, or as a timedelta when
    ``PTB_TIMEDELTA`` is set; both are read. Telegram leaves it out of a static
    location, and out of a live one whose sharing has stopped.
    """
    period = getattr(location, "live_period", None)
    if isinstance(period, timedelta):
        period = period.total_seconds()
    if not _is_number(period) or period <= 0:
        return None
    return float(period)


def _utc(value: Any) -> datetime | None:
    """``value`` as an aware UTC datetime, or ``None`` if it is not a datetime."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clock(moment: datetime, tz: tzinfo | None) -> str:
    return f"{moment.astimezone(tz):%H:%M:%S}"


@dataclass(frozen=True)
class Position:
    """One reported position."""

    at: datetime
    latitude: float
    longitude: float
    accuracy: float | None = None

    @classmethod
    def of(cls, location: Any, at: datetime) -> Position | None:
        """The position in a Telegram location, or ``None`` without coordinates."""
        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if not (_is_number(lat) and _is_number(lon)):
            return None
        accuracy = getattr(location, "horizontal_accuracy", None)
        if not (_is_number(accuracy) and accuracy > 0):
            accuracy = None
        return cls(at, float(lat), float(lon), accuracy)

    def line(self, tz: tzinfo | None) -> str:
        text = f"{_clock(self.at, tz)} {self.latitude:.6f}, {self.longitude:.6f}"
        if self.accuracy is not None:
            text += f" (±{self.accuracy:.0f} m)"
        return text


@dataclass
class Track:
    """The positions of one live location message."""

    started: datetime
    first: Position
    latest: collections.deque[Position] = field(
        default_factory=lambda: collections.deque(maxlen=SHOWN),
    )
    count: int = 1
    until: datetime | None = None  # None while it is shared "until stopped"
    stopped: datetime | None = None
    ended_described: bool = False

    @property
    def last(self) -> Position:
        return self.latest[-1] if self.latest else self.first

    def ended(self, now: datetime) -> datetime | None:
        """When the sharing ended, or ``None`` while it goes on."""
        if self.stopped is not None:
            return self.stopped
        if self.until is not None and now >= self.until:
            return self.until
        return None

    def describe(self, tz: tzinfo | None, now: datetime) -> str:
        ended = self.ended(now)
        if self.stopped is not None:
            state = f"stopped at {_clock(self.stopped, tz)}"
        elif ended is not None:
            state = f"ended at {_clock(ended, tz)}"
        elif self.until is None:
            state = "shared until stopped"
        else:
            state = f"shared until {_clock(self.until, tz)}"
        started = self.started.astimezone(tz)
        noun = "position" if self.count == 1 else "positions"
        lines = [
            f"[Live location since {started:%H:%M:%S %Z}".rstrip()
            + f", {state} ({self.count} {noun}, latest last):",
            self.first.line(tz),
        ]
        left_out = self.count - 1 - len(self.latest)
        if left_out:
            lines.append(f"… {left_out} more …")
        lines.extend(position.line(tz) for position in self.latest)
        return "\n".join(lines) + "]"


class LiveLocations:
    """Live locations per chat, from the first message through its edits."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._tracks: dict[tuple[str, int], Track] = {}

    def record(self, chat_key: str, message: Any, *, at: Any = None) -> None:
        """Keep the live location on ``message``: its first position or a later one.

        ``at`` is when the position was reported, an edit's ``edit_date``; it
        defaults to the message's date. A location without a live period is
        the last position of a known live location: Telegram drops
        ``live_period`` once the sharing stops. On a message not seen before
        it is a static location, and nothing is kept.
        """
        now = self._now()
        self._prune(now)
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        location = getattr(message, "location", None)
        sent = _utc(getattr(message, "date", None))
        position = Position.of(location, _utc(at) or sent or now)
        if position is None:
            return
        period = live_period_seconds(location)
        key = (chat_key, message_id)
        track = self._tracks.get(key)
        if track is None:
            if period is None:
                return
            track = Track(started=sent or position.at, first=position)
            self._tracks[key] = track
        else:
            track.latest.append(position)
            track.count += 1
        if period is None:
            track.stopped = position.at
        elif period >= LIVE_PERIOD_FOREVER:
            track.until = None
        else:
            track.until = track.started + timedelta(seconds=period)

    def describe(self, chat_key: str, tz: tzinfo | None, *, skip: Any = None) -> str:
        """The chat's live locations, for its next message; ``""`` if none.

        ``skip`` is the message being delivered when it is a live location
        itself: its own line says so. A live location that has ended is
        described once more, then dropped. ``tz=None`` is the system's zone.
        """
        now = self._now()
        self._prune(now)
        parts: list[str] = []
        for key, track in list(self._tracks.items()):
            if key[0] != chat_key or key[1] == skip:
                continue
            ended = track.ended(now)
            if ended is not None and track.ended_described:
                del self._tracks[key]
                continue
            parts.append(track.describe(tz, now))
            if ended is not None:
                track.ended_described = True
        return "\n\n".join(parts)

    def _prune(self, now: datetime) -> None:
        for key, track in list(self._tracks.items()):
            ended = track.ended(now)
            if (ended is not None and now - ended > LINGER) or now - track.last.at > STALE:
                del self._tracks[key]

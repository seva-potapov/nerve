"""Reap orphaned zombies when Nerve runs as PID 1.

In a container without an init (no ``init: true`` / ``--init``), the
entrypoint execs ``nerve start`` and Nerve becomes PID 1. Every process whose
parent exits first (a tool call's ``nohup … &``, a shell's grandchild, a CLI's
helper) is then re-parented to Nerve, and only PID 1 can collect its exit
status. Nerve never asked for those children, so nobody waits for them and
they stay zombies: an entry in the process table each, no memory, but they
pile up (over a thousand in a few hours of agent work) and make "is this PID
alive" checks lie.

The sweep collects only zombies that were already zombies at the previous
sweep. Nerve's own children (agent CLIs, ``asyncio`` subprocesses) are
collected by their watchers within milliseconds of exiting, so a zombie that
outlives a whole sweep interval belongs to nobody, and taking its exit status
cannot rob a waiter. Each zombie is reaped by its own PID, never with
``waitpid(-1)``, which could take a child somebody is about to wait for.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# How often the sweep runs; a zombie is reaped at the second sweep that sees it.
SWEEP_INTERVAL_SECONDS = 60.0


def _zombie_children_of_init(proc: Path) -> set[int]:
    """PIDs under ``proc`` that are zombies whose parent is PID 1."""
    found: set[int] = set()
    try:
        entries: Iterable[Path] = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # gone already, or unreadable
        # "<pid> (<comm>) <state> <ppid> ...": comm may hold spaces and
        # parentheses, so split after the LAST ")".
        close = stat.rfind(")")
        if close < 0:
            continue
        fields = stat[close + 1:].split()
        if len(fields) < 2:
            continue
        state, ppid = fields[0], fields[1]
        if state == "Z" and ppid == "1":
            found.add(int(entry.name))
    return found


class OrphanReaper:
    """Collects zombies re-parented to PID 1, two sweeps after it sees them."""

    def __init__(
        self,
        *,
        proc: Path | str = "/proc",
        getpid: Callable[[], int] = os.getpid,
        waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
    ) -> None:
        self._proc = Path(proc)
        self._getpid = getpid
        self._waitpid = waitpid
        self._seen: set[int] = set()
        self.reaped_total = 0

    @property
    def active(self) -> bool:
        """Only PID 1 inherits orphans; any other process has nothing to do."""
        return self._getpid() == 1

    def sweep(self) -> list[int]:
        """One sweep: reap the zombies the previous sweep already saw,
        remember the new ones. Returns the PIDs reaped."""
        if not self.active:
            return []
        zombies = _zombie_children_of_init(self._proc)
        reaped: list[int] = []
        for pid in sorted(zombies & self._seen):
            try:
                done, _status = self._waitpid(pid, os.WNOHANG)
            except OSError as e:  # ChildProcessError: its owner collected it
                logger.debug("waitpid(%d) failed: %s", pid, e)
                continue
            if done == pid:
                reaped.append(pid)
        self._seen = zombies  # a reaped PID is gone from the next scan
        self.reaped_total += len(reaped)
        return reaped


async def run_orphan_reaper(
    reaper: OrphanReaper | None = None,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every ``interval`` seconds until cancelled. Returns at once when
    Nerve is not PID 1."""
    reaper = reaper or OrphanReaper()
    if not reaper.active:
        return
    logger.info("Nerve runs as PID 1: reaping orphaned zombies every %.0f s", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            reaped = await asyncio.to_thread(reaper.sweep)
        except Exception:  # noqa: BLE001 — the sweep must never stop the gateway
            logger.warning("Orphan reaper sweep failed", exc_info=True)
            continue
        if reaped:
            logger.info(
                "Reaped %d orphaned zombie process(es) (%d since start)",
                len(reaped), reaper.reaped_total,
            )

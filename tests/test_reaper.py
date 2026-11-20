"""The orphan reaper (``nerve/reaper.py``): when Nerve is PID 1 it collects
zombies re-parented to it, but only those that outlived a whole sweep."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from nerve.config import GatewayConfig
from nerve.reaper import OrphanReaper, _zombie_children_of_init, run_orphan_reaper


def _proc(tmp_path, procs: dict[int, str]):
    """A fake /proc: pid -> its ``stat`` line."""
    for pid, stat in procs.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(stat)
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("")
    return tmp_path


def _stat(pid: int, comm: str, state: str, ppid: int) -> str:
    return f"{pid} ({comm}) {state} {ppid} {pid} {pid} 0 -1 4194560 100 0 0 0 0 0\n"


class _Wait:
    def __init__(self, gone: set[int] | None = None):
        self.calls: list[tuple[int, int]] = []
        self.gone = gone or set()

    def __call__(self, pid: int, options: int):
        self.calls.append((pid, options))
        if pid in self.gone:
            raise ChildProcessError(10, "No child processes")
        return pid, 0


def test_only_zombies_whose_parent_is_pid_1_are_found(tmp_path):
    proc = _proc(tmp_path, {
        10: _stat(10, "node", "Z", 1),
        11: _stat(11, "bash", "S", 1),        # alive
        12: _stat(12, "node", "Z", 500),      # another process's child
        13: _stat(13, "we(i)rd) name", "Z", 1),  # comm with ')' and spaces
        14: "garbage",
    })
    assert _zombie_children_of_init(proc) == {10, 13}


def test_a_zombie_is_reaped_at_the_second_sweep_that_sees_it(tmp_path):
    proc = _proc(tmp_path, {10: _stat(10, "node", "Z", 1)})
    wait = _Wait()
    reaper = OrphanReaper(proc=proc, getpid=lambda: 1, waitpid=wait)
    assert reaper.sweep() == []          # first sight: only remembered
    assert wait.calls == []
    assert reaper.sweep() == [10]        # still a zombie a sweep later
    assert wait.calls == [(10, os.WNOHANG)]
    assert reaper.reaped_total == 1


def test_a_zombie_its_owner_collects_in_between_is_left_alone(tmp_path):
    proc = _proc(tmp_path, {10: _stat(10, "node", "Z", 1), 11: _stat(11, "sh", "Z", 1)})
    wait = _Wait(gone={11})
    reaper = OrphanReaper(proc=proc, getpid=lambda: 1, waitpid=wait)
    reaper.sweep()
    assert reaper.sweep() == [10]
    assert reaper.reaped_total == 1


def test_a_zombie_that_vanished_is_not_waited_for(tmp_path):
    proc = _proc(tmp_path, {10: _stat(10, "node", "Z", 1)})
    wait = _Wait()
    reaper = OrphanReaper(proc=proc, getpid=lambda: 1, waitpid=wait)
    reaper.sweep()
    (proc / "10" / "stat").unlink()
    (proc / "10").rmdir()
    assert reaper.sweep() == []
    assert wait.calls == []


def test_nothing_happens_when_nerve_is_not_pid_1(tmp_path):
    proc = _proc(tmp_path, {10: _stat(10, "node", "Z", 1)})
    wait = _Wait()
    reaper = OrphanReaper(proc=proc, getpid=lambda: 4242, waitpid=wait)
    assert not reaper.active
    assert reaper.sweep() == [] and reaper.sweep() == []
    assert wait.calls == []


@pytest.mark.asyncio
async def test_the_loop_returns_at_once_when_not_pid_1(tmp_path):
    reaper = OrphanReaper(proc=tmp_path, getpid=lambda: 4242)
    await asyncio.wait_for(run_orphan_reaper(reaper, interval=3600), timeout=1)


@pytest.mark.asyncio
async def test_the_loop_sweeps_and_survives_a_failed_sweep(tmp_path):
    calls: list[str] = []

    class _Flaky(OrphanReaper):
        def sweep(self):
            calls.append("sweep")
            if len(calls) == 1:
                raise RuntimeError("proc unreadable")
            return super().sweep()

    reaper = _Flaky(proc=tmp_path, getpid=lambda: 1)
    task = asyncio.create_task(run_orphan_reaper(reaper, interval=0.01))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3


def test_a_real_zombie_child_is_reaped_by_its_pid(tmp_path):
    """Against the real /proc: a child of this process that exited and was
    never waited for is a zombie; the reaper (told this process is PID 1 and
    pointed at a /proc whose PPid fields name 1) collects it by PID."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = child.pid
    for _ in range(200):
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        if stat[stat.rfind(")") + 2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("the child did not become a zombie")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "stat").write_text(stat[: stat.rfind(")") + 1] + f" Z 1 {stat.split()[-1]}\n")
    reaper = OrphanReaper(proc=proc, getpid=lambda: 1)
    reaper.sweep()
    assert reaper.sweep() == [pid]
    assert not os.path.exists(f"/proc/{pid}")
    child.returncode = 0  # already collected; keep Popen's __del__ quiet


def test_the_switch_is_on_by_default_and_can_be_turned_off():
    assert GatewayConfig.from_dict({}).reap_orphans is True
    assert GatewayConfig.from_dict({"reap_orphans": False}).reap_orphans is False
    assert GatewayConfig.from_dict({"reap_orphans": "false"}).reap_orphans is False

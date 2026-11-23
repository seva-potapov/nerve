"""``GET /api/sessions/{id}/processes`` and ``POST …/processes/{pid}/signal``
against real processes on this machine's ``/proc``."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from nerve.agent import processes as procs
from nerve.agent.engine import AgentEngine
from nerve.config import NerveConfig

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")

SID = "s-proc-1"
PROCESS_KEYS = {
    "pid", "ppid", "role", "task_id", "agent_id", "name", "state", "start_ticks",
    "started_at", "cpu_seconds", "cpu_percent", "rss_bytes", "threads", "cwd", "command",
}


@pytest_asyncio.fixture
async def api(db, tmp_path, bypass_auth, monkeypatch):
    from nerve.gateway.auth import require_auth
    from nerve.gateway.routes import _deps as deps_module
    from nerve.gateway.routes.agents import router as agents_router

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = NerveConfig.from_dict({"workspace": str(workspace), "agent": {"backend": "claude"}})
    engine = AgentEngine(cfg, db)
    engine.PROCESS_KILL_AFTER = 0
    monkeypatch.setattr(deps_module, "_deps", deps_module.RouteDeps(engine=engine, db=db))
    await engine.sessions.get_or_create(SID, source="web", backend="claude", actor=None)
    app = FastAPI()
    app.include_router(agents_router)
    bypass_auth(app)
    app.dependency_overrides[require_auth] = lambda: db.system_actor
    spawned: list[subprocess.Popen] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nerve-test",
    ) as client:
        yield SimpleNamespace(client=client, engine=engine, db=db, spawned=spawned)
    for p in spawned:
        if p.poll() is None:
            p.kill()
            p.wait()


def _tagged(api, *argv: str) -> subprocess.Popen:
    p = subprocess.Popen(
        list(argv), env={**os.environ, procs.TAG_ENV: SID},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    api.spawned.append(p)
    return p


@pytest.mark.asyncio
async def test_unknown_session_is_404(api):
    r = await api.client.get("/api/sessions/nope/processes")
    assert r.status_code == 404
    r = await api.client.post("/api/sessions/nope/processes/5/signal", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_lists_a_tagged_process_without_a_runtime(api):
    p = _tagged(api, "sleep", "30")
    r = await api.client.get(f"/api/sessions/{SID}/processes")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True and body["session_id"] == SID
    assert body["root_pid"] is None
    rows = {row["pid"]: row for row in body["processes"]}
    # A child of the test process, not of PID 1, but outside every live tree.
    assert rows[p.pid]["role"] == "orphan"
    assert set(rows[p.pid]) == PROCESS_KEYS


@pytest.mark.asyncio
async def test_runtime_tree_has_cli_role_and_cli_is_refused(api):
    cli = subprocess.Popen(["sleep", "30"])
    api.spawned.append(cli)
    api.engine.sessions.set_client(SID, SimpleNamespace(process_pid=lambda: cli.pid))
    try:
        body = (await api.client.get(f"/api/sessions/{SID}/processes")).json()
        assert body["root_pid"] == cli.pid
        row = next(r for r in body["processes"] if r["pid"] == cli.pid)
        assert row["role"] == "cli"
        r = await api.client.post(
            f"/api/sessions/{SID}/processes/{cli.pid}/signal",
            json={"signal": "TERM", "start_ticks": row["start_ticks"]},
        )
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_signalable"
        assert cli.poll() is None
    finally:
        api.engine.sessions._clients.pop(SID, None)


@pytest.mark.asyncio
async def test_signal_validation(api):
    p = _tagged(api, "sleep", "30")
    base = f"/api/sessions/{SID}/processes"
    r = await api.client.post(f"{base}/{p.pid}/signal", json={"signal": "HUP"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_signal"
    r = await api.client.post(f"{base}/{p.pid}/signal", json={"start_ticks": "x"})
    assert r.status_code == 422
    r = await api.client.post(f"{base}/{os.getpid()}/signal", json={})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "process_not_found"
    r = await api.client.post(f"{base}/{p.pid}/signal", json={"start_ticks": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "process_changed"
    assert p.poll() is None


@pytest.mark.asyncio
async def test_signal_terminates_and_records_event(api):
    p = _tagged(api, "sleep", "30")
    body = (await api.client.get(f"/api/sessions/{SID}/processes")).json()
    row = next(r for r in body["processes"] if r["pid"] == p.pid)
    r = await api.client.post(
        f"/api/sessions/{SID}/processes/{p.pid}/signal",
        json={"signal": "TERM", "start_ticks": row["start_ticks"]},
    )
    assert r.status_code == 200
    assert r.json() == {"pid": p.pid, "via": "signal", "signalled": [p.pid]}
    assert p.wait(5) == -15
    events = await api.db.get_session_events(SID)
    ev = [e for e in events if e["event_type"] == "process_signal"]
    assert ev and "TERM" in str(ev[0].get("details"))


@pytest.mark.asyncio
async def test_signal_tree(api):
    shell = _tagged(api, "bash", "-c", "sleep 30 & sleep 30 & wait")
    deadline = time.time() + 5
    while time.time() < deadline:
        body = (await api.client.get(f"/api/sessions/{SID}/processes")).json()
        kids = [r for r in body["processes"] if r["ppid"] == shell.pid]
        if len(kids) == 2:
            break
        time.sleep(0.1)
    assert len(kids) == 2
    row = next(r for r in body["processes"] if r["pid"] == shell.pid)
    r = await api.client.post(
        f"/api/sessions/{SID}/processes/{shell.pid}/signal",
        json={"signal": "KILL", "start_ticks": row["start_ticks"], "tree": True},
    )
    assert r.status_code == 200
    signalled = r.json()["signalled"]
    assert signalled[-1] == shell.pid and set(signalled[:-1]) == {k["pid"] for k in kids}
    shell.wait(5)


@pytest.mark.asyncio
async def test_session_list_and_status_carry_process_count(api):
    from nerve.gateway.routes.sessions import router as sessions_router

    app = api.client._transport.app
    app.include_router(sessions_router)
    _tagged(api, "sleep", "30")
    await api.engine.watch_processes_once()
    assert api.engine.process_count(SID) == 1
    listed = (await api.client.get("/api/sessions")).json()
    rows = listed if isinstance(listed, list) else listed.get("sessions", [])
    assert next(s for s in rows if s["id"] == SID)["process_count"] == 1
    status = (await api.client.get(f"/api/sessions/{SID}/status")).json()
    assert status["process_count"] == 1

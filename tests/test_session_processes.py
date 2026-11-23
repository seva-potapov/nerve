"""The session process scanner (nerve.agent.processes), the engine's
listing and signal guards, and the routes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerve.agent import processes as procs
from nerve.agent import task_output

SID = "sess-a"
OTHER = "sess-b"


class FakeProc:
    """A fake ``/proc``: one directory per pid with stat, cmdline, environ
    and the cwd and fd/1 links."""

    def __init__(self, root: Path, tasks: Path):
        self.root = root
        self.tasks = tasks
        root.mkdir(parents=True, exist_ok=True)
        tasks.mkdir(parents=True, exist_ok=True)
        (root / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n")
        self.add(1, 0, "nerve", ["nerve", "start"])

    def add(self, pid, ppid, comm, argv, *, state="S", env=None, fd1=None,
            utime=0, stime=0, threads=1, start=1000, rss=10, cwd="/work"):
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        fields = [state, ppid, pid, pid, 0, -1, 0, 0, 0, 0, 0, utime, stime, 0, 0,
                  20, 0, threads, 0, start, 0, rss]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))
        env = env or {}
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))
        if cwd:
            os.symlink(cwd, d / "cwd")
        if fd1:
            os.symlink(fd1, d / "fd" / "1")
        return d

    def task_file(self, task_id):
        return str(self.tasks / f"{task_id}.output")

    def set_cpu(self, pid, utime):
        p = self.root / str(pid) / "stat"
        text = p.read_text()
        head, rest = text.rsplit(") ", 1)
        parts = rest.split()
        parts[11] = str(utime)
        p.write_text(head + ") " + " ".join(parts) + "\n")


@pytest.fixture
def fake(tmp_path):
    tasks_root = tmp_path / "claude-0"
    fp = FakeProc(tmp_path / "proc", tasks_root / "-work" / "cli-sess" / "tasks")
    fp.oroots = [str(tasks_root)]
    return fp


def _scan(fp, sid=SID, roots=None, background=None, redact=None, scanner=None, max_age=0.0):
    scanner = scanner or procs.ProcessScanner(str(fp.root), self_pid=999999)
    return scanner.session_processes(
        sid, roots if roots is not None else {SID: 100},
        background_tasks=background or {}, redact=redact, max_age=max_age,
        output_roots=fp.oroots,
    ), scanner


def _by_pid(view):
    return {r["pid"]: r for r in view["processes"]}


# --------------------------------------------------------------------------- #
#  Parsing                                                                     #
# --------------------------------------------------------------------------- #


def test_parse_stat_handles_parentheses_and_spaces_in_comm():
    st = procs.parse_stat(
        "42 (a (weird) name) R 7 42 42 0 -1 0 0 0 0 0 30 20 0 0 20 0 3 0 555 0 77\n",
    )
    assert (st.pid, st.comm, st.state, st.ppid) == (42, "a (weird) name", "R", 7)
    assert (st.cpu_ticks, st.threads, st.start_ticks, st.rss_pages) == (50, 3, 555, 77)


def test_parse_stat_rejects_garbage():
    assert procs.parse_stat("") is None
    assert procs.parse_stat("12 (x) S") is None


def test_unsupported_without_proc(tmp_path):
    scanner = procs.ProcessScanner(str(tmp_path / "nope"))
    assert scanner.session_processes(SID, {}) == {"supported": False, "processes": []}


# --------------------------------------------------------------------------- #
#  Roles                                                                       #
# --------------------------------------------------------------------------- #


def test_roles_from_tree_and_task_files(fake):
    fake.add(100, 1, "claude", ["claude", "--print"])
    fake.add(101, 100, "node", ["node", "mcp-remote", "https://x"])          # internal
    fake.add(102, 100, "bash", ["bash", "-c", "sleep 9"], fd1=fake.task_file("bfgaaaaa1"))
    fake.add(103, 102, "sleep", ["sleep", "9"], fd1=fake.task_file("bfgaaaaa1"))
    fake.add(104, 100, "bash", ["bash", "-c", "make"], fd1=fake.task_file("bbgbbbbb2"))
    fake.add(105, 104, "make", ["make"], fd1="/dev/null")                     # inherits
    view, _ = _scan(fake, background={"bbgbbbbb2": "agent-xyz"})
    rows = _by_pid(view)
    assert view["supported"] is True and view["root_pid"] == 100
    assert rows[100]["role"] == "cli"
    assert rows[101]["role"] == "internal"
    assert (rows[102]["role"], rows[102]["task_id"]) == ("call", "bfgaaaaa1")
    assert (rows[103]["role"], rows[103]["task_id"]) == ("call", "bfgaaaaa1")
    assert (rows[104]["role"], rows[104]["agent_id"]) == ("background", "agent-xyz")
    assert (rows[105]["role"], rows[105]["agent_id"]) == ("background", "agent-xyz")
    assert 1 not in rows
    # Sorted by role order.
    assert [r["role"] for r in view["processes"]][0] == "cli"


def test_task_file_outside_roots_or_wrong_shape_is_not_a_call(fake, tmp_path):
    fake.add(100, 1, "claude", ["claude"])
    elsewhere = tmp_path / "evil" / "tasks"
    elsewhere.mkdir(parents=True)
    fake.add(102, 100, "bash", ["bash"], fd1=str(elsewhere / "bfgaaaaa1.output"))
    fake.add(103, 100, "bash", ["bash"], fd1=str(fake.tasks.parent / "bfgaaaaa1.output"))
    fake.add(104, 100, "bash", ["bash"], fd1=fake.task_file("BAD") )
    rows = _by_pid(_scan(fake)[0])
    assert {rows[p]["role"] for p in (102, 103, 104)} == {"internal"}


def test_task_id_of_output_rules(tmp_path):
    root = str(tmp_path)
    assert task_output.task_id_of_output(f"{root}/x/tasks/bxyz12345.output", roots=[root]) == "bxyz12345"
    assert task_output.task_id_of_output(f"{root}/x/tasks/bxyz12345.output (deleted)", roots=[root]) is None
    assert task_output.task_id_of_output(f"{root}/x/bxyz12345.output", roots=[root]) is None
    assert task_output.task_id_of_output(f"/elsewhere/tasks/bxyz12345.output", roots=[root]) is None
    assert task_output.task_id_of_output(None, roots=[root]) is None


def test_tagged_orphans_and_their_children(fake):
    fake.add(100, 1, "claude", ["claude"])
    fake.add(200, 1, "sleep", ["sleep", "300"], env={procs.TAG_ENV: SID, "HOME": "/root"},
             fd1=fake.task_file("bfgccccc3"))
    fake.add(201, 200, "child", ["child"], env={procs.TAG_ENV: SID})
    fake.add(300, 1, "other", ["other"], env={procs.TAG_ENV: OTHER})
    fake.add(400, 1, "plain", ["plain"])
    view, _ = _scan(fake)
    rows = _by_pid(view)
    assert (rows[200]["role"], rows[200]["task_id"]) == ("orphan", "bfgccccc3")
    assert rows[201]["role"] == "orphan"
    assert 300 not in rows and 400 not in rows
    assert "environ" not in json.dumps(view) and "HOME" not in json.dumps(view)


def test_without_live_cli_only_tagged_processes(fake):
    fake.add(200, 1, "sleep", ["sleep"], env={procs.TAG_ENV: SID})
    view, _ = _scan(fake, roots={})
    assert view["root_pid"] is None
    assert [r["role"] for r in view["processes"]] == ["orphan"]


def test_environ_never_read_inside_a_live_tree(fake, monkeypatch):
    fake.add(100, 1, "claude", ["claude"], env={procs.TAG_ENV: SID})
    fake.add(101, 100, "bash", ["bash"], env={procs.TAG_ENV: SID})
    fake.add(500, 1, "claude", ["claude"], env={procs.TAG_ENV: OTHER})
    fake.add(501, 500, "bash", ["bash"], env={procs.TAG_ENV: OTHER})
    fake.add(600, 1, "x", ["x"])
    read = []
    real = procs._read

    def spy(path, *a, **kw):
        if path.endswith("environ"):
            read.append(int(Path(path).parent.name))
        return real(path, *a, **kw)

    monkeypatch.setattr(procs, "_read", spy)
    _scan(fake, roots={SID: 100, OTHER: 500})
    assert sorted(read) == [600]


def test_other_sessions_tree_is_not_listed(fake):
    fake.add(100, 1, "claude", ["claude"])
    fake.add(500, 1, "claude", ["claude"])
    fake.add(501, 500, "bash", ["bash"], fd1=fake.task_file("bfgddddd4"))
    rows = _by_pid(_scan(fake, roots={SID: 100, OTHER: 500})[0])
    assert set(rows) == {100}


def test_vanished_and_zombie_processes(fake):
    fake.add(100, 1, "claude", ["claude"])
    fake.add(101, 100, "bash", [], state="Z", cwd=None)
    (fake.root / "102").mkdir()                       # vanished: no stat
    rows = _by_pid(_scan(fake)[0])
    assert rows[101]["state"] == "Z"
    assert rows[101]["command"] == "[bash] <defunct>"
    assert rows[101]["cwd"] is None
    assert 102 not in rows


def test_fields(fake):
    fake.add(100, 1, "claude", ["claude", "-p"], utime=300, stime=100, threads=7,
             start=2000, rss=256, cwd="/home/x")
    row = _by_pid(_scan(fake)[0])[100]
    tck = procs._clk_tck()
    assert row["cpu_seconds"] == round(400 / tck, 2)
    assert row["threads"] == 7
    assert row["rss_bytes"] == 256 * procs._page_size()
    assert row["start_ticks"] == 2000
    assert row["started_at"].startswith("2023-11-14")
    assert row["cwd"] == "/home/x"
    assert row["command"] == "claude -p"
    assert row["cpu_percent"] is None  # no earlier scan


def test_cpu_percent_and_cache(fake):
    fake.add(100, 1, "claude", ["claude"], utime=0)
    now = [100.0]
    scanner = procs.ProcessScanner(str(fake.root), clock=lambda: now[0], self_pid=999999)
    _scan(fake, scanner=scanner, max_age=1.0)
    fake.set_cpu(100, procs._clk_tck())       # one CPU second
    now[0] = 100.5
    cached, _ = _scan(fake, scanner=scanner, max_age=1.0)
    assert _by_pid(cached)[100]["cpu_seconds"] == 0.0      # the cached pass
    now[0] = 102.0
    fresh, _ = _scan(fake, scanner=scanner, max_age=1.0)
    assert _by_pid(fresh)[100]["cpu_percent"] == 50.0


def test_command_truncated(fake):
    fake.add(100, 1, "claude", ["claude", "x" * 5000])
    cmd = _by_pid(_scan(fake)[0])[100]["command"]
    assert len(cmd) == procs.COMMAND_MAX and cmd.endswith("…")


def test_descendants_leaves_first(fake):
    fake.add(100, 1, "claude", ["claude"])
    fake.add(101, 100, "a", ["a"])
    fake.add(102, 101, "b", ["b"])
    fake.add(103, 101, "c", ["c"])
    _, scanner = _scan(fake)
    assert scanner.descendants(101) == [102, 103]
    assert scanner.descendants(100) == [102, 103, 101]


# --------------------------------------------------------------------------- #
#  Redaction                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw, leaked", [
    ("curl -H 'Authorization: Bearer abcdefgh12345678' x", "abcdefgh12345678"),
    ("curl -H Authorization:token9876543210 x", "token9876543210"),
    ("mysql --password=hunter2xx db", "hunter2xx"),
    ("tool --api-key s3cr3tvalue run", "s3cr3tvalue"),
    ("GH_TOKEN=abc123456789 gh api", "abc123456789"),
    ("git clone https://user:pa55word99@github.com/x", "pa55word99"),
    ("echo ghp_ABCDEFGHIJKLMNOPQRST", "ABCDEFGHIJKLMNOPQRST"),
    ("echo github_pat_11AAAAAAA_bbbbbbbbbbb", "11AAAAAAA_bbbbbbbbbbb"),
    ("x sk-ant-api03-abcdefghijk", "api03-abcdefghijk"),
    ("x xoxb-1234-5678-abcdefgh", "1234-5678-abcdefgh"),
    ("aws AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
])
def test_redaction_patterns(raw, leaked):
    out = procs.Redactor()(raw)
    assert leaked not in out and "***" in out


def test_redaction_known_secrets_exact_and_short_ignored():
    r = procs.Redactor(["plainsecretvalue", "short"])
    assert r("echo plainsecretvalue short") == "echo *** short"


def test_known_secrets_from_env_and_config():
    cfg = SimpleNamespace(
        effective_api_key="cfg-api-key-123", claude_oauth_token="",
        provider=SimpleNamespace(aws_secret_access_key="aws-secret-xyz", aws_access_key_id=None),
    )
    found = procs.known_secrets(cfg, {"GH_TOKEN": "gh-token-val", "PATH": "/usr/bin", "MY_PASSWORD": "pw"})
    assert {"cfg-api-key-123", "aws-secret-xyz", "gh-token-val", "pw"} <= set(found)
    assert "/usr/bin" not in found


def test_scan_redacts_command(fake):
    fake.add(100, 1, "claude", ["claude"])
    fake.add(102, 100, "x", ["x", "--password=hunter2", "--k", "knownsecret99"],
             fd1=fake.task_file("bfgaaaaa1"))
    view, _ = _scan(fake, redact=procs.Redactor(["knownsecret99"]))
    blob = json.dumps(view)
    assert "hunter2" not in blob and "knownsecret99" not in blob


# --------------------------------------------------------------------------- #
#  Live, on this machine's /proc                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_live_tagged_orphan_found_and_env_secret_not_leaked(tmp_path):
    sid = f"live-{os.getpid()}"
    planted = "PLANTED-ENV-SECRET-0123456789"
    env = {**os.environ, procs.TAG_ENV: sid, "SOME_VALUE": planted}
    # A shell that starts a sleep and exits: the sleep is re-parented.
    out = subprocess.run(
        ["bash", "-c", "exec -a 'sleepy --password=hunter2xyz' sleep 30 >/dev/null 2>&1 & echo $!"],
        env=env, capture_output=True, text=True, check=True,
    )
    pid = int(out.stdout.strip())
    try:
        scanner = procs.ProcessScanner()
        deadline = time.time() + 5
        rows = {}
        while time.time() < deadline:
            view = scanner.session_processes(sid, {}, max_age=0.0)
            rows = _by_pid(view)
            if pid in rows:
                break
            time.sleep(0.1)
        assert rows[pid]["role"] == "orphan"
        blob = json.dumps(view)
        assert planted not in blob
        assert "hunter2xyz" not in blob
        assert "sleepy --password=***" in rows[pid]["command"]
    finally:
        os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------------------- #
#  Engine: signal guards                                                       #
# --------------------------------------------------------------------------- #


class _Agents:
    def __init__(self, recs=None):
        self.recs = recs or {}
        self.stopped = []

    def shell_task_ids(self, sid):
        return {r.task_id: r.id for r in self.recs.values()}

    def get(self, sid, aid):
        return self.recs.get(aid)


class _Sessions:
    def __init__(self, clients):
        self.clients = clients

    def client_items(self):
        return list(self.clients.items())

    def get_client(self, sid):
        return self.clients.get(sid)


def _engine(view_rows, recs=None, clients=None):
    from nerve.agent.engine import AgentEngine

    eng = AgentEngine.__new__(AgentEngine)
    eng.config = SimpleNamespace()
    eng.agents = _Agents(recs)
    eng.sessions = _Sessions(clients or {})
    events = []

    class _DB:
        async def log_session_event(self, sid, etype, details, attribution=None):
            events.append((sid, etype, details))

    eng.db = _DB()

    async def fake_view(session_id, *, fresh=False):
        return {"supported": True, "processes": view_rows}

    eng.session_processes = fake_view
    eng.PROCESS_KILL_AFTER = 0
    return eng, events


def _row(pid, role, ppid=1, ticks=10, **kw):
    return {"pid": pid, "ppid": ppid, "role": role, "start_ticks": ticks,
            "command": f"cmd{pid}", "task_id": kw.get("task_id"), "agent_id": kw.get("agent_id")}


@pytest.mark.asyncio
async def test_signal_refuses_unknown_changed_and_protected():
    from nerve.agent.agents.errors import NotSignalable, ProcessChanged, ProcessNotFound

    eng, _ = _engine([_row(100, "cli"), _row(101, "internal", 100), _row(200, "orphan")])
    with pytest.raises(ProcessNotFound):
        await eng.signal_session_process(SID, 999)
    with pytest.raises(ProcessChanged):
        await eng.signal_session_process(SID, 200, start_ticks=11)
    for pid in (100, 101):
        with pytest.raises(NotSignalable):
            await eng.signal_session_process(SID, pid)
    with pytest.raises(NotSignalable):
        await eng.signal_session_process(SID, 200, signal_name="HUP")


@pytest.mark.asyncio
async def test_signal_tree_leaves_first_and_logs(monkeypatch):
    rows = [_row(200, "orphan"), _row(201, "orphan", 200), _row(202, "orphan", 201),
            _row(300, "orphan")]
    eng, events = _engine(rows)
    sent = []
    monkeypatch.setattr(type(eng), "_send_signals",
                        lambda self, wanted, sig: sent.append((list(wanted), sig)) or list(wanted))
    res = await eng.signal_session_process(SID, 200, start_ticks=10, tree=True)
    assert res["signalled"] == [202, 201, 200]
    assert sent == [([202, 201, 200], signal.SIGTERM)]
    assert events[0][1] == "process_signal" and events[0][2]["tree"] is True


@pytest.mark.asyncio
async def test_signal_background_goes_through_agent_stop(monkeypatch):
    rec = SimpleNamespace(id="ag1", task_id="bfgaaaaa1", terminal=False)
    eng, events = _engine([_row(102, "background", 100, task_id="bfgaaaaa1", agent_id="ag1")],
                          recs={"ag1": rec})
    calls = []

    async def stop_agent(sid, aid, *, attribution=None, cascade=True):
        calls.append((sid, aid, cascade))
        return {}, [aid]

    eng.stop_agent = stop_agent
    monkeypatch.setattr(type(eng), "_send_signals", lambda *a: pytest.fail("signalled directly"))
    res = await eng.signal_session_process(SID, 102)
    assert res["via"] == "agent" and calls == [(SID, "ag1", False)]
    assert events[0][2]["via"] == "agent"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_send_signals_checks_start_ticks():
    from nerve.agent.engine import AgentEngine

    eng = AgentEngine.__new__(AgentEngine)
    p = subprocess.Popen(["sleep", "30"])
    try:
        ticks = procs.ProcessScanner().start_ticks(p.pid)
        assert eng._send_signals({p.pid: ticks + 1}, signal.SIGTERM) == []
        assert p.poll() is None
        assert eng._send_signals({p.pid: ticks}, signal.SIGTERM) == [p.pid]
        assert p.wait(5) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()


def test_session_process_root_reads_client():
    from nerve.agent.engine import AgentEngine

    eng = AgentEngine.__new__(AgentEngine)
    eng.sessions = _Sessions({SID: SimpleNamespace(process_pid=lambda: 4242),
                              OTHER: SimpleNamespace(process_pid=lambda: None),
                              "c": SimpleNamespace()})
    assert eng.session_process_root(SID) == 4242
    assert eng.session_process_root(OTHER) is None
    assert eng.session_process_root("c") is None
    assert eng._process_roots() == {SID: 4242}


# --------------------------------------------------------------------------- #
#  Backends                                                                    #
# --------------------------------------------------------------------------- #


def test_claude_env_carries_session_tag():
    from nerve.agent.backends.claude import ClaudeBackend
    from nerve.config import NerveConfig

    backend = ClaudeBackend(SimpleNamespace(config=lambda: NerveConfig()))
    assert backend._build_env(session_id="s-123")[procs.TAG_ENV] == "s-123"
    assert procs.TAG_ENV not in backend._build_env()


def test_claude_client_process_pid():
    from nerve.agent.backends.claude import ClaudeClient

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = SimpleNamespace(_transport=SimpleNamespace(
        _process=SimpleNamespace(pid=77, returncode=None)))
    assert client.process_pid() == 77
    client._sdk._transport._process.returncode = 0
    assert client.process_pid() is None
    client._sdk = SimpleNamespace(_transport=None)
    assert client.process_pid() is None


# --------------------------------------------------------------------------- #
#  Process counts: the watch pass                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_watch_pass_counts_work_processes_and_broadcasts_changes(fake, monkeypatch):
    from nerve.agent import engine as engine_mod
    from nerve.agent.engine import AgentEngine

    fake.add(100, 1, "claude", ["claude"])
    fake.add(101, 100, "node", ["node", "mcp"])                                  # internal
    fake.add(102, 100, "bash", ["bash"], fd1=fake.task_file("bfgaaaaa1"))       # call
    fake.add(200, 1, "sleep", ["sleep"], env={procs.TAG_ENV: OTHER})            # orphan
    fake.add(201, 200, "zz", [], state="Z", env={procs.TAG_ENV: OTHER}, cwd=None)

    eng = AgentEngine.__new__(AgentEngine)
    eng.agents = _Agents()
    eng.sessions = _Sessions({SID: SimpleNamespace(process_pid=lambda: 100)})
    eng._process_scanner = procs.ProcessScanner(str(fake.root), self_pid=999999)
    sent = []

    async def broadcast(channel, msg):
        sent.append((channel, msg))

    monkeypatch.setattr(engine_mod.broadcaster, "broadcast", broadcast)
    monkeypatch.setattr(procs.task_output, "output_roots", lambda: fake.oroots)

    assert await eng.watch_processes_once() == {SID: 1, OTHER: 1}
    assert eng.process_count(SID) == 1 and eng.process_count("nope") == 0
    assert sorted(m["session_id"] for _, m in sent) == sorted([OTHER, SID])
    assert all(c == "__global__" and m["type"] == "session_processes" for c, m in sent)

    sent.clear()
    assert await eng.watch_processes_once() == {SID: 1, OTHER: 1}
    assert sent == []                                    # nothing changed

    import shutil
    shutil.rmtree(fake.root / "200")
    eng._process_scanner._snap = None
    await eng.watch_processes_once()
    assert sent == [("__global__", {"type": "session_processes", "session_id": OTHER, "process_count": 0})]

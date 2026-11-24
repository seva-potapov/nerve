"""The OS processes a session started, read from ``/proc`` (Linux only).

A session's processes are its CLI's descendants, plus every process tagged
with ``NERVE_SESSION_ID=<session>`` in its environment (the backends set the
tag in the CLI's environment, and every shell and command inherits it). The
tag is what ties a process back to its session once its shell exited and it
was re-parented to PID 1.

Each process gets a role, decided at the CLI's direct children and inherited
by their descendants:

- ``cli``: the session's CLI (the root).
- ``call``: a Bash call's shell, found through its stdout: the CLI points it
  at ``tasks/<task_id>.output``, and that id is not a background agent.
- ``background``: the task id is a background shell or monitor record;
  carries the record's ``agent_id``.
- ``internal``: any other child of the CLI (MCP servers, hooks).
- ``orphan``: tagged for the session but outside its live CLI tree (left
  running after its call, or the CLI is gone).

Environment content never leaves this module: a process's environment is
read only when it is outside every live CLI tree, and only the tag's value
is kept. Commands are redacted (:class:`Redactor`) before they are returned.

The scan is pure over ``proc_root`` so tests run it on a fake tree.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from nerve.agent import task_output

TAG_ENV = "NERVE_SESSION_ID"
_TAG_BYTES = TAG_ENV.encode() + b"="
COMMAND_MAX = 2048
CACHE_SECONDS = 1.0

ROLES = ("cli", "call", "background", "internal", "orphan")
# Roles a user may signal: never the CLI itself or its own helpers.
SIGNALABLE_ROLES = frozenset({"call", "background", "orphan"})
# The session's own work (what "N processes running" counts): not the CLI
# or its helpers.
WORK_ROLES = SIGNALABLE_ROLES


def _clk_tck() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, ValueError, OSError):
        return 4096


# --------------------------------------------------------------------------- #
#  Redaction                                                                   #
# --------------------------------------------------------------------------- #

_MASK = "***"
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH)",
    re.IGNORECASE,
)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer <x> / Authorization: <x>
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)?\s*)([^\s\"']+)"), r"\1" + _MASK),
    (re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"), r"\1" + _MASK),
    # --password=x, --api-key x, --token=x
    (re.compile(
        r"(?i)(--?[\w-]*(?:password|passwd|token|secret|api[-_]?key|access[-_]?key|credential)[\w-]*(?:=|\s+))"
        r"([^\s\"']+)",
    ), r"\1" + _MASK),
    # FOO_TOKEN=x in an inline environment assignment
    (re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY)[A-Z0-9_]*=)([^\s\"']+)",
    ), r"\1" + _MASK),
    # scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)"), r"\1" + _MASK + r"\3"),
    # Well-known token prefixes
    (re.compile(r"\b(ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|sk-ant-|sk-|xox[abprse]-|AKIA|ASIA)[A-Za-z0-9_\-]{8,}"),
     r"\1" + _MASK),
]


class Redactor:
    """Masks secrets in a command line: the exact values Nerve knows (8+
    characters) and common credential shapes."""

    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets = sorted(
            {s for s in secrets if isinstance(s, str) and len(s) >= 8},
            key=len, reverse=True,
        )

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _MASK)
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text


def known_secrets(config: Any = None, environ: Mapping[str, str] | None = None) -> list[str]:
    """Secret values Nerve itself holds: its own environment's credential
    variables and the config's keys and tokens."""
    env = os.environ if environ is None else environ
    out = [v for k, v in env.items() if _SECRET_NAME_RE.search(k) and v]
    if config is not None:
        for path in (
            "effective_api_key", "claude_oauth_token", "anthropic_api_key",
            "provider.aws_secret_access_key", "provider.aws_access_key_id",
            "codex.api_key", "telegram.bot_token", "auth.jwt_secret",
            "auth.password_hash",
        ):
            obj: Any = config
            for part in path.split("."):
                try:
                    obj = getattr(obj, part, None)
                except Exception:  # noqa: BLE001 (a property may raise)
                    obj = None
                if obj is None:
                    break
            if isinstance(obj, str) and obj:
                out.append(obj)
    return out


# --------------------------------------------------------------------------- #
#  Reading /proc                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class _Stat:
    pid: int
    comm: str
    state: str
    ppid: int
    cpu_ticks: int
    threads: int
    start_ticks: int
    rss_pages: int


def parse_stat(text: str) -> _Stat | None:
    """``/proc/<pid>/stat``. The command name may hold spaces and
    parentheses, so the fields after it are split after the LAST ``)``."""
    try:
        open_at = text.index("(")
        close_at = text.rindex(")")
        pid = int(text[:open_at].strip())
        comm = text[open_at + 1:close_at]
        rest = text[close_at + 2:].split()
        # rest[0] is field 3 (state); field N is rest[N - 3].
        return _Stat(
            pid=pid, comm=comm, state=rest[0], ppid=int(rest[1]),
            cpu_ticks=int(rest[11]) + int(rest[12]),
            threads=int(rest[17]), start_ticks=int(rest[19]),
            rss_pages=int(rest[21]),
        )
    except (ValueError, IndexError):
        return None


def _read(path: str, binary: bool = False, limit: int = 65536) -> Any:
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return None
    return data if binary else data.decode("utf-8", "replace")


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _env_tag(data: bytes | None) -> str | None:
    """The tag's value in an ``environ`` blob; nothing else is kept."""
    if not data:
        return None
    for entry in data.split(b"\0"):
        if entry.startswith(_TAG_BYTES):
            return entry[len(_TAG_BYTES):].decode("utf-8", "replace") or None
    return None


def supported(proc_root: str = "/proc") -> bool:
    return os.path.isfile(os.path.join(proc_root, "self", "stat")) or os.path.isfile(
        os.path.join(proc_root, "1", "stat"),
    )


@dataclass
class Snapshot:
    """One pass over ``/proc``: every process's ``stat``, and the tag of
    those outside every live CLI tree."""

    taken_at: float
    stats: dict[int, _Stat]
    children: dict[int, list[int]]
    tags: dict[int, str]
    roots: dict[str, int]                 # session id -> live CLI pid
    tree_of: dict[int, str]               # pid -> session whose live tree holds it
    cpu_percent: dict[int, float] = field(default_factory=dict)
    boot_time: float = 0.0


class ProcessScanner:
    """Scans ``/proc`` at most once per :data:`CACHE_SECONDS` and builds
    per-session process lists from the shared snapshot. Thread-safe: the
    scan runs in a worker thread."""

    def __init__(self, proc_root: str = "/proc", *, clock: Any = time.monotonic,
                 self_pid: int | None = None):
        self.proc_root = proc_root
        self._clock = clock
        self._self_pid = os.getpid() if self_pid is None else self_pid
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        # (pid, start_ticks) -> (cpu_ticks, taken_at): the previous scan, for CPU %.
        self._prev_cpu: dict[tuple[int, int], tuple[int, float]] = {}
        # (pid, start_ticks) -> tag or None: environ is fixed at exec, so
        # read once per process.
        self._tag_cache: dict[tuple[int, int], str | None] = {}
        self._tck = _clk_tck()
        self._page = _page_size()

    @property
    def supported(self) -> bool:
        return supported(self.proc_root)

    # -- the pass ------------------------------------------------------- #

    def _boot_time(self) -> float:
        text = _read(os.path.join(self.proc_root, "stat")) or ""
        for line in text.splitlines():
            if line.startswith("btime "):
                try:
                    return float(line.split()[1])
                except (IndexError, ValueError):
                    break
        return 0.0

    def snapshot(self, roots: Mapping[str, int], *, max_age: float = CACHE_SECONDS) -> Snapshot:
        """The cached snapshot when it is younger than ``max_age`` and was
        taken for the same roots; else a fresh pass."""
        with self._lock:
            now = self._clock()
            snap = self._snap
            if snap is not None and now - snap.taken_at < max_age and snap.roots == dict(roots):
                return snap
            snap = self._scan(dict(roots), now)
            self._snap = snap
            return snap

    def _scan(self, roots: dict[str, int], now: float) -> Snapshot:
        stats: dict[int, _Stat] = {}
        try:
            names = os.listdir(self.proc_root)
        except OSError:
            names = []
        for name in names:
            if not name.isdigit():
                continue
            text = _read(os.path.join(self.proc_root, name, "stat"), limit=4096)
            if not text:
                continue  # vanished
            st = parse_stat(text)
            if st is not None:
                stats[st.pid] = st
        children: dict[int, list[int]] = {}
        for st in stats.values():
            children.setdefault(st.ppid, []).append(st.pid)
        for kids in children.values():
            kids.sort()

        tree_of: dict[int, str] = {}
        for sid, root in roots.items():
            if root not in stats:
                continue
            stack = [root]
            while stack:
                pid = stack.pop()
                if pid in tree_of:
                    continue
                tree_of[pid] = sid
                stack.extend(children.get(pid, ()))

        tags: dict[int, str] = {}
        seen: set[tuple[int, int]] = set()
        for pid, st in stats.items():
            if pid in tree_of or pid == self._self_pid or pid == 1 or st.state == "Z":
                continue
            key = (pid, st.start_ticks)
            seen.add(key)
            if key in self._tag_cache:
                tag = self._tag_cache[key]
            else:
                tag = _env_tag(_read(
                    os.path.join(self.proc_root, str(pid), "environ"),
                    binary=True, limit=1 << 20,
                ))
                self._tag_cache[key] = tag
            if tag:
                tags[pid] = tag
        for key in [k for k in self._tag_cache if k not in seen]:
            del self._tag_cache[key]

        cpu: dict[int, float] = {}
        prev = self._prev_cpu
        cur: dict[tuple[int, int], tuple[int, float]] = {}
        for pid, st in stats.items():
            key = (pid, st.start_ticks)
            cur[key] = (st.cpu_ticks, now)
            old = prev.get(key)
            if old is not None and now > old[1]:
                cpu[pid] = round(
                    max(0.0, (st.cpu_ticks - old[0]) / self._tck / (now - old[1]) * 100.0), 1,
                )
        self._prev_cpu = cur
        return Snapshot(
            taken_at=now, stats=stats, children=children, tags=tags,
            roots=roots, tree_of=tree_of, cpu_percent=cpu,
            boot_time=self._boot_time(),
        )

    # -- per-session view ----------------------------------------------- #

    def session_processes(
        self, session_id: str, roots: Mapping[str, int], *,
        background_tasks: Mapping[str, str] | None = None,
        redact: Redactor | None = None, max_age: float = CACHE_SECONDS,
        output_roots: list[str] | None = None,
    ) -> dict:
        """The session's processes as JSON-ready dicts.

        ``roots``: every session's live CLI pid (all of them, so that no
        environment is read inside another session's tree).
        ``background_tasks``: task id -> agent id of the session's
        background shells and monitors.
        """
        if not self.supported:
            return {"supported": False, "processes": []}
        snap = self.snapshot(roots, max_age=max_age)
        background_tasks = background_tasks or {}
        redact = redact or Redactor()
        oroots = output_roots if output_roots is not None else task_output.output_roots()
        root = roots.get(session_id)
        if root not in snap.stats:
            root = None

        rows: dict[int, dict] = {}

        def _row(pid: int, role: str, task_id: str | None, agent_id: str | None) -> dict:
            st = snap.stats[pid]
            pdir = os.path.join(self.proc_root, str(pid))
            if st.state == "Z":
                command = f"[{st.comm}] <defunct>"
            else:
                raw = _read(os.path.join(pdir, "cmdline"), binary=True, limit=1 << 16) or b""
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
                command = " ".join(parts) if parts else f"[{st.comm}]"
            command = redact(command)
            if len(command) > COMMAND_MAX:
                command = command[: COMMAND_MAX - 1] + "…"
            started = snap.boot_time + st.start_ticks / self._tck if snap.boot_time else None
            return {
                "pid": pid,
                "ppid": st.ppid,
                "role": role,
                "task_id": task_id,
                "agent_id": agent_id,
                "name": st.comm,
                "state": st.state,
                "start_ticks": st.start_ticks,
                "started_at": (
                    datetime.fromtimestamp(started, UTC).isoformat() if started else None
                ),
                "cpu_seconds": round(st.cpu_ticks / self._tck, 2),
                "cpu_percent": snap.cpu_percent.get(pid),
                "rss_bytes": st.rss_pages * self._page,
                "threads": st.threads,
                "cwd": None if st.state == "Z" else _readlink(os.path.join(pdir, "cwd")),
                "command": command,
            }

        def _walk(start: int, role: str, task_id: str | None, agent_id: str | None) -> None:
            stack = [start]
            while stack:
                pid = stack.pop()
                if pid in rows or pid not in snap.stats:
                    continue
                rows[pid] = _row(pid, role, task_id, agent_id)
                stack.extend(reversed(snap.children.get(pid, ())))

        if root is not None:
            rows[root] = _row(root, "cli", None, None)
            for child in snap.children.get(root, ()):
                link = _readlink(os.path.join(self.proc_root, str(child), "fd", "1"))
                task_id = task_output.task_id_of_output(link, roots=oroots)
                if task_id is None:
                    _walk(child, "internal", None, None)
                elif task_id in background_tasks:
                    _walk(child, "background", task_id, background_tasks[task_id])
                else:
                    _walk(child, "call", task_id, None)

        # Tagged processes outside the live tree: a tagged process whose
        # parent is tagged too belongs under that parent.
        for pid in sorted(snap.tags):
            if snap.tags[pid] != session_id or pid in rows:
                continue
            st = snap.stats[pid]
            if snap.tags.get(st.ppid) == session_id:
                continue
            link = _readlink(os.path.join(self.proc_root, str(pid), "fd", "1"))
            task_id = task_output.task_id_of_output(link, roots=oroots)
            _walk(pid, "orphan", task_id, background_tasks.get(task_id) if task_id else None)
        # Untagged children of an orphan (a command that cleared its
        # environment) were taken by _walk; nothing else is left.

        order = {r: i for i, r in enumerate(ROLES)}
        procs = sorted(rows.values(), key=lambda r: (order[r["role"]], r["start_ticks"], r["pid"]))
        return {
            "supported": True,
            "root_pid": root,
            "scanned_at": datetime.now(UTC).isoformat(),
            "processes": procs,
        }

    def session_ids(self, roots: Mapping[str, int], *, max_age: float = CACHE_SECONDS) -> set[str]:
        """Sessions with a live CLI or at least one tagged process."""
        snap = self.snapshot(roots, max_age=max_age)
        return {sid for sid, pid in roots.items() if pid in snap.stats} | set(snap.tags.values())

    # -- signals -------------------------------------------------------- #

    def descendants(self, pid: int) -> list[int]:
        """``pid``'s descendants in the last snapshot, leaves first."""
        snap = self._snap
        if snap is None:
            return []
        out: list[int] = []

        def visit(p: int) -> None:
            for c in snap.children.get(p, ()):
                visit(c)
                out.append(c)

        visit(pid)
        return out

    def start_ticks(self, pid: int) -> int | None:
        """The process's current start time, read now (not from the cache)."""
        text = _read(os.path.join(self.proc_root, str(pid), "stat"), limit=4096)
        st = parse_stat(text) if text else None
        return st.start_ticks if st else None

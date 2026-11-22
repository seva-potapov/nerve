"""Where the Claude CLI keeps a task's output file, and how to tell a real
one from a planted path.

Every Bash call the CLI runs (foreground or background) and every Monitor
script writes to ``<temp>/claude-<uid>/<cwd>/<cli-session>/tasks/<task_id>.output``.
The agents route reads background output through :func:`safe_output_path`;
the process scanner (:mod:`nerve.agent.processes`) links a process to its
call through :func:`task_id_of_output`.
"""

from __future__ import annotations

import os
import re
import tempfile

# A CLI task id (the output file is named after it).
TASK_ID_RE = re.compile(r"^[a-z][0-9a-z]{7,31}$")
_SUFFIX = ".output"


def output_roots() -> list[str]:
    """Where the Claude CLI writes task output: its per-user directory under
    the temp dir. The CLI uses ``/tmp`` even where Python's temp dir is
    elsewhere (macOS sets ``TMPDIR`` to a per-user folder), so both count."""
    uid = getattr(os, "getuid", lambda: 0)()
    return sorted({
        os.path.realpath(os.path.join(tempfile.gettempdir(), f"claude-{uid}")),
        os.path.realpath(f"/tmp/claude-{uid}"),
    })


def _under(real: str, roots: list[str]) -> bool:
    return any(real.startswith(root + os.sep) for root in roots)


def safe_output_path(
    output_file: str | None, task_id: str | None, *, roots: list[str] | None = None,
) -> str | None:
    """The real path of a task's output file, or ``None`` unless it is
    ``<task_id>.output`` inside the CLI's temp root (no symlink out, no other
    file)."""
    if not output_file or not task_id or not TASK_ID_RE.match(task_id):
        return None
    name = f"{task_id}{_SUFFIX}"
    if os.path.basename(output_file) != name:
        return None
    real = os.path.realpath(output_file)
    if os.path.basename(real) != name:
        return None
    return real if _under(real, roots if roots is not None else output_roots()) else None


def task_id_of_output(target: str | None, *, roots: list[str]) -> str | None:
    """The task id of an open file's path (a ``/proc/<pid>/fd/N`` link
    target), or ``None`` unless it is ``<task_id>.output`` in a ``tasks``
    directory inside one of ``roots``.

    The kernel already resolved the path, so no symlink check is needed;
    a deleted file (`` (deleted)`` suffix) does not count.
    """
    if not target or not target.endswith(_SUFFIX):
        return None
    head, name = os.path.split(target)
    if os.path.basename(head) != "tasks" or not _under(target, roots):
        return None
    task_id = name[: -len(_SUFFIX)]
    return task_id if TASK_ID_RE.match(task_id) else None

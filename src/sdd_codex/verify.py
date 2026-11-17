"""The verify command of a task: where it comes from and how it runs.

Design decision D-10: the command comes from the task record itself — a
ready command in ``**Test:**`` or selectors in ``**Verifies:**`` substituted
into the ``verify.selector_runner`` template. A task with neither field is
rejected by id; a "generic" command from the config is never substituted.
Only the launch template lives in the config, never the subject of the check.

The runner executes the command itself (D-09), without a shell, and captures
the combined output — the red-run proof is this module's output verbatim.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sdd_codex.config import VerifyConfig
from sdd_codex.errors import TaskRejected
from sdd_codex.format.tasks import Task


@dataclass(frozen=True)
class VerifyResult:
    """One execution of a task's verify command."""

    command: str
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.returncode == 0


def verify_command(task: Task, verify: VerifyConfig) -> str:
    """BEH-08: the command comes from the task record, never from the config."""
    if task.test_command:
        command = task.test_command
    elif task.verifies:
        # Selectors are quoted so pytest ids with quotes or spaces survive
        # the no-shell split in run_verify.
        selectors = " ".join(shlex.quote(selector) for selector in task.verifies)
        command = verify.selector_runner.format(selectors=selectors)
    else:
        raise TaskRejected(
            task.id,
            "task has no '**Test:**' command and no '**Verifies:**' selectors; "
            "a generic command from the config is never substituted",
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise TaskRejected(
            task.id, f"verify command cannot be parsed: {command!r} ({exc})"
        ) from exc
    if not argv:
        raise TaskRejected(task.id, "verify command is empty")
    return command


def command_files(task: Task) -> list[str]:
    """Repo-relative files named by the task's verify command.

    Selector file parts (``tests/x.py::test_a`` → ``tests/x.py``) plus the
    relative path-looking tokens of ``**Test:**`` (containing ``/`` or a
    file suffix). Absolute tokens are interpreters or tools on the machine,
    never repository test files, and are skipped.
    """
    files: list[str] = []

    def add(candidate: str) -> None:
        if candidate and not candidate.startswith("/") and candidate not in files:
            files.append(candidate)

    for selector in task.verifies:
        add(selector.split("::")[0].strip())
    tokens = shlex.split(task.test_command) if task.test_command else []
    for index, token in enumerate(tokens):
        part = token.split("::")[0]
        if token.startswith("-") or part.startswith("/"):
            continue
        if index == 0 and "/" not in part:
            continue  # bare program name resolved via PATH (python3.11, make)
        if "/" in part or PurePosixPath(part).suffix:
            add(part)
    return files


def run_verify(command: str, cwd: str | Path, timeout_sec: int) -> VerifyResult:
    """Execute the verify command (no shell) and capture its combined output."""
    argv = shlex.split(command)
    if not argv:
        return VerifyResult(command, returncode=2, output="empty verify command")
    try:
        proc = subprocess.run(
            argv,
            check=False,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return VerifyResult(
            command,
            returncode=-1,
            output=f"{partial}\n[verify command timed out after {timeout_sec}s]",
            timed_out=True,
        )
    except OSError as exc:
        return VerifyResult(
            command, returncode=127, output=f"cannot run {argv[0]!r}: {exc}"
        )
    return VerifyResult(command, proc.returncode, proc.stdout or "")

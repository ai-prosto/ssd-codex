"""Frozen-files check: changed paths against the task's allowed list.

Design decision D-11: the allowed list of a task is the paths from
``**Touches:**`` (a directory covers its contents) plus the files named by
its verify command; the rest of the tree is frozen. The check compares
``git status --porcelain`` with that list — it trusts git, not the agent.
Any extra modified, created or deleted file fails the attempt and the tree
is rolled back. A task that lists no files is rejected by id.

Design decision D-19: paths are normalized and must stay inside the
repository — ``..`` and absolute paths are rejected. ``.sdd-codex/`` is
outside the check entirely.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from sdd_codex.errors import TaskRejected
from sdd_codex.format.tasks import Task
from sdd_codex.git import Git, is_artifact_path, parse_porcelain
from sdd_codex.verify import command_files


@dataclass(frozen=True)
class FreezeViolation:
    """One changed path outside the task's allowed list."""

    status: str
    path: str

    def __str__(self) -> str:
        return f"{self.path} ({self.status.strip() or '??'})"


def _normalize(task_id: str, raw: str) -> str:
    path = posixpath.normpath(raw.strip().rstrip("/"))
    if raw.strip().startswith("/") or path.split("/")[0] in ("..", ".", ""):
        raise TaskRejected(task_id, f"path {raw.strip()!r} escapes the repository")
    return path


def allowed_test_paths(task: Task) -> tuple[str, ...]:
    """Files named by the verify command — all the test-writing step may touch."""
    paths: list[str] = []
    for entry in command_files(task):
        norm = _normalize(task.id, entry)
        if norm not in paths:
            paths.append(norm)
    return tuple(paths)


def allowed_paths(task: Task) -> tuple[str, ...]:
    """BEH-12: ``**Touches:**`` plus the verify command's files, nothing else."""
    paths: list[str] = []
    for entry in task.touches:
        norm = _normalize(task.id, entry)
        if norm not in paths:
            paths.append(norm)
    for norm in allowed_test_paths(task):
        if norm not in paths:
            paths.append(norm)
    if not paths:
        raise TaskRejected(
            task.id,
            "task lists no files: nothing in '**Touches:**' and no files "
            "named by its verify command",
        )
    return tuple(paths)


def _covered(path: str, allowed: tuple[str, ...]) -> bool:
    return any(
        path == entry or path.startswith(entry + "/") for entry in allowed
    )


def check_frozen(
    status_lines: list[str], allowed: tuple[str, ...]
) -> list[FreezeViolation]:
    """Compare porcelain output with the allowed list; return every violation."""
    violations: list[FreezeViolation] = []
    for change in parse_porcelain(status_lines):
        if is_artifact_path(change.path):
            continue
        if not _covered(change.path, tuple(allowed)):
            violations.append(FreezeViolation(change.status, change.path))
    return violations


def rollback(git: Git) -> None:
    """Roll the tree back: tracked edits restored, untracked files removed.

    ``.sdd-codex/`` survives — logs and reports of the failed attempt stay.
    """
    git.checkout_all()
    git.clean_untracked()

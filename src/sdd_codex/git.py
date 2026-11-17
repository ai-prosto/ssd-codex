"""Git operations: diff, porcelain status, commit to the current branch.

Design decision D-13: this module can only ``diff``, ``status`` and
``commit`` on the current HEAD. There is no branch creation, no ``push``
and no pull-request handling anywhere in the code. A dirty tree before an
attempt starts is a refusal, so foreign edits can never slip into a task's
commit.

The artifacts directory ``.sdd-codex/`` is invisible to every check here:
it is excluded from the dirty-tree refusal and never staged by a commit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from sdd_codex.errors import GitError

ARTIFACTS_DIR = ".sdd-codex"


@dataclass(frozen=True)
class Change:
    """One changed path from ``git status --porcelain``."""

    status: str
    path: str


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of paths with special characters."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return (
            path[1:-1]
            .encode("utf-8")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8", "replace")
        )
    return path


def parse_porcelain(lines: list[str]) -> list[Change]:
    """Parse ``git status --porcelain`` lines; a rename yields both paths."""
    changes: list[Change] = []
    for line in lines:
        if len(line) < 4:
            continue
        status, rest = line[:2], line[3:]
        if " -> " in rest and status.strip().startswith(("R", "C")):
            old, new = rest.split(" -> ", 1)
            parts = [old, new]
        else:
            parts = [rest]
        for part in parts:
            changes.append(Change(status, _unquote(part).rstrip("/")))
    return changes


def is_artifact_path(path: str) -> bool:
    """``.sdd-codex/`` is outside every freeze and cleanliness check."""
    return path == ARTIFACTS_DIR or path.startswith(ARTIFACTS_DIR + "/")


class Git:
    """Thin wrapper over the ``git`` CLI for one repository."""

    def __init__(self, repo: str | Path) -> None:
        self.repo = Path(repo)

    def _run(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise GitError(f"cannot run git: {exc}") from exc
        if proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed "
                f"(exit {proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").strip()

    def status_porcelain(self) -> list[str]:
        return self._run(
            "status", "--porcelain", "--untracked-files=all"
        ).splitlines()

    def changes(self) -> list[Change]:
        return parse_porcelain(self.status_porcelain())

    def is_clean(self) -> bool:
        return not any(
            not is_artifact_path(change.path) for change in self.changes()
        )

    def ensure_clean(self) -> None:
        """Refuse to start on a dirty tree (BEH-14): no foreign edits in commits."""
        foreign = [
            f"{change.status} {change.path}"
            for change in self.changes()
            if not is_artifact_path(change.path)
        ]
        if foreign:
            raise GitError(
                "working tree is dirty; commit or stash these changes first:\n"
                + "\n".join(foreign)
            )

    def diff_head(self) -> str:
        return self._run("diff", "HEAD")

    def checkout_all(self) -> None:
        """Restore every tracked file to HEAD."""
        self._run("checkout", "--quiet", "--", ".")

    def clean_untracked(self) -> None:
        """Remove untracked files and directories, keeping ``.sdd-codex/``."""
        self._run("clean", "-fdq", "-e", ARTIFACTS_DIR, "--", ".")

    def commit_all(self, message: str) -> str:
        """Stage everything except artifacts and commit to the current HEAD."""
        self._run("add", "-A", "--", ".", f":(exclude){ARTIFACTS_DIR}")
        self._run("commit", "--quiet", "-m", message)
        return self.head()

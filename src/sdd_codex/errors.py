"""Error types shared across sdd-codex.

Format and dependency problems are collected as :class:`SpecIssue` values and
raised together inside :class:`SpecFormatError`, so every problem of a spec set
is visible at once — before any paid agent call is made.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpecIssue:
    """One format or dependency problem, pinned to file / line / task."""

    file: str
    line: int | None
    task_id: str | None
    message: str

    def __str__(self) -> str:
        location = self.file if self.line is None else f"{self.file}:{self.line}"
        if self.task_id:
            return f"{location}: {self.task_id}: {self.message}"
        return f"{location}: {self.message}"


class SpecFormatError(Exception):
    """Spec set does not parse or its dependencies are invalid."""

    def __init__(self, issues: list[SpecIssue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


class ConfigError(Exception):
    """sdd-codex.config.yaml is missing, malformed, or has unknown keys."""

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


class TaskRejected(Exception):
    """A task record cannot be executed as written; carries the task id.

    Raised before any paid call or code change: a task without a verify
    command, without a file list, or with a path escaping the repository is
    refused by name instead of being papered over with defaults.
    """

    def __init__(self, task_id: str, message: str) -> None:
        super().__init__(f"{task_id}: {message}")
        self.task_id = task_id


class GitError(Exception):
    """A git operation failed or the working tree is in the wrong state."""


class PlannerError(Exception):
    """The planner agent failed or returned unusable output."""


class CoderError(Exception):
    """The coder agent process failed to run at all.

    Raised out of the agent callable and caught by the task loop: a crashed
    coder closes the attempt like any other failed step (D-09).
    """


class ReviewError(Exception):
    """The reviewer agent failed to run at all.

    An *unrecognised reply* is not an error — it parses into a ``block``
    verdict (BEH-17); this exception is for the agent process itself
    failing, which closes the attempt like any other crashed step.
    """


class CliError(Exception):
    """A command refused to run; carries the process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code

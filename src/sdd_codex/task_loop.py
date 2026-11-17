"""The cycle of one task attempt: test → proven red run → code → green run.

Design decision D-09: four steps in a fixed order. The coder agent writes
the test; **the runner itself** executes the verify command and saves the
failing output to ``attempt-NN.red.txt``; the agent writes code; the runner
executes **the same** command string and the task is accepted only when it
passes. A test that passes at the red step proves nothing — the attempt is
closed as failed before any coding. The test files are frozen by hash after
the red run, so a green result can never be obtained by editing the test.

Every step failure closes the attempt, the tree is rolled back, and the
reason lands in the attempt log. On success the runner commits to the
current branch (D-13) and puts the ``DONE`` mark into ``tasks.md`` within
the same commit (D-18).

The coder agent is injectable: any callable taking a :class:`CoderRequest`
and editing the working tree. The real Codex CLI agent arrives with the
review/orchestration tasks; tests substitute a scripted fake.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sdd_codex.config import Config
from sdd_codex.format.status import mark_task_done
from sdd_codex.format.tasks import Task
from sdd_codex.freeze import (
    allowed_paths,
    allowed_test_paths,
    check_frozen,
    rollback,
)
from sdd_codex.git import Git
from sdd_codex.verify import run_verify, verify_command

STEP_WRITE_TEST = "write_test"
STEP_RED = "red"
STEP_CODE = "code"
STEP_GREEN = "green"
STEP_FREEZE = "freeze"
STEP_COMMIT = "commit"


@dataclass(frozen=True)
class CoderRequest:
    """Everything the coder agent receives for one step of one attempt."""

    task: Task
    step: str  # STEP_WRITE_TEST or STEP_CODE
    attempt: int
    allowed_files: tuple[str, ...]
    verify_command: str
    red_output: str = ""


CoderAgent = Callable[[CoderRequest], None]


@dataclass
class AttemptResult:
    """The outcome of one attempt of one task."""

    task_id: str
    number: int
    verify_command: str
    log_path: Path
    accepted: bool = False
    step_reached: str = ""
    failure_reason: str = ""
    red_output_path: Path | None = None
    commit: str = ""
    log_lines: list[str] = field(default_factory=list)


def _hash_files(repo: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for rel in paths:
        file = repo / rel
        hashes[rel] = (
            hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
        )
    return hashes


def run_task_attempt(
    task: Task,
    config: Config,
    repo: str | Path,
    coder: CoderAgent,
    task_dir: str | Path,
    attempt: int = 1,
) -> AttemptResult:
    """Run one attempt of ``task``; never raises on a *failed* attempt.

    Raises :class:`~sdd_codex.errors.TaskRejected` when the task record
    itself is unusable (no verify command, no file list) and
    :class:`~sdd_codex.errors.GitError` when the tree is dirty before the
    start — both are refusals, not failed attempts.
    """
    repo = Path(repo)
    git = Git(repo)
    command = verify_command(task, config.verify)
    allowed = allowed_paths(task)
    test_paths = allowed_test_paths(task)

    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / f"attempt-{attempt:02d}.log"
    result = AttemptResult(
        task_id=task.id, number=attempt, verify_command=command, log_path=log_path
    )
    log = result.log_lines
    log.append(f"task {task.id} attempt {attempt:02d}")
    log.append(f"verify command: {command}")
    log.append(f"allowed files: {', '.join(allowed)}")

    def finish() -> AttemptResult:
        log_path.write_text("\n".join(log) + "\n", encoding="utf-8")
        return result

    def fail(step: str, reason: str) -> AttemptResult:
        result.step_reached = step
        result.failure_reason = reason
        log.append(f"[{step}] FAIL: {reason}")
        rollback(git)
        log.append("[rollback] working tree rolled back")
        return finish()

    git.ensure_clean()

    # Step 1 — the agent writes the test; only test files may change.
    result.step_reached = STEP_WRITE_TEST
    coder(
        CoderRequest(
            task=task,
            step=STEP_WRITE_TEST,
            attempt=attempt,
            allowed_files=test_paths,
            verify_command=command,
        )
    )
    violations = check_frozen(git.status_porcelain(), test_paths)
    if violations:
        return fail(
            STEP_WRITE_TEST,
            "non-test files changed while writing the test: "
            + ", ".join(str(violation) for violation in violations),
        )
    log.append("[write_test] ok")

    # Step 2 — the runner itself proves the red run (BEH-09).
    result.step_reached = STEP_RED
    red = run_verify(command, repo, config.verify.timeout_sec)
    red_path = task_dir / f"attempt-{attempt:02d}.red.txt"
    red_path.write_text(red.output, encoding="utf-8")
    result.red_output_path = red_path
    log.append(f"[red] $ {command}")
    log.append(f"[red] exit {red.returncode}; output saved to {red_path.name}")
    if red.passed:
        return fail(
            STEP_RED,
            "verify command already passes before any code change; "
            "a test that is green at the red step proves nothing (BEH-10)",
        )
    frozen = _hash_files(repo, test_paths)
    log.append(f"[red] test files frozen by hash: {', '.join(test_paths) or '-'}")

    # Step 3 — the agent writes code; test files are frozen by hash.
    result.step_reached = STEP_CODE
    coder(
        CoderRequest(
            task=task,
            step=STEP_CODE,
            attempt=attempt,
            allowed_files=allowed,
            verify_command=command,
            red_output=red.output,
        )
    )
    current = _hash_files(repo, test_paths)
    tampered = sorted(path for path in frozen if frozen[path] != current[path])
    if tampered:
        return fail(
            STEP_CODE,
            "test files changed after the red run (frozen hash broken): "
            + ", ".join(tampered),
        )
    log.append("[code] ok")

    # Step 4 — the same command string must pass now (BEH-11).
    result.step_reached = STEP_GREEN
    green = run_verify(command, repo, config.verify.timeout_sec)
    log.append(f"[green] $ {command}")
    log.append(f"[green] exit {green.returncode}")
    if not green.passed:
        return fail(
            STEP_GREEN,
            f"verify command failed after coding (exit {green.returncode}):\n"
            f"{green.output}",
        )

    # Step 5 — freeze: only the task's listed files may have changed.
    result.step_reached = STEP_FREEZE
    violations = check_frozen(git.status_porcelain(), allowed)
    if violations:
        return fail(
            STEP_FREEZE,
            "files outside the task's list changed: "
            + ", ".join(str(violation) for violation in violations),
        )
    log.append("[freeze] only allowed files changed")

    # Step 6 — commit to the current branch; the DONE mark joins the commit.
    result.step_reached = STEP_COMMIT
    mark_task_done(Path(task.file), task.id)
    message = config.git.commit_template.format(
        task_id=task.id, task_name=task.name
    )
    commit = git.commit_all(message)
    result.commit = commit
    result.accepted = True
    log.append(
        f"[commit] {commit[:12]} on branch {git.current_branch()}: {message}"
    )
    return finish()

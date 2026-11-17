"""The cycle of one task attempt: test → proven red run → code → green run.

Design decision D-09: four steps in a fixed order. The coder agent writes
the test; **the runner itself** executes the verify command and saves the
failing output to ``attempt-NN.red.txt``; the agent writes code; the runner
executes **the same** command string and the task is accepted only when it
passes. A test that passes at the red step proves nothing — the attempt is
closed as failed before any coding. The test files are frozen by hash after
the red run, so a green result can never be obtained by editing the test.

Every step failure — including an exception out of the coder agent or any
step — closes the attempt, the tree is rolled back, and the reason lands
in the attempt log. Before the commit a separate reviewer agent judges the
diff (D-12, see :mod:`sdd_codex.review`): only its ``commit`` verdict opens
the path to the commit, ``block`` or an unrecognised reply closes the
attempt and carries the comments into the next attempt's prompt. On success
the runner commits to the current branch (D-13) and puts the ``DONE`` mark
into ``tasks.md`` within the same commit (D-18).

The coder agent is injectable: any callable taking a :class:`CoderRequest`
and editing the working tree. The real Codex CLI agent arrives with the
orchestration task; tests substitute a scripted fake.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sdd_codex.config import Config
from sdd_codex.errors import GitError
from sdd_codex.format.status import mark_task_done
from sdd_codex.format.tasks import Task
from sdd_codex.freeze import (
    allowed_paths,
    allowed_test_paths,
    check_frozen,
    reject_escaping_paths,
    rollback,
)
from sdd_codex.git import Git
from sdd_codex.review import ReviewerAgent, review_path, run_review
from sdd_codex.verify import run_verify, verify_command

STEP_WRITE_TEST = "write_test"
STEP_RED = "red"
STEP_CODE = "code"
STEP_GREEN = "green"
STEP_FREEZE = "freeze"
STEP_REVIEW = "review"
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
    # BEH-17: the review comments of the previous blocked attempt, so the
    # coder sees what the reviewer objected to.
    review_comments: tuple[str, ...] = ()


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
    review_path: Path | None = None
    review_verdict: str = ""
    review_comments: tuple[str, ...] = ()
    commit: str = ""
    log_lines: list[str] = field(default_factory=list)


def _hash_files(repo: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    """Content hashes of the given paths; a directory contributes every file
    under it, so a test inside ``tests/`` cannot be edited, added or deleted
    unnoticed. Bytecode caches are skipped: the verify runs create them."""
    hashes: dict[str, str | None] = {}

    def add_file(key: str, file: Path) -> None:
        hashes[key] = hashlib.sha256(file.read_bytes()).hexdigest()

    for rel in paths:
        target = repo / rel
        if target.is_dir():
            for file in sorted(target.rglob("*")):
                if not file.is_file() or file.suffix == ".pyc":
                    continue
                if "__pycache__" in file.parts:
                    continue
                add_file(file.relative_to(repo).as_posix(), file)
        elif target.is_file():
            add_file(rel, target)
        else:
            hashes[rel] = None
    return hashes


def run_task_attempt(
    task: Task,
    config: Config,
    repo: str | Path,
    coder: CoderAgent,
    task_dir: str | Path,
    attempt: int = 1,
    reviewer: ReviewerAgent | None = None,
    review_comments: Sequence[str] = (),
) -> AttemptResult:
    """Run one attempt of ``task``; never raises on a *failed* attempt.

    ``reviewer`` is the automatic review gate before the commit (BEH-16);
    the orchestrator always wires one, ``None`` skips the gate for
    lower-level tests. ``review_comments`` are the comments of the previous
    blocked attempt; they travel into every coder prompt of this attempt.

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
    reject_escaping_paths(repo, task.id, allowed)

    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / f"attempt-{attempt:02d}.log"
    result = AttemptResult(
        task_id=task.id, number=attempt, verify_command=command, log_path=log_path
    )
    log = result.log_lines
    prior_comments = tuple(review_comments)
    log.append(f"task {task.id} attempt {attempt:02d}")
    log.append(f"verify command: {command}")
    log.append(f"allowed files: {', '.join(allowed)}")
    if prior_comments:
        log.append(
            "review comments from the previous attempt: "
            + "; ".join(prior_comments)
        )

    def finish() -> AttemptResult:
        log_path.write_text("\n".join(log) + "\n", encoding="utf-8")
        return result

    def fail(step: str, reason: str) -> AttemptResult:
        result.step_reached = step
        result.failure_reason = reason
        log.append(f"[{step}] FAIL: {reason}")
        try:
            rollback(git)
            log.append("[rollback] working tree rolled back")
        except GitError as exc:
            log.append(f"[rollback] FAILED, tree may be dirty: {exc}")
        return finish()

    git.ensure_clean()

    try:
        # Step 1 — the agent writes the test; D-11: after every step only
        # the task's allowed list may differ from HEAD.
        result.step_reached = STEP_WRITE_TEST
        coder(
            CoderRequest(
                task=task,
                step=STEP_WRITE_TEST,
                attempt=attempt,
                allowed_files=allowed,
                verify_command=command,
                review_comments=prior_comments,
            )
        )
        violations = check_frozen(git.status_porcelain(), allowed)
        if violations:
            return fail(
                STEP_WRITE_TEST,
                "files outside the task's list changed while writing the test: "
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
        log.append(
            f"[red] test files frozen by hash: {', '.join(test_paths) or '-'}"
        )

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
                review_comments=prior_comments,
            )
        )
        current = _hash_files(repo, test_paths)
        tampered = sorted(
            path
            for path in frozen.keys() | current.keys()
            if frozen.get(path) != current.get(path)
        )
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

        # Step 6 — the review gate (D-12): a separate reviewer agent, on its
        # own model, judges the diff automatically; no human is asked and
        # only its "commit" verdict opens the path to the commit (BEH-16).
        if reviewer is not None:
            result.step_reached = STEP_REVIEW
            verdict = run_review(
                task=task,
                attempt=attempt,
                model=config.openai.models.for_role("reviewer"),
                git=git,
                reviewer=reviewer,
                task_dir=task_dir,
                red_output=red.output,
                green_output=green.output,
            )
            result.review_path = review_path(task_dir, attempt)
            result.review_verdict = verdict.verdict
            result.review_comments = verdict.comments
            log.append(
                f"[review] verdict {verdict.verdict!r} "
                f"saved to {result.review_path.name}"
            )
            if not verdict.approved:
                # BEH-17: a blocked attempt closes without a commit and
                # counts as a retry exactly like a failed test.
                return fail(
                    STEP_REVIEW,
                    "reviewer verdict 'block': "
                    + ("; ".join(verdict.comments) or "no comments"),
                )

        # Step 7 — commit to the current branch; the DONE mark joins the commit.
        result.step_reached = STEP_COMMIT
        tasks_file = Path(task.file)
        if not tasks_file.is_absolute():
            tasks_file = repo / tasks_file
        mark_task_done(tasks_file, task.id)
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
    except Exception as exc:  # noqa: BLE001 — a crashing step is a failed
        # attempt with a rolled-back tree, never an escaping exception that
        # leaves the agent's half-done edits in place (D-09).
        return fail(
            result.step_reached or STEP_WRITE_TEST,
            f"unexpected error: {exc!r}",
        )

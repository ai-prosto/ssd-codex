"""BEH-09 / BEH-10 / BEH-11: the attempt cycle of one task."""

from __future__ import annotations

from pathlib import Path

import pytest

from sdd_codex import task_loop
from sdd_codex.errors import GitError
from sdd_codex.task_loop import (
    STEP_CODE,
    STEP_GREEN,
    STEP_RED,
    run_task_attempt,
)

ALWAYS_GREEN_TEST = 'print("ok")\n'


def record_verify_runs(
    monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, str]]
) -> None:
    real_run_verify = task_loop.run_verify

    def recording(command: str, cwd: Path, timeout_sec: int):
        events.append(("verify", command))
        return real_run_verify(command, cwd, timeout_sec)

    monkeypatch.setattr(task_loop, "run_verify", recording)


def test_runner_proves_red_before_coding(
    task_repo, make_coder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEH-09: after the test-writing step the runner itself executes the
    verify command — before the agent touched any code — and the failing
    output is stored in attempt-NN.red.txt."""
    events: list[tuple[str, str]] = []
    record_verify_runs(monkeypatch, events)
    coder = make_coder(events=events)

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert result.accepted
    command = task_repo.task.test_command
    assert events == [
        ("coder", "write_test"),
        ("verify", command),  # the red run, run by the runner, before coding
        ("coder", "code"),
        ("verify", command),
    ]
    red = task_repo.task_dir / "attempt-01.red.txt"
    assert result.red_output_path == red
    assert "missing or wrong src/out.txt" in red.read_text(encoding="utf-8")


def test_green_at_red_step_fails_attempt(task_repo, make_coder) -> None:
    """BEH-10: a verify command that passes right after the test-writing step
    closes the attempt as failed — no coding, no commit, reason logged."""
    coder = make_coder(test_body=ALWAYS_GREEN_TEST)

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == STEP_RED
    assert coder.calls == ["write_test"]  # the coding step never started
    assert "proves nothing" in result.failure_reason
    assert not result.commit
    assert task_repo.commit_count() == 1  # no commit was made
    assert task_repo.porcelain() == []  # the tree was rolled back
    log = (task_repo.task_dir / "attempt-01.log").read_text(encoding="utf-8")
    assert "FAIL" in log
    assert "proves nothing" in log


def test_accepted_only_when_same_command_passes_green(
    task_repo, make_coder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEH-11: the green run uses the very same command string as the red run
    and only its success accepts the task; the DONE mark joins the commit."""
    events: list[tuple[str, str]] = []
    record_verify_runs(monkeypatch, events)
    coder = make_coder()

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert result.accepted
    commands = [payload for kind, payload in events if kind == "verify"]
    assert commands == [result.verify_command] * 2  # same string, red and green
    assert task_repo.commit_count() == 2
    assert task_repo.porcelain() == []
    assert task_repo.git("log", "-1", "--format=%s").strip() == "TASK-101: Demo task"
    committed = task_repo.git("show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == ["src/out.txt", "tasks.md", "tests/check.py"]
    assert "P2 | DONE" in (task_repo.repo / "tasks.md").read_text(encoding="utf-8")


def test_failing_green_run_closes_attempt(task_repo, make_coder) -> None:
    coder = make_coder(out_content="wrong\n")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == STEP_GREEN
    assert "failed after coding" in result.failure_reason
    assert task_repo.commit_count() == 1
    assert task_repo.porcelain() == []


def test_tampered_test_files_break_the_frozen_hash(task_repo, make_coder) -> None:
    """BEH-11: editing the test after the red run breaks the frozen hash and
    the attempt is not accepted, even though the command would pass."""
    coder = make_coder(tamper_test=True)

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == STEP_CODE
    assert "tests/check.py" in result.failure_reason
    assert "frozen hash" in result.failure_reason
    assert task_repo.commit_count() == 1
    assert task_repo.porcelain() == []


def test_non_test_edit_during_write_test_step_fails(task_repo, make_coder) -> None:
    """D-09: while writing the test only the test files may change."""

    def rogue_coder(request) -> None:
        (task_repo.repo / "src").mkdir(exist_ok=True)
        (task_repo.repo / "src" / "out.txt").write_text("early\n", encoding="utf-8")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, rogue_coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == task_loop.STEP_WRITE_TEST
    assert "src/out.txt" in result.failure_reason
    assert task_repo.porcelain() == []


def test_dirty_tree_before_start_is_refused(task_repo, make_coder) -> None:
    """BEH-14: a dirty tree before the attempt starts is a refusal, so foreign
    edits can never end up in the task's commit."""
    (task_repo.repo / "tasks.md").write_text("foreign edit\n", encoding="utf-8")
    coder = make_coder()

    with pytest.raises(GitError, match="dirty"):
        run_task_attempt(
            task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
        )
    assert coder.calls == []

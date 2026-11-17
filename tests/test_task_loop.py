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


def test_file_outside_allowed_during_write_test_fails(task_repo, make_coder) -> None:
    """D-11: after the test-writing step, any file outside the task's allowed
    list is a violation — the attempt closes before the red run."""

    def rogue_coder(request) -> None:
        (task_repo.repo / "rogue.txt").write_text("early\n", encoding="utf-8")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, rogue_coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == task_loop.STEP_WRITE_TEST
    assert "rogue.txt" in result.failure_reason
    assert task_repo.porcelain() == []


def test_write_test_step_may_touch_any_allowed_file(task_repo, make_coder) -> None:
    """D-11: the per-step check uses the task's full allowed list, so a task
    whose verify command names no files (``make check`` style) stays runnable;
    a helper written inside ``**Touches:**`` at the test step is no violation."""
    coder = make_coder(helper_at_write_test="src/helper.txt")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert result.accepted
    committed = task_repo.git("show", "--name-only", "--format=", "HEAD").split()
    assert "src/helper.txt" in committed


def test_coder_exception_closes_attempt_with_rollback(task_repo, make_coder) -> None:
    """A crashing coder agent (the realistic failure of the real Codex CLI)
    is a failed attempt: tree rolled back, reason logged, nothing raised."""
    base = make_coder()

    def crashing(request) -> None:
        base(request)
        if request.step == task_loop.STEP_CODE:
            raise RuntimeError("codex exploded")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, crashing, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == STEP_CODE
    assert "codex exploded" in result.failure_reason
    assert task_repo.commit_count() == 1
    assert task_repo.porcelain() == []  # no half-done edits left behind
    log = (task_repo.task_dir / "attempt-01.log").read_text(encoding="utf-8")
    assert "codex exploded" in log
    assert "rolled back" in log


def test_hash_freeze_covers_directory_contents(tmp_path: Path) -> None:
    """D-09: when a frozen path is a directory, every file under it is part
    of the hash — editing, adding or deleting a test inside it is detected."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("a\n", encoding="utf-8")
    (tests_dir / "__pycache__").mkdir()
    (tests_dir / "__pycache__" / "test_a.pyc").write_bytes(b"cache")

    frozen = task_loop._hash_files(tmp_path, ("tests",))
    assert set(frozen) == {"tests/test_a.py"}  # caches are not part of the hash

    (tests_dir / "test_a.py").write_text("b\n", encoding="utf-8")
    assert task_loop._hash_files(tmp_path, ("tests",)) != frozen

    (tests_dir / "test_a.py").write_text("a\n", encoding="utf-8")
    (tests_dir / "test_new.py").write_text("sneaky\n", encoding="utf-8")
    assert set(task_loop._hash_files(tmp_path, ("tests",))) > set(frozen)


def test_artifacts_inside_repo_stay_out_of_the_commit(task_repo, make_coder) -> None:
    """``.sdd-codex/`` inside the repository is never staged, even when it is
    not gitignored, and the attempt artifacts survive the successful commit."""
    (task_repo.repo / ".gitignore").write_text("", encoding="utf-8")
    task_repo.git("commit", "-aqm", "drop ignore rules")
    task_dir = task_repo.repo / ".sdd-codex" / "TASK-101"

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, make_coder(), task_dir
    )

    assert result.accepted
    committed = task_repo.git("show", "--name-only", "--format=", "HEAD").split()
    assert not any(path.startswith(".sdd-codex") for path in committed)
    assert (task_dir / "attempt-01.red.txt").is_file()
    assert (task_dir / "attempt-01.log").is_file()


def test_relative_tasks_file_resolves_against_repo(task_repo, make_coder) -> None:
    """``task.file`` may be repo-relative; the DONE mark must land in the
    repo's tasks.md, not in a file relative to the process cwd."""
    from dataclasses import replace

    task = replace(task_repo.task, file="tasks.md")

    result = run_task_attempt(
        task, task_repo.config, task_repo.repo, make_coder(), task_repo.task_dir
    )

    assert result.accepted
    assert "P2 | DONE" in (task_repo.repo / "tasks.md").read_text(encoding="utf-8")


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

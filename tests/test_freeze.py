"""BEH-12 / BEH-13: frozen files are checked via git, not on trust."""

from __future__ import annotations

import pytest

from sdd_codex.errors import TaskRejected
from sdd_codex.format.tasks import Task
from sdd_codex.freeze import allowed_paths, check_frozen, rollback
from sdd_codex.git import Git
from sdd_codex.task_loop import STEP_FREEZE, run_task_attempt


def make_task(**kwargs: object) -> Task:
    return Task(id="TASK-042", name="demo", file="tasks.md", line=1, position=0, **kwargs)  # type: ignore[arg-type]


def test_only_task_listed_files_allowed() -> None:
    """BEH-12: Touches paths (a directory covers its contents) plus the files
    named by the verify command pass the check; ``.sdd-codex/`` is outside the
    check; a task that lists no files is rejected by id."""
    task = make_task(
        touches=["src/", "docs/readme.md"],
        verifies=["tests/test_a.py::t_one"],
    )
    allowed = allowed_paths(task)
    assert allowed == ("src", "docs/readme.md", "tests/test_a.py")

    status = [
        " M src/deep/module.py",  # inside an allowed directory
        "?? tests/test_a.py",  # named by the verify command
        " D docs/readme.md",  # listed file, deletion is still its change
        "?? .sdd-codex/runs/r1/attempt-01.log",  # artifacts: never checked
    ]
    assert check_frozen(status, allowed) == []

    extra = check_frozen(["?? src/x.py", "?? stray.txt", " M README.md"], allowed)
    assert [violation.path for violation in extra] == ["stray.txt", "README.md"]

    with pytest.raises(TaskRejected) as excinfo:
        allowed_paths(make_task())
    assert excinfo.value.task_id == "TASK-042"

    # D-19: paths escaping the repository are rejected, not silently allowed.
    with pytest.raises(TaskRejected, match="escapes"):
        allowed_paths(make_task(touches=["../evil.py"]))
    with pytest.raises(TaskRejected, match="escapes"):
        allowed_paths(make_task(touches=["/etc/passwd"]))


def test_extra_changed_file_rejects_attempt_and_rolls_back(
    task_repo, make_coder
) -> None:
    """BEH-13: a file outside the task's list fails the attempt; the tree is
    rolled back, the violation is named in the log, there is no commit."""
    coder = make_coder(extra_file="stray.txt")

    result = run_task_attempt(
        task_repo.task, task_repo.config, task_repo.repo, coder, task_repo.task_dir
    )

    assert not result.accepted
    assert result.step_reached == STEP_FREEZE
    assert "stray.txt" in result.failure_reason
    log = (task_repo.task_dir / "attempt-01.log").read_text(encoding="utf-8")
    assert "stray.txt" in log
    assert "rolled back" in log

    assert task_repo.commit_count() == 1  # no commit
    assert task_repo.porcelain() == []  # tree rolled back…
    assert not (task_repo.repo / "stray.txt").exists()
    assert not (task_repo.repo / "src" / "out.txt").exists()
    # …and tasks.md still carries TODO, the attempt left no mark.
    assert "TODO" in (task_repo.repo / "tasks.md").read_text(encoding="utf-8")


def test_rollback_restores_tracked_edits_and_keeps_artifacts(task_repo) -> None:
    gitignore = task_repo.repo / ".gitignore"
    original = gitignore.read_text(encoding="utf-8")
    gitignore.write_text(original + "junk\n", encoding="utf-8")
    (task_repo.repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    artifact = task_repo.repo / ".sdd-codex" / "runs" / "r1" / "log.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("keep me\n", encoding="utf-8")

    rollback(Git(task_repo.repo))

    assert gitignore.read_text(encoding="utf-8") == original
    assert not (task_repo.repo / "untracked.txt").exists()
    assert artifact.read_text(encoding="utf-8") == "keep me\n"
    assert task_repo.porcelain() == []

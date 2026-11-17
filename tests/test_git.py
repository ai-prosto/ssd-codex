"""BEH-14: commits go to the current branch only — no branching, no PR."""

from __future__ import annotations

import pytest

from sdd_codex.errors import GitError
from sdd_codex.git import Change, Git, parse_porcelain
from sdd_codex.task_loop import run_task_attempt


def test_commits_to_current_branch_without_branching_or_pr(
    task_repo, make_coder
) -> None:
    """BEH-14: a green attempt makes exactly one commit on the current HEAD;
    no branch appears or changes, no remote and no push are involved."""
    task_repo.git("checkout", "-q", "-b", "work")
    branches_before = set(task_repo.git("branch", "--list").split())

    result = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        make_coder(),
        task_repo.task_dir,
    )

    assert result.accepted
    git = Git(task_repo.repo)
    assert git.current_branch() == "work"  # the run never switched branches
    branches_after = set(task_repo.git("branch", "--list").split())
    assert branches_after == branches_before  # no new branch was created
    assert task_repo.git("rev-list", "--count", "work").strip() == "2"
    assert task_repo.git("rev-list", "--count", "main").strip() == "1"
    assert result.commit == git.head()
    assert (
        task_repo.git("log", "-1", "--format=%s").strip() == "TASK-101: Demo task"
    )  # message from git.commit_template

    assert task_repo.git("remote").strip() == ""  # no remote, nothing to push
    # The git module exposes no branching, push or pull-request operation.
    for forbidden in ("push", "create_branch", "branch", "pull_request", "open_pr"):
        assert not hasattr(Git, forbidden)


def test_ensure_clean_and_status_helpers(task_repo) -> None:
    git = Git(task_repo.repo)
    assert git.is_clean()
    git.ensure_clean()  # no exception on a clean tree
    assert git.diff_head() == ""

    (task_repo.repo / "tasks.md").write_text("edited\n", encoding="utf-8")
    assert not git.is_clean()
    with pytest.raises(GitError, match="tasks.md"):
        git.ensure_clean()
    assert "tasks.md" in git.diff_head()

    # Artifacts never make the tree dirty, even when they are not gitignored.
    git.checkout_all()
    artifact = task_repo.repo / ".sdd-codex" / "note.txt"
    artifact.parent.mkdir()
    artifact.write_text("local\n", encoding="utf-8")
    assert git.is_clean()
    git.ensure_clean()


def test_parse_porcelain_handles_renames_and_quoted_paths() -> None:
    lines = [
        " M src/app.py",
        "?? docs/",
        'R  "old name.py" -> new_name.py',
        '?? "\\320\\244.py"',  # git C-quotes UTF-8 bytes as octal escapes
        "x",  # too short: ignored
    ]
    assert parse_porcelain(lines) == [
        Change(" M", "src/app.py"),
        Change("??", "docs"),
        Change("R ", "old name.py"),
        Change("R ", "new_name.py"),
        Change("??", "Ф.py"),
    ]


def test_git_failure_raises_git_error(tmp_path) -> None:
    with pytest.raises(GitError, match="failed"):
        Git(tmp_path).current_branch()  # not a repository

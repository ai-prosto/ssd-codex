"""BEH-18: ``sdd-codex run`` executes a ready set without a planning step."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import StaticReviewer

from sdd_codex import cli


@dataclass
class RecordingPlanner:
    """A planner that must never be called by ``run``."""

    requests: list = field(default_factory=list)

    def __call__(self, request):
        self.requests.append(request)
        raise AssertionError("the planner must not be called by 'run' (BEH-18)")


def test_runs_ready_set_without_planning_step(
    spec_repo, make_spec_coder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEH-18 / D-08: the three set files already exist (made elsewhere);
    ``run`` parses and executes them — no planner call, no rewriting of the
    set files, and nothing was executed before this command."""
    monkeypatch.chdir(spec_repo.repo)
    assert spec_repo.commit_count() == 1  # nothing executed before `run`
    planner = RecordingPlanner()
    coder = make_spec_coder()
    requirements_before = (spec_repo.repo / "spec" / "requirements.md").read_bytes()
    design_before = (spec_repo.repo / "spec" / "design.md").read_bytes()

    rc = cli.main(
        ["run"], planner=planner, coder=coder, reviewer=StaticReviewer()
    )

    assert rc == 0
    assert planner.requests == []  # the planning step never ran
    # The set files were not rewritten: requirements and design are
    # byte-identical; tasks.md only received its DONE marks (D-18).
    files = (spec_repo.repo / "spec" / "requirements.md").read_bytes()
    assert files == requirements_before
    assert (spec_repo.repo / "spec" / "design.md").read_bytes() == design_before
    tasks_text = (spec_repo.repo / "spec" / "tasks.md").read_text(encoding="utf-8")
    assert tasks_text.count("P2 | DONE") == 2
    # Both tasks were executed and committed on the current branch.
    assert spec_repo.commit_count() == 3
    assert (spec_repo.repo / "src" / "out_201.txt").is_file()
    assert (spec_repo.repo / "src" / "out_202.txt").is_file()


def test_missing_set_is_refused_by_file_name(
    spec_repo, make_spec_coder, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BEH-18: a missing or unparsable set is a refusal naming the file —
    never an attempt to recreate it."""
    monkeypatch.chdir(spec_repo.repo)
    (spec_repo.repo / "spec" / "tasks.md").unlink()
    planner = RecordingPlanner()

    rc = cli.main(["run"], planner=planner, coder=make_spec_coder())

    assert rc == 1
    assert "tasks.md" in capsys.readouterr().err
    assert planner.requests == []  # no attempt to "recreate" the set
    assert spec_repo.commit_count() == 1  # nothing was executed


def test_run_with_unknown_task_id_is_refused(
    spec_repo, make_spec_coder, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(spec_repo.repo)

    rc = cli.main(["run", "--task", "TASK-999"], coder=make_spec_coder())

    assert rc == 1
    assert "TASK-999" in capsys.readouterr().err


def test_run_outside_git_tree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D-13: commits go to the current branch, so ``run`` needs a work tree."""
    from conftest import SPEC_DESIGN, SPEC_REQUIREMENTS, SPEC_TASKS

    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "requirements.md").write_text(SPEC_REQUIREMENTS, encoding="utf-8")
    (spec / "design.md").write_text(SPEC_DESIGN, encoding="utf-8")
    (spec / "tasks.md").write_text(SPEC_TASKS.format(py="python3"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["run"])

    assert rc == 1
    assert "git work tree" in capsys.readouterr().err

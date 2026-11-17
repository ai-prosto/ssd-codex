"""BEH-05 / BEH-06 / BEH-07: the ``spec`` subcommand of the sdd-codex CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdd_codex import cli
from sdd_codex.errors import PlannerError
from sdd_codex.planner import (
    CodexCliPlanner,
    PlannerRequest,
    SpecFiles,
    split_planner_output,
)
from sdd_codex.specset import load_spec_set

VALID_REQUIREMENTS = """\
# demo — requirements

### REQ-001: Greet the user
The program prints a greeting.
"""

VALID_DESIGN = """\
# demo — design

### DESIGN-001: One script
A single Python script implements REQ-001.
"""

VALID_TASKS = """\
## Milestone 1: demo

### TASK-001: Print a greeting
P2 | TODO   Est: 0.5d

Implement the greeting.

**Checklist:**
- [ ] write the code

**Traces to:** [REQ-001], [DESIGN-001]
**Touches:** src/demo.py, tests/test_demo.py
**Verifies:** tests/test_demo.py::test_greets
"""

BROKEN_TASKS = """\
### TASK-ONE: malformed header, not TASK-NNN
P2 | TODO

**Checklist:**
- [ ] never valid
"""


class StubPlanner:
    """Records every request and answers with canned file contents."""

    def __init__(self, files: SpecFiles | None = None) -> None:
        self.requests: list[PlannerRequest] = []
        self.files = files or SpecFiles(
            requirements=VALID_REQUIREMENTS, design=VALID_DESIGN, tasks=VALID_TASKS
        )

    def __call__(self, request: PlannerRequest) -> SpecFiles:
        self.requests.append(request)
        return self.files


def test_description_from_argument_or_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BEH-05: positional argument and --from-file hand the planner the same
    description text; no description and no file is a refusal with a hint."""
    monkeypatch.chdir(tmp_path)
    description = "A tool that greets.\nIt must be polite."
    planner = StubPlanner()

    assert cli.main(["spec", description], planner=planner) == 0

    source = tmp_path / "idea.md"
    source.write_text(description + "\n", encoding="utf-8")
    assert cli.main(["spec", "--from-file", str(source)], planner=planner) == 0

    assert len(planner.requests) == 2
    first, second = planner.requests
    assert first.description == description
    assert second.description == first.description
    assert second.prompt == first.prompt

    # Neither a description nor a file: refusal with a hint, nothing planned.
    assert cli.main(["spec"], planner=planner) == 2
    assert "--from-file" in capsys.readouterr().err
    assert len(planner.requests) == 2

    # Both at once is ambiguous and refused as well.
    assert cli.main(["spec", description, "--from-file", str(source)]) == 2
    assert "not both" in capsys.readouterr().err


def test_writes_exactly_three_files_in_spec_runner_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BEH-06: exactly three files appear in paths.spec_dir and all three
    parse cleanly; a format mismatch is a command error, not a silent write."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sdd-codex.config.yaml").write_text(
        "paths:\n  spec_dir: myspec\n", encoding="utf-8"
    )

    assert cli.main(["spec", "a greeting tool"], planner=StubPlanner()) == 0
    spec_dir = tmp_path / "myspec"
    written = sorted(path.name for path in spec_dir.iterdir())
    assert written == ["design.md", "requirements.md", "tasks.md"]

    spec_set = load_spec_set(spec_dir)  # raises on any format issue
    assert [task.id for task in spec_set.tasks.tasks] == ["TASK-001"]
    assert "REQ-001" in spec_set.requirements.blocks
    assert "DESIGN-001" in spec_set.design.blocks

    # A planner answer that does not conform to the spec-runner format is a
    # command error and nothing is written.
    bad = StubPlanner(
        SpecFiles(
            requirements=VALID_REQUIREMENTS, design=VALID_DESIGN, tasks=BROKEN_TASKS
        )
    )
    (tmp_path / "sdd-codex.config.yaml").write_text(
        "paths:\n  spec_dir: otherspec\n", encoding="utf-8"
    )
    capsys.readouterr()
    assert cli.main(["spec", "a greeting tool"], planner=bad) == 1
    err = capsys.readouterr().err
    assert "tasks.md" in err
    assert "malformed task header" in err
    assert not (tmp_path / "otherspec").exists()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_spec_command_does_not_execute_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """BEH-07: spec and run are separate subcommands; after ``spec`` no agent
    and no verify command ran, no commit was made — only the files exist."""
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    monkeypatch.chdir(repo)

    # The CLI exposes two independent subcommands with distinct handlers.
    parser = cli.build_parser()
    assert parser.parse_args(["spec", "x"]).handler is cli._cmd_spec
    assert parser.parse_args(["run"]).handler is cli._cmd_run

    planner = StubPlanner()
    run_calls: list[object] = []
    with monkeypatch.context() as patch:
        # No subprocess may start during ``spec``: no coder agent, no verify
        # command, no git invocation.
        def no_subprocess(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"subprocess started during spec: {args!r}")

        patch.setattr(subprocess, "Popen", no_subprocess)
        patch.setattr(subprocess, "run", no_subprocess)
        patch.setattr(
            cli,
            "_cmd_run",
            lambda *args, **kwargs: run_calls.append(args) or 0,
        )
        assert cli.main(["spec", "a greeting tool"], planner=planner) == 0

    assert run_calls == []  # the run handler was never entered
    assert len(planner.requests) == 1

    # No commit was made and the only change is the untracked spec directory.
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"
    status = _git(repo, "status", "--porcelain").splitlines()
    assert status == ["?? spec/"]


def test_run_subcommand_parses_set_but_refuses_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["spec", "a greeting tool"], planner=StubPlanner()) == 0

    assert cli.main(["run"]) == 1
    assert "not implemented" in capsys.readouterr().err

    # A missing set is a refusal naming the file, never a planning attempt.
    (tmp_path / "spec" / "tasks.md").unlink()
    assert cli.main(["run"]) == 1
    assert "tasks.md" in capsys.readouterr().err


def test_validate_subcommand_reports_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["spec", "a greeting tool"], planner=StubPlanner()) == 0
    assert cli.main(["validate"]) == 0
    assert "valid" in capsys.readouterr().out

    (tmp_path / "spec" / "tasks.md").write_text(BROKEN_TASKS, encoding="utf-8")
    assert cli.main(["validate"]) == 1
    assert "malformed task header" in capsys.readouterr().err


def test_doctor_reports_missing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)  # not a git repository
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "codex" in out
    assert "OPENAI_API_KEY" in out
    assert "git" in out

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (tmp_path / ".git").mkdir()
    assert cli.main(["doctor"]) == 0


def test_codex_cli_planner_runs_codex_and_splits_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PlannerRequest(
        description="d", prompt="p", model="gpt-test", reasoning_effort="medium"
    )

    monkeypatch.setattr("sdd_codex.planner.codex.shutil.which", lambda name: None)
    with pytest.raises(PlannerError, match="not found in PATH"):
        CodexCliPlanner()(request)

    monkeypatch.setattr(
        "sdd_codex.planner.codex.shutil.which", lambda name: "/usr/bin/codex"
    )
    commands: list[list[str]] = []
    output = (
        "=== FILE: requirements.md ===\n"
        + VALID_REQUIREMENTS
        + "=== FILE: design.md ===\n"
        + VALID_DESIGN
        + "=== FILE: tasks.md ===\n"
        + VALID_TASKS
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", fake_run)
    files = CodexCliPlanner()(request)
    assert files.tasks == VALID_TASKS
    assert commands[0][:2] == ["codex", "exec"]
    assert "gpt-test" in commands[0]
    assert commands[0][-1] == "p"

    def failing_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=3, stdout="", stderr="boom")

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", failing_run)
    with pytest.raises(PlannerError, match="code 3"):
        CodexCliPlanner()(request)

    with pytest.raises(PlannerError, match="missing section"):
        split_planner_output("=== FILE: requirements.md ===\nonly one\n")

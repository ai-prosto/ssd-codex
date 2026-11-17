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
    build_planner_prompt,
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


def test_run_subcommand_refusals_name_the_problem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["spec", "a greeting tool"], planner=StubPlanner()) == 0

    # A parseable set outside a git work tree is a refusal (D-13).
    assert cli.main(["run"]) == 1
    assert "git work tree" in capsys.readouterr().err

    # A missing set is a refusal naming the file, never a planning attempt.
    # (The work-tree refusal comes first: the spec path is anchored at the
    # repo root, so the root must exist before the set can be looked up.)
    _git(tmp_path, "init", "-q")
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

    run_kwargs: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        run_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", fake_run)
    files = CodexCliPlanner(timeout_sec=123)(request)
    assert files.tasks == VALID_TASKS
    assert commands[0][:2] == ["codex", "exec"]
    assert commands[0][commands[0].index("--model") + 1] == "gpt-test"
    assert "model_reasoning_effort=medium" in commands[0]
    assert commands[0][-1] == "p"
    # The prompt must be the only input channel and the timeout must reach
    # the subprocess, or a hung codex would stall the command forever.
    assert run_kwargs[0]["timeout"] == 123
    assert run_kwargs[0]["stdin"] is subprocess.DEVNULL
    assert run_kwargs[0]["encoding"] == "utf-8"

    def failing_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=3, stdout="", stderr="boom")

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", failing_run)
    with pytest.raises(PlannerError, match="code 3"):
        CodexCliPlanner()(request)

    with pytest.raises(PlannerError, match="missing section"):
        split_planner_output("=== FILE: requirements.md ===\nonly one\n")


def test_codex_cli_planner_timeout_and_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PlannerRequest(
        description="d", prompt="p", model="m", reasoning_effort="low"
    )
    monkeypatch.setattr(
        "sdd_codex.planner.codex.shutil.which", lambda name: "/usr/bin/codex"
    )

    def timeout_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", timeout_run)
    with pytest.raises(PlannerError, match="timed out after 7s"):
        CodexCliPlanner(timeout_sec=7)(request)

    def exec_failure_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise OSError("exec failed")

    monkeypatch.setattr("sdd_codex.planner.codex.subprocess.run", exec_failure_run)
    with pytest.raises(PlannerError, match="cannot run 'codex'"):
        CodexCliPlanner()(request)


def test_build_planner_prompt_contains_contract_templates_and_description() -> None:
    prompt = build_planner_prompt("WIDGET FROBNICATOR")
    # The output contract (the FILE markers) is specified in the system prompt.
    assert "=== FILE: requirements.md ===" in prompt
    for template in (
        "requirements.template.md",
        "design.template.md",
        "tasks.template.md",
    ):
        assert f"=== TEMPLATE: {template} ===" in prompt
    assert "### REQ-001" in prompt
    assert "### DESIGN-001" in prompt
    assert "### TASK-001" in prompt
    assert prompt.rstrip().endswith("WIDGET FROBNICATOR")


def test_split_planner_output_ignores_preamble_and_rejects_duplicates() -> None:
    preamble = "codex session 0123\nthinking tokens: 42\n"
    output = (
        preamble
        + "=== FILE: requirements.md ===\n"
        + VALID_REQUIREMENTS
        + "=== FILE: design.md ===\n"
        + VALID_DESIGN
        + "=== FILE: tasks.md ===\n"
        + VALID_TASKS
    )
    files = split_planner_output(output)
    assert files.requirements == VALID_REQUIREMENTS
    assert files.design == VALID_DESIGN
    assert files.tasks == VALID_TASKS

    with pytest.raises(PlannerError, match="more than once"):
        split_planner_output(output + "=== FILE: tasks.md ===\nsecond copy\n")


def test_empty_requirements_or_design_is_a_command_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BEH-06: a block-less document is a format error, not a silent write."""
    monkeypatch.chdir(tmp_path)
    empty_requirements = StubPlanner(
        SpecFiles(requirements="\n", design=VALID_DESIGN, tasks=VALID_TASKS)
    )
    assert cli.main(["spec", "x"], planner=empty_requirements) == 1
    assert "no '### REQ-NNN: name' blocks found" in capsys.readouterr().err
    assert not (tmp_path / "spec").exists()

    empty_design = StubPlanner(
        SpecFiles(requirements=VALID_REQUIREMENTS, design="", tasks=VALID_TASKS)
    )
    assert cli.main(["spec", "x"], planner=empty_design) == 1
    assert "no '### DESIGN-NNN: name' blocks found" in capsys.readouterr().err
    assert not (tmp_path / "spec").exists()


def test_explicit_config_flag_beats_default_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "configs" / "custom.yaml"
    custom.parent.mkdir()
    custom.write_text("paths:\n  spec_dir: fromflag\n", encoding="utf-8")
    assert cli.main(["spec", "x", "--config", str(custom)], planner=StubPlanner()) == 0
    assert (tmp_path / "fromflag" / "tasks.md").is_file()

    capsys.readouterr()
    missing = str(tmp_path / "missing.yaml")
    assert cli.main(["spec", "x", "--config", missing], planner=StubPlanner()) == 1
    assert "config file not found" in capsys.readouterr().err


def test_spec_builds_codex_planner_with_configured_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sdd-codex.config.yaml").write_text(
        "run:\n  attempt_timeout_sec: 123\n", encoding="utf-8"
    )
    stub = StubPlanner()
    created: list[int] = []

    def factory(timeout_sec: int) -> StubPlanner:
        created.append(timeout_sec)
        return stub

    monkeypatch.setattr(cli, "CodexCliPlanner", factory)
    assert cli.main(["spec", "x"]) == 0
    assert created == [123]
    assert len(stub.requests) == 1


def test_from_file_unreadable_and_blank_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    planner = StubPlanner()
    absent = str(tmp_path / "absent.md")
    assert cli.main(["spec", "--from-file", absent], planner=planner) == 1
    assert "cannot read --from-file" in capsys.readouterr().err

    assert cli.main(["spec", "   \n\t "], planner=planner) == 2
    assert "description is required" in capsys.readouterr().err
    assert planner.requests == []


def test_doctor_reports_broken_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (tmp_path / ".git").mkdir()
    (tmp_path / "sdd-codex.config.yaml").write_text(
        "paths: [not, a, mapping\n", encoding="utf-8"
    )
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "loads" in out


def test_keyboard_interrupt_exits_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def interrupt(request: PlannerRequest) -> SpecFiles:
        raise KeyboardInterrupt

    assert cli.main(["spec", "x"], planner=interrupt) == 130
    assert "interrupted" in capsys.readouterr().err


def test_planner_prompt_mandates_uv_and_forbids_backticks() -> None:
    prompt = build_planner_prompt("anything")
    assert "uv run pytest" in prompt
    assert "backtick" in prompt

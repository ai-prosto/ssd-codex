"""The default Codex CLI coder agent: prompt, subprocess call, usage."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from sdd_codex.budget import Usage
from sdd_codex.coder import CodexCliCoder, coder_prompt
from sdd_codex.errors import CoderError
from sdd_codex.format.tasks import Task
from sdd_codex.task_loop import STEP_CODE, STEP_WRITE_TEST, CoderRequest


def _task() -> Task:
    return Task(
        id="TASK-101",
        name="Demo task",
        file="tasks.md",
        line=1,
        position=0,
        description="Make the demo output file.",
        touches=["src/"],
        test_command="python tests/check.py",
    )


def _request(step: str, **kwargs) -> CoderRequest:
    return CoderRequest(
        task=_task(),
        step=step,
        attempt=1,
        allowed_files=("src", "tests/check.py"),
        verify_command="python tests/check.py",
        **kwargs,
    )


def test_coder_prompt_differs_per_step_and_carries_context() -> None:
    write_test = coder_prompt(_request(STEP_WRITE_TEST))
    assert "must fail" in write_test  # the red run is the proof (D-09)
    assert "TASK-101: Demo task" in write_test
    assert "src, tests/check.py" in write_test
    assert "python tests/check.py" in write_test

    code = coder_prompt(
        _request(
            STEP_CODE,
            red_output="AssertionError: boom",
            review_comments=("fix the name",),
        )
    )
    assert "frozen" in code  # the test files may not be edited (D-09)
    assert "AssertionError: boom" in code
    assert "fix the name" in code  # BEH-17: comments reach the next attempt


def test_codex_cli_coder_spawns_exec_with_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout="done\ntokens used: 1,234\n", stderr=""
        )

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    coder = CodexCliCoder("/work/repo", model="coder-model", reasoning_effort="high")

    coder(_request(STEP_WRITE_TEST))

    ((command, kwargs),) = calls
    assert command[:4] == ["codex", "exec", "--cd", "/work/repo"]
    assert command[4:6] == ["--model", "coder-model"]
    assert "workspace-write" in command  # the coder edits the tree
    assert "model_reasoning_effort=high" in command
    assert command[-1] == "-"  # the prompt comes through stdin, not argv
    assert "TASK-101: Demo task" in kwargs["input"]
    # The reported spend is drained once and then cleared (D-15).
    assert coder.take_usage() == [Usage(model="coder-model", output_tokens=1234)]
    assert coder.take_usage() == []


def test_codex_cli_coder_failures_raise_coder_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(CoderError, match="not found in PATH"):
        CodexCliCoder("/work/repo", model="m")(_request(STEP_WRITE_TEST))

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def failing_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(CoderError, match="exited with code 2: boom"):
        CodexCliCoder("/work/repo", model="m")(_request(STEP_WRITE_TEST))

    def timing_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timing_out)
    with pytest.raises(CoderError, match="timed out after 5s"):
        CodexCliCoder("/work/repo", model="m", timeout_sec=5)(
            _request(STEP_WRITE_TEST)
        )

    def unrunnable(command, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", unrunnable)
    with pytest.raises(CoderError, match="cannot run"):
        CodexCliCoder("/work/repo", model="m")(_request(STEP_WRITE_TEST))


def test_coder_transcript_and_trailing_token_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coder hands the prompt and the agent's reply to the attempt log
    through ``take_transcript`` (BEH-24); the spend comes from the stderr
    summary line, not from prose that merely mentions tokens."""

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="I think tokens used: 5 is fine\ndone\n",
            stderr="work summary\ntokens used: 2,000\n",
        )

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    coder = CodexCliCoder("/work/repo", model="m")

    coder(_request(STEP_WRITE_TEST))

    # stderr's trailing summary wins over the stdout prose mention.
    assert coder.take_usage() == [Usage(model="m", output_tokens=2000)]
    transcript = coder.take_transcript()
    assert any(
        part.startswith("--- prompt") and "TASK-101: Demo task" in part
        for part in transcript
    )
    assert any(
        part.startswith("--- agent output") and "done" in part
        for part in transcript
    )
    assert coder.take_transcript() == []  # drained once, then cleared

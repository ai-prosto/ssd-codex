"""BEH-08: the verify command of a task comes from its own record."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from sdd_codex.config import VerifyConfig
from sdd_codex.errors import TaskRejected
from sdd_codex.format.tasks import Task
from sdd_codex.verify import command_files, run_verify, verify_command

PY = shlex.quote(sys.executable)


def make_task(**kwargs: object) -> Task:
    return Task(id="TASK-042", name="demo", file="tasks.md", line=1, position=0, **kwargs)  # type: ignore[arg-type]


def test_command_comes_from_task_record() -> None:
    """BEH-08: ``**Test:**`` is used verbatim, ``**Verifies:**`` selectors go
    through the config template, a task with neither is rejected by id."""
    config = VerifyConfig(selector_runner="uv run pytest {selectors} -q")

    explicit = make_task(test_command="make check")
    assert verify_command(explicit, config) == "make check"

    selectors = make_task(verifies=["tests/test_a.py::t_one", "tests/test_b.py::t_two"])
    assert (
        verify_command(selectors, config)
        == "uv run pytest tests/test_a.py::t_one tests/test_b.py::t_two -q"
    )

    # No Test and no Verifies: the task is rejected with its id; the config's
    # generic template is never substituted on its own.
    neither = make_task()
    with pytest.raises(TaskRejected) as excinfo:
        verify_command(neither, config)
    assert excinfo.value.task_id == "TASK-042"
    assert "TASK-042" in str(excinfo.value)


def test_test_files_come_from_selectors_and_command_tokens() -> None:
    task = make_task(
        test_command=f"{PY} tests/check.py -q --cov=src",
        verifies=["tests/test_a.py::t_one", "tests/test_a.py::t_two"],
    )
    # Selector file parts are deduplicated; flags and the absolute
    # interpreter path are never treated as repository test files.
    assert command_files(task) == ["tests/test_a.py", "tests/check.py"]
    assert command_files(make_task(test_command="make check")) == []


def test_run_verify_captures_output_and_exit_code(tmp_path: Path) -> None:
    passing = run_verify(f"{PY} -c \"print('fine')\"", tmp_path, timeout_sec=30)
    assert passing.passed
    assert passing.returncode == 0
    assert "fine" in passing.output

    failing = run_verify(
        f"{PY} -c \"import sys; print('boom'); sys.exit(3)\"", tmp_path, 30
    )
    assert not failing.passed
    assert failing.returncode == 3
    assert "boom" in failing.output


def test_run_verify_missing_binary_and_timeout(tmp_path: Path) -> None:
    missing = run_verify("./does-not-exist --flag", tmp_path, timeout_sec=30)
    assert not missing.passed
    assert "does-not-exist" in missing.output

    slow = run_verify(f'{PY} -c "import time; time.sleep(30)"', tmp_path, 1)
    assert slow.timed_out
    assert not slow.passed
    assert "timed out after 1s" in slow.output

    empty = run_verify("   ", tmp_path, timeout_sec=1)
    assert not empty.passed
    assert "empty verify command" in empty.output

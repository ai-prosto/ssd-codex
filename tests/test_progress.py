"""BEH-22 / BEH-23: the terminal progress lines of a run."""

from __future__ import annotations

import io
import re

from sdd_codex.artifacts import RunArtifacts
from sdd_codex.progress import Progress
from sdd_codex.runner import run_tasks


class TtyStream(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def test_prints_task_start_line(spec_repo):
    """BEH-22: starting a task prints exactly one line with its id, its
    description and the start time."""
    stream = io.StringIO()
    progress = Progress(stream)
    task = spec_repo.spec_set().execution_order[0]

    progress.task_start(task)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1  # exactly one line per task start
    (line,) = lines
    assert task.id in line
    assert task.name in line
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)


def test_prints_line_per_attempt_with_step_and_result(
    spec_repo, make_spec_coder, tmp_path
):
    """BEH-23: under the task line every attempt gets its own line with the
    attempt number and the current step; once the verify command finished,
    its result lands in the same line. Without a TTY each step change is a
    separate line with the same facts; the agent's output stream never
    reaches the terminal."""
    stream = io.StringIO()
    coder = make_spec_coder()

    run_tasks(
        spec_repo.spec_set(),
        spec_repo.config,
        spec_repo.repo,
        coder,
        artifacts=RunArtifacts(tmp_path / "artifacts"),
        progress=Progress(stream),
        only_task="TASK-201",
    )

    lines = stream.getvalue().splitlines()
    attempt_lines = [line for line in lines if "attempt 01" in line]
    # Non-TTY: one separate line per step change, same set of facts.
    assert any("написание теста" in line for line in attempt_lines)
    assert any("кодинг" in line for line in attempt_lines)
    # The verify result lands in the same line as the step: the red run
    # fails before coding, the green run passes after it.
    assert any(
        "тест" in line and "fail (exit 1)" in line for line in attempt_lines
    )
    assert any(
        "тест" in line and "ok (exit 0)" in line for line in attempt_lines
    )
    # The agent/test output stream is not in the terminal.
    assert "missing or wrong" not in stream.getvalue()


def test_tty_redraws_the_attempt_line_in_place(spec_repo):
    """D-16: on a TTY the current attempt's line is redrawn on each step
    change instead of printing a new line."""
    stream = TtyStream()
    progress = Progress(stream)
    task = spec_repo.spec_set().execution_order[0]

    progress.task_start(task)
    progress.attempt_step(task.id, 1, "write_test")
    progress.attempt_step(task.id, 1, "red", "fail (exit 1)")
    progress.attempt_step(task.id, 2, "write_test")
    progress.close()

    out = stream.getvalue()
    assert "\r" in out  # the attempt line was redrawn in place
    # A new attempt starts a new line instead of redrawing the old one.
    assert out.count("\n") >= 2

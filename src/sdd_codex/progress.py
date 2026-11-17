"""Terminal progress: one line per task, one line per attempt.

Design decision D-16: when a task starts, exactly one line with its id,
description and start time is printed (BEH-22). Under it every attempt gets
its own line: the attempt number, the current step (``написание теста`` /
``кодинг`` / ``тест``) and, once the verify command finished, its result in
the same line (BEH-23). On a TTY the current attempt's line is redrawn on
every step change; without a TTY (log, CI) each step change is printed as a
separate line carrying the same set of facts. The agent's output stream
never reaches the terminal — it goes to the attempt log.
"""

from __future__ import annotations

import sys
import time
from typing import TextIO

from sdd_codex.format.tasks import Task
from sdd_codex.task_loop import (
    STEP_CODE,
    STEP_COMMIT,
    STEP_FREEZE,
    STEP_GREEN,
    STEP_RED,
    STEP_REVIEW,
    STEP_WRITE_TEST,
)

# The step labels of D-16; the red and green runs are both "тест" — the
# runner executing the task's verify command.
STEP_LABELS = {
    STEP_WRITE_TEST: "написание теста",
    STEP_CODE: "кодинг",
    STEP_RED: "тест",
    STEP_GREEN: "тест",
    STEP_FREEZE: "заморозка",
    STEP_REVIEW: "ревью",
    STEP_COMMIT: "коммит",
}


class Progress:
    """Prints run progress to a stream, TTY-aware."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        isatty = getattr(self.stream, "isatty", None)
        self.tty = bool(isatty()) if callable(isatty) else False
        self._line_open = False
        self._attempt_key: tuple[str, int] | None = None

    def _emit(self, text: str, rewrite: bool) -> None:
        if self.tty:
            if rewrite and self._line_open:
                # Redraw the current attempt's line in place.
                self.stream.write("\r\x1b[K" + text)
            else:
                if self._line_open:
                    self.stream.write("\n")
                self.stream.write(text)
            self._line_open = True
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def task_start(self, task: Task) -> None:
        """BEH-22: exactly one line — task id, description, start time."""
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        self._attempt_key = None
        self._emit(f"{task.id} · {task.name} · start {started}", rewrite=False)

    def attempt_step(
        self, task_id: str, attempt: int, step: str, result: str = ""
    ) -> None:
        """BEH-23: the attempt's line — number, step and, when the verify
        command has finished, its result in the same line."""
        label = STEP_LABELS.get(step, step)
        text = f"  attempt {attempt:02d} · {label}"
        if result:
            text += f" · {result}"
        key = (task_id, attempt)
        self._emit(text, rewrite=key == self._attempt_key)
        self._attempt_key = key

    def close(self) -> None:
        """Terminate the last redrawn line on a TTY."""
        if self.tty and self._line_open:
            self.stream.write("\n")
            self.stream.flush()
        self._line_open = False

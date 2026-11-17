"""The real coder agent: spawns the ``codex`` CLI for one attempt step.

The task loop (D-09) treats the coder as an injectable callable taking a
:class:`~sdd_codex.task_loop.CoderRequest`; this module provides the default
implementation that runs ``codex exec`` as a subprocess with workspace write
access. Each call is a fresh conversation; authorisation comes only from the
environment (D-04). The agent's output never reaches the terminal (D-16) —
the subprocess output is captured here and handed to the task loop through
:meth:`CodexCliCoder.take_transcript`, so ``attempt-NN.log`` keeps the
prompts and the agent's reply (BEH-24).
"""

from __future__ import annotations

import shutil
import subprocess

from sdd_codex.budget import Usage, parse_codex_tokens
from sdd_codex.errors import CoderError
from sdd_codex.review import task_record
from sdd_codex.task_loop import STEP_WRITE_TEST, CoderRequest

CODEX_BINARY = "codex"


def coder_prompt(request: CoderRequest) -> str:
    """The full prompt of one coder step."""
    if request.step == STEP_WRITE_TEST:
        instructions = (
            "Write the automated test(s) proving the task below is done. "
            "Do NOT implement the production code yet: the test must fail "
            "when run now, and the runner will prove that failure itself."
        )
    else:
        instructions = (
            "The failing test below is frozen: do not edit any test file. "
            "Write the production code that makes the verify command pass."
        )
    parts = [
        instructions,
        f"Only create or modify these paths: {', '.join(request.allowed_files)}.",
        f"The runner verifies with exactly: {request.verify_command}",
        f"## Task record\n{task_record(request.task)}",
    ]
    if request.red_output:
        parts.append(f"## Failing (red) test output\n{request.red_output}")
    if request.review_comments:
        parts.append(
            "## Review comments from the previous blocked attempt\n"
            + "\n".join(f"- {comment}" for comment in request.review_comments)
        )
    return "\n\n".join(parts) + "\n"


class CodexCliCoder:
    """Default coder: ``codex exec`` with write access to the repository.

    Usage is collected per call and handed to the budget tracker through
    :meth:`take_usage` (D-15).
    """

    def __init__(
        self,
        repo: str,
        model: str,
        reasoning_effort: str = "medium",
        timeout_sec: int = 1800,
    ) -> None:
        self.repo = str(repo)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_sec = timeout_sec
        self._usage: list[Usage] = []
        self._transcript: list[str] = []

    def take_usage(self) -> list[Usage]:
        """Return and clear the usage events accumulated since the last drain."""
        usage, self._usage = self._usage, []
        return usage

    def take_transcript(self) -> list[str]:
        """Return and clear the prompt/reply record since the last drain.

        BEH-24: the attempt log must hold the prompts and the agent's reply;
        the task loop drains this after every coder step and appends it to
        ``attempt-NN.log``.
        """
        transcript, self._transcript = self._transcript, []
        return transcript

    def __call__(self, request: CoderRequest) -> None:
        if shutil.which(CODEX_BINARY) is None:
            raise CoderError(
                f"'{CODEX_BINARY}' CLI not found in PATH; run 'sdd-codex doctor'"
            )
        prompt = coder_prompt(request)
        self._transcript.append(
            f"--- prompt ({request.step}, attempt {request.attempt:02d}) ---\n"
            + prompt
        )
        command = [
            CODEX_BINARY,
            "exec",
            "--cd",
            self.repo,
            "--model",
            self.model,
            "--sandbox",
            "workspace-write",
            "-c",
            f"model_reasoning_effort={self.reasoning_effort}",
            # "-" reads the prompt from stdin: it embeds test output and
            # review comments, which as an argv element would hit the OS
            # argument-size limit and show up in the process list.
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            self._transcript.append(
                f"--- agent output ---\n(timed out after {self.timeout_sec}s)"
            )
            raise CoderError(f"coder timed out after {self.timeout_sec}s") from exc
        except OSError as exc:
            raise CoderError(f"cannot run '{CODEX_BINARY}': {exc}") from exc
        self._transcript.append(f"--- agent output ---\n{result.stdout}")
        if result.stderr.strip():
            self._transcript.append(f"--- agent stderr ---\n{result.stderr}")
        # The spend summary goes to stderr; stdout (the agent's reply) is
        # only a fallback, as its prose may mention "tokens used" itself.
        total = parse_codex_tokens(result.stderr)
        if total is None:
            total = parse_codex_tokens(result.stdout)
        if total is not None:
            self._usage.append(Usage(model=self.model, output_tokens=total))
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CoderError(
                f"'{CODEX_BINARY}' exited with code {result.returncode}: {detail}"
            )

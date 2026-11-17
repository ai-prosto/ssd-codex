"""Default planner: runs ``codex`` as a CLI subprocess.

Design decision D-04: authorisation comes only from the environment —
``OPENAI_API_KEY`` or an already-configured ``codex`` session. The key is
never read from the config and never written anywhere; ``sdd-codex doctor``
reports a missing environment before the first paid call.
"""

from __future__ import annotations

import shutil
import subprocess

from sdd_codex.errors import PlannerError
from sdd_codex.planner.plan import PlannerRequest, SpecFiles, split_planner_output

CODEX_BINARY = "codex"


class CodexCliPlanner:
    """Planner that spawns the ``codex`` CLI and parses its final answer."""

    def __init__(self, timeout_sec: int = 1800) -> None:
        self.timeout_sec = timeout_sec

    def __call__(self, request: PlannerRequest) -> SpecFiles:
        if shutil.which(CODEX_BINARY) is None:
            raise PlannerError(
                f"'{CODEX_BINARY}' CLI not found in PATH; run 'sdd-codex doctor'"
            )
        command = [
            CODEX_BINARY,
            "exec",
            "--model",
            request.model,
            "-c",
            f"model_reasoning_effort={request.reasoning_effort}",
            request.prompt,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlannerError(
                f"planner timed out after {self.timeout_sec}s"
            ) from exc
        except OSError as exc:
            raise PlannerError(f"cannot run '{CODEX_BINARY}': {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise PlannerError(
                f"'{CODEX_BINARY}' exited with code {result.returncode}: {detail}"
            )
        return split_planner_output(result.stdout)

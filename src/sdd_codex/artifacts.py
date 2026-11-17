"""The run's artifact tree: per-task subdirectories, reports, run.jsonl.

Design decision D-17: every run gets ``<artifacts_dir>/<run-id>/`` with one
subdirectory per task. The task subdirectory holds ``attempt-NN.log``
(prompts, agent reply, command output), ``attempt-NN.red.txt`` and
``attempt-NN.review.json`` (BEH-24), plus ``report.md`` — the task's final
report: verify command, allowed files, step and failure reason of every
attempt, reviewer verdicts, the commit and the spend; it is written on
success and on failure alike (BEH-25). Next to the task directories live
``run.jsonl`` (one event per line) and ``summary.md``.

The OpenAI key is masked in everything written here: the environment is the
only place the key lives (D-04), so its value is simply replaced wherever an
agent or a verify command happened to echo it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

from sdd_codex.errors import TaskRejected
from sdd_codex.format.tasks import Task
from sdd_codex.freeze import allowed_paths
from sdd_codex.task_loop import AttemptResult

RUN_LOG_NAME = "run.jsonl"
SUMMARY_NAME = "summary.md"
REPORT_NAME = "report.md"
MASK = "***"


def mask_secrets(text: str, secrets: Sequence[str] | None = None) -> str:
    """Replace every secret value in ``text`` with ``***``.

    By default the only secret is the ``OPENAI_API_KEY`` environment value —
    the single credential sdd-codex ever handles (D-04).
    """
    if secrets is None:
        secrets = [os.environ.get("OPENAI_API_KEY") or ""]
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def _unique_run_id(parent: Path) -> str:
    base = time.strftime("%Y%m%d-%H%M%S")
    run_id = base
    counter = 1
    while (parent / run_id).exists():
        counter += 1
        run_id = f"{base}-{counter:02d}"
    return run_id


class RunArtifacts:
    """The artifact directory of one run."""

    def __init__(self, artifacts_dir: str | Path, run_id: str | None = None) -> None:
        parent = Path(artifacts_dir)
        parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or _unique_run_id(parent)
        self.root = parent / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def run_log(self) -> Path:
        return self.root / RUN_LOG_NAME

    @property
    def summary_path(self) -> Path:
        return self.root / SUMMARY_NAME

    def task_dir(self, task_id: str) -> Path:
        """BEH-24: each task gets its own subdirectory under the run."""
        directory = self.root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def append_event(self, **event: object) -> None:
        """Append one masked JSON line to ``run.jsonl``."""
        line = mask_secrets(json.dumps(event, ensure_ascii=False))
        with self.run_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_summary(self, text: str) -> Path:
        self.summary_path.write_text(mask_secrets(text), encoding="utf-8")
        return self.summary_path

    def mask_task_files(self, task_dir: str | Path) -> None:
        """Mask the key in the attempt artifacts the task loop wrote.

        The attempt log, the red output and the review verdict capture agent
        and command output verbatim; the key is scrubbed after every attempt
        so no artifact ever holds it longer than the attempt itself took.
        """
        for path in sorted(Path(task_dir).glob("attempt-*")):
            if not path.is_file():
                continue
            original = path.read_text(encoding="utf-8")
            masked = mask_secrets(original)
            if masked != original:
                path.write_text(masked, encoding="utf-8")


def write_task_report(
    task_dir: str | Path,
    task: Task,
    status: str,
    attempts: Sequence[AttemptResult],
    total_tokens: int = 0,
    total_usd: float = 0.0,
) -> Path:
    """BEH-25: the task's final report, written on success and failure alike."""
    lines = [f"# {task.id}: {task.name}", "", f"Status: {status}"]
    if attempts:
        lines.append(f"Verify command: `{attempts[0].verify_command}`")
    try:
        allowed = allowed_paths(task)
    except TaskRejected:
        allowed = ()
    if allowed:
        lines.append("Allowed files: " + ", ".join(allowed))
    lines += ["", "## Attempts", ""]
    for attempt in attempts:
        outcome = "accepted" if attempt.accepted else "failed"
        lines.append(
            f"- attempt {attempt.number:02d}: step {attempt.step_reached} — {outcome}"
        )
        if attempt.failure_reason:
            reason = attempt.failure_reason.replace("\n", "\n    ")
            lines.append(f"  - reason: {reason}")
        if attempt.review_verdict:
            lines.append(f"  - reviewer verdict: {attempt.review_verdict}")
            for comment in attempt.review_comments:
                lines.append(f"    - {comment}")
        if attempt.commit:
            lines.append(f"  - commit: {attempt.commit}")
    lines += ["", f"Spend: {total_tokens} tokens, ${total_usd:.4f}"]
    path = Path(task_dir) / REPORT_NAME
    path.write_text(mask_secrets("\n".join(lines) + "\n"), encoding="utf-8")
    return path

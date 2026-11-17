"""The whole-run orchestrator: tasks in order, retries, budget, artifacts.

Design decision D-14: every task gets at most ``run.max_retries`` attempts;
a failed test, a freeze violation, a reviewer ``block`` and a crashed agent
are equal failed attempts. After the retries are exhausted the *whole run*
stops: no following task is started, the summary marks them ``not started``
and the exit code is non-zero (BEH-19, BEH-20). There is no switch to turn
this stop off. Already committed tasks are marked ``DONE`` in ``tasks.md``
(D-18), so a re-run continues from where the previous run stopped.

Design decision D-15: the spend stop works the same way — when an enabled
``budget`` limit is exceeded the run stops with the reason ``budget``; with
the limits at ``null`` the run never stops on spend, but the accumulated
usage still lands in ``run.jsonl`` and ``summary.md`` (BEH-21).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sdd_codex.artifacts import RunArtifacts, write_task_report
from sdd_codex.budget import BudgetTracker, drain_usage
from sdd_codex.config import Config
from sdd_codex.errors import GitError, TaskRejected
from sdd_codex.format.tasks import Task
from sdd_codex.progress import Progress
from sdd_codex.review import ReviewerAgent
from sdd_codex.specset import SpecSet
from sdd_codex.task_loop import AttemptResult, CoderAgent, run_task_attempt

STOP_RETRIES = "retries"
STOP_BUDGET = "budget"
STOP_ERROR = "error"

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NOT_STARTED = "not started"
STATUS_SKIPPED = "already DONE"


@dataclass
class TaskRun:
    """What happened to one task within the run."""

    task: Task
    status: str = STATUS_NOT_STARTED
    attempts: list[AttemptResult] = field(default_factory=list)
    reason: str = ""


@dataclass
class RunReport:
    """The outcome of one whole run."""

    run_id: str
    run_dir: Path
    tasks: list[TaskRun]
    stop_reason: str = ""  # "" when every task ran; STOP_* otherwise
    stop_detail: str = ""
    total_tokens: int = 0
    total_usd: float = 0.0

    @property
    def exit_code(self) -> int:
        return 0 if not self.stop_reason else 1


def run_tasks(
    spec_set: SpecSet,
    config: Config,
    repo: str | Path,
    coder: CoderAgent,
    reviewer: ReviewerAgent | None = None,
    *,
    artifacts: RunArtifacts | None = None,
    progress: Progress | None = None,
    budget: BudgetTracker | None = None,
    only_task: str | None = None,
) -> RunReport:
    """Execute the spec set's tasks in dependency order; never raises on a
    failed or stopped run — the outcome is the :class:`RunReport`.

    ``only_task`` narrows the run to a single task id (the ``--task`` flag);
    the caller validates that the id exists.
    """
    repo = Path(repo)
    artifacts = artifacts or RunArtifacts(config.paths.artifacts_dir)
    progress = progress or Progress()
    budget = budget or BudgetTracker(config.budget)

    order = spec_set.execution_order
    if only_task is not None:
        order = [task for task in order if task.id == only_task]
    records = [TaskRun(task=task) for task in order]
    report = RunReport(run_id=artifacts.run_id, run_dir=artifacts.root, tasks=records)

    def stop(reason: str, detail: str) -> None:
        report.stop_reason = reason
        report.stop_detail = detail
        artifacts.append_event(event="stop", reason=reason, detail=detail)

    for record in records:
        task = record.task
        if task.status == "done":
            # D-18: the DONE mark in tasks.md is the resume point; a task
            # already committed stays "already DONE" even after a stop.
            record.status = STATUS_SKIPPED
            artifacts.append_event(event="task", task=task.id, status=STATUS_SKIPPED)
            continue
        if report.stop_reason:
            # BEH-20: after the stop no following task is started.
            record.status = STATUS_NOT_STARTED
            continue

        progress.task_start(task)
        task_dir = artifacts.task_dir(task.id)
        comments: tuple[str, ...] = ()
        for attempt in range(1, config.run.max_retries + 1):

            def on_step(
                step: str,
                detail: str,
                _task_id: str = task.id,
                _attempt: int = attempt,
            ) -> None:
                progress.attempt_step(_task_id, _attempt, step, detail)

            try:
                result = run_task_attempt(
                    task,
                    config,
                    repo,
                    coder,
                    task_dir,
                    attempt=attempt,
                    reviewer=reviewer,
                    review_comments=comments,
                    on_step=on_step,
                )
            except (TaskRejected, GitError) as exc:
                # A refusal, not a failed attempt: retrying cannot help, so
                # the run stops with the reason instead of burning retries.
                record.status = STATUS_FAILED
                record.reason = str(exc)
                stop(STOP_ERROR, str(exc))
                break
            record.attempts.append(result)
            drain_usage(budget, coder, reviewer)
            artifacts.append_event(
                event="attempt",
                task=task.id,
                attempt=attempt,
                step=result.step_reached,
                accepted=result.accepted,
                reason=result.failure_reason,
                commit=result.commit,
                total_tokens=budget.total_tokens,
                total_usd=budget.total_usd,
            )
            artifacts.mask_task_files(task_dir)
            if result.accepted:
                record.status = STATUS_DONE
                break
            record.reason = result.failure_reason
            if result.review_comments:
                # BEH-17: a blocked attempt's comments travel into the next.
                comments = result.review_comments
            budget_reason = budget.stop_reason()
            if budget_reason:
                record.status = STATUS_FAILED
                record.reason = budget_reason
                stop(STOP_BUDGET, budget_reason)
                break

        if record.status == STATUS_NOT_STARTED:
            # Every attempt ran and failed: the retries are exhausted and
            # the whole run stops (D-14); there is no switch for this stop.
            record.status = STATUS_FAILED
            stop(
                STOP_RETRIES,
                f"{task.id}: {config.run.max_retries} failed attempts "
                f"(run.max_retries)",
            )
        artifacts.append_event(
            event="task", task=task.id, status=record.status, reason=record.reason
        )
        write_task_report(
            task_dir,
            task,
            record.status,
            record.attempts,
            total_tokens=budget.total_tokens,
            total_usd=budget.total_usd,
            reason=record.reason,
        )
        if not report.stop_reason:
            budget_reason = budget.stop_reason()
            if budget_reason:
                stop(STOP_BUDGET, budget_reason)

    report.total_tokens = budget.total_tokens
    report.total_usd = budget.total_usd
    artifacts.append_event(
        event="run",
        stop_reason=report.stop_reason,
        stop_detail=report.stop_detail,
        total_tokens=report.total_tokens,
        total_usd=report.total_usd,
    )
    artifacts.write_summary(summary_markdown(report))
    progress.close()
    return report


def summary_markdown(report: RunReport) -> str:
    """``summary.md``: one line per task, the stop reason and the spend."""
    lines = [f"# Run {report.run_id}", ""]
    for record in report.tasks:
        line = f"- {record.task.id}: {record.status}"
        if record.reason:
            line += f" — {record.reason.splitlines()[0]}"
        lines.append(line)
    lines.append("")
    if report.stop_reason:
        lines.append(f"Stopped: {report.stop_reason} — {report.stop_detail}")
    lines.append(
        f"Spend: {report.total_tokens} tokens, ${report.total_usd:.4f}"
    )
    return "\n".join(lines) + "\n"

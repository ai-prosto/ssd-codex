"""BEH-19 / BEH-20: retries exhaust → the whole run stops, nothing follows."""

from __future__ import annotations

import io
from dataclasses import dataclass, replace

from sdd_codex.artifacts import RunArtifacts
from sdd_codex.config import RunConfig
from sdd_codex.progress import Progress
from sdd_codex.runner import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_STARTED,
    STATUS_SKIPPED,
    STOP_ERROR,
    STOP_RETRIES,
    run_tasks,
)


def _run(spec_repo, coder, tmp_path, config=None, reviewer=None):
    return run_tasks(
        spec_repo.spec_set(),
        config or spec_repo.config,
        spec_repo.repo,
        coder,
        reviewer,
        artifacts=RunArtifacts(tmp_path / "artifacts"),
        progress=Progress(io.StringIO()),
    )


def test_stops_whole_run_after_max_retries(spec_repo, make_spec_coder, tmp_path):
    """BEH-19: a task failing ``run.max_retries`` times gets exactly N
    attempts — a crashed agent counts like a failed test — and then the
    runner stops the whole run."""
    config = replace(spec_repo.config, run=RunConfig(max_retries=2))
    coder = make_spec_coder(crash=frozenset({"TASK-201"}))

    report = _run(spec_repo, coder, tmp_path, config=config)

    first = report.tasks[0]
    assert first.task.id == "TASK-201"
    assert len(first.attempts) == 2  # exactly N attempts, not one more
    assert [c for c in coder.calls if c[0] == "TASK-201"] == [
        ("TASK-201", "write_test", 1),
        ("TASK-201", "write_test", 2),
    ]
    assert first.status == STATUS_FAILED
    assert "agent crashed" in first.reason
    assert report.stop_reason == STOP_RETRIES
    assert "max_retries" in report.stop_detail
    assert report.exit_code != 0


def test_following_tasks_not_started_after_stop(spec_repo, make_spec_coder, tmp_path):
    """BEH-20: after the retry stop no following task starts; the summary
    marks it ``not started`` and the exit code is non-zero."""
    config = replace(spec_repo.config, run=RunConfig(max_retries=1))
    coder = make_spec_coder(crash=frozenset({"TASK-201"}))

    report = _run(spec_repo, coder, tmp_path, config=config)

    assert report.stop_reason == STOP_RETRIES
    assert report.exit_code != 0
    second = report.tasks[1]
    assert second.task.id == "TASK-202"
    assert second.status == STATUS_NOT_STARTED
    assert second.attempts == []
    assert all(call[0] != "TASK-202" for call in coder.calls)
    summary = (report.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "TASK-202: not started" in summary
    assert spec_repo.commit_count() == 1  # nothing was committed


def test_refusal_stops_run_with_error_reason(spec_repo, make_spec_coder, tmp_path):
    """A refusal (here: a dirty tree) is not a failed attempt — retrying
    cannot help, so the run stops with the reason ``error`` without burning
    retries, and the task's report still names the reason."""
    (spec_repo.repo / "foreign.txt").write_text("uncommitted\n", encoding="utf-8")
    coder = make_spec_coder()

    report = _run(spec_repo, coder, tmp_path)

    first = report.tasks[0]
    assert first.status == STATUS_FAILED
    assert first.attempts == []  # a refusal, not an attempt
    assert "dirty" in first.reason
    assert report.stop_reason == STOP_ERROR
    assert report.exit_code != 0
    assert coder.calls == []  # the coder never ran
    report_md = (report.run_dir / "TASK-201" / "report.md").read_text("utf-8")
    assert "Status: failed" in report_md
    assert "dirty" in report_md  # BEH-25: the reason reaches the report
    summary = (report.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "TASK-201: failed" in summary
    assert "TASK-202: not started" in summary


@dataclass
class _BlockFirstReviewer:
    """Blocks the very first review with a comment, then approves."""

    calls: int = 0

    def __call__(self, request) -> str:
        self.calls += 1
        if self.calls == 1:
            return '{"verdict": "block", "comments": ["rename the variable"]}'
        return '{"verdict": "commit", "comments": []}'


def test_blocked_review_counts_as_retry_and_comments_travel(
    spec_repo, make_spec_coder, tmp_path
):
    """BEH-17 through the runner: a reviewer ``block`` closes the attempt
    like a failed test, and its comments reach every coder prompt of the
    next attempt."""
    config = replace(spec_repo.config, run=RunConfig(max_retries=2))
    coder = make_spec_coder()

    report = _run(
        spec_repo, coder, tmp_path, config=config, reviewer=_BlockFirstReviewer()
    )

    first = report.tasks[0]
    assert first.status == STATUS_DONE
    assert len(first.attempts) == 2  # the blocked attempt burned a retry
    assert first.attempts[0].review_verdict == "block"
    assert first.attempts[1].accepted
    second_attempt = [
        request
        for request in coder.requests
        if request.task.id == "TASK-201" and request.attempt == 2
    ]
    assert second_attempt
    assert all(
        "rename the variable" in request.review_comments
        for request in second_attempt
    )
    assert report.stop_reason == ""


def test_done_task_after_stop_stays_skipped(spec_repo, make_spec_coder, tmp_path):
    """A later task already marked DONE is reported ``already DONE`` even
    after a stop: the stop only blocks *starting* tasks, it does not undo
    the D-18 resume point."""
    tasks_md = spec_repo.repo / "spec" / "tasks.md"
    head, tail = tasks_md.read_text(encoding="utf-8").split("### TASK-202", 1)
    tasks_md.write_text(
        head + "### TASK-202" + tail.replace("P2 | TODO", "P2 | DONE", 1),
        encoding="utf-8",
    )
    spec_repo.git("commit", "-aqm", "mark TASK-202 done")
    config = replace(spec_repo.config, run=RunConfig(max_retries=1))
    coder = make_spec_coder(crash=frozenset({"TASK-201"}))

    report = _run(spec_repo, coder, tmp_path, config=config)

    assert report.stop_reason == STOP_RETRIES
    assert report.tasks[1].status == STATUS_SKIPPED
    summary = (report.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "TASK-202: already DONE" in summary


def test_rerun_continues_from_done_mark(spec_repo, make_spec_coder, tmp_path):
    """BEH-20 / D-18: tasks already marked DONE in tasks.md are skipped, so
    a re-run continues from where the previous run stopped."""
    tasks_md = spec_repo.repo / "spec" / "tasks.md"
    text = tasks_md.read_text(encoding="utf-8")
    tasks_md.write_text(text.replace("P2 | TODO", "P2 | DONE", 1), encoding="utf-8")
    spec_repo.git("commit", "-aqm", "mark TASK-201 done")
    coder = make_spec_coder()

    report = _run(spec_repo, coder, tmp_path)

    assert report.tasks[0].status == STATUS_SKIPPED
    assert report.tasks[0].attempts == []
    assert all(call[0] != "TASK-201" for call in coder.calls)
    assert report.tasks[1].status == STATUS_DONE
    assert report.stop_reason == ""
    assert report.exit_code == 0

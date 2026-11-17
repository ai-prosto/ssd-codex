"""BEH-24 / BEH-25: the per-task artifact directory and the final report."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from conftest import StaticReviewer

from sdd_codex.artifacts import RunArtifacts
from sdd_codex.config import RunConfig
from sdd_codex.progress import Progress
from sdd_codex.runner import run_tasks


def _run(spec_repo, coder, tmp_path, config=None, reviewer=None, **kwargs):
    return run_tasks(
        spec_repo.spec_set(),
        config or spec_repo.config,
        spec_repo.repo,
        coder,
        reviewer,
        artifacts=RunArtifacts(tmp_path / "artifacts"),
        progress=Progress(io.StringIO()),
        **kwargs,
    )


def test_per_task_dir_with_log_per_attempt(
    spec_repo, make_spec_coder, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """BEH-24: ``<artifacts_dir>/<run-id>/<TASK-id>/`` holds attempt-NN.log
    per attempt, next to attempt-NN.red.txt and attempt-NN.review.json; the
    OpenAI key is masked in the artifacts."""
    secret = "sk-test-secret-123"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = replace(spec_repo.config, run=RunConfig(max_retries=2))
    # Attempt 1 writes the wrong output (green run fails); attempt 2 passes.
    coder = make_spec_coder(wrong_until={"TASK-201": 2})
    reviewer = StaticReviewer()

    report = _run(
        spec_repo, coder, tmp_path, config=config, reviewer=reviewer,
        only_task="TASK-201",
    )

    assert report.exit_code == 0
    task_dir = report.run_dir / "TASK-201"
    assert task_dir.is_dir()
    assert (task_dir / "attempt-01.log").is_file()  # the failed attempt
    assert (task_dir / "attempt-02.log").is_file()  # the accepted attempt
    assert (task_dir / "attempt-01.red.txt").is_file()
    assert (task_dir / "attempt-02.red.txt").is_file()
    assert (task_dir / "attempt-02.review.json").is_file()

    # The check script echoed OPENAI_API_KEY on failure; the artifacts must
    # carry the mask, never the key itself.
    contents = {
        path: path.read_text(encoding="utf-8")
        for path in report.run_dir.rglob("*")
        if path.is_file()
    }
    assert all(secret not in text for text in contents.values())
    assert "***" in contents[task_dir / "attempt-01.red.txt"]


def test_task_report_written_on_success_and_failure(
    spec_repo, make_spec_coder, tmp_path
):
    """BEH-25: report.md lands in the task's subdirectory with the verify
    command, the allowed files, every attempt's step and failure reason, the
    reviewer verdicts, the commit and the spend — on success and on failure
    alike."""
    config = replace(spec_repo.config, run=RunConfig(max_retries=2))
    coder = make_spec_coder(crash=frozenset({"TASK-202"}), usage_per_call=100)
    reviewer = StaticReviewer()

    report = _run(spec_repo, coder, tmp_path, config=config, reviewer=reviewer)

    done = (report.run_dir / "TASK-201" / "report.md").read_text(encoding="utf-8")
    assert "Status: done" in done
    assert "tests/check_201.py" in done  # the verify command
    assert "Allowed files: src" in done
    assert "attempt 01" in done and "accepted" in done
    assert "reviewer verdict: commit" in done
    assert report.tasks[0].attempts[0].commit[:12] in done
    assert "tokens" in done  # the spend line

    failed = (report.run_dir / "TASK-202" / "report.md").read_text(encoding="utf-8")
    assert "Status: failed" in failed
    assert "attempt 01" in failed and "attempt 02" in failed
    assert "step write_test" in failed
    assert "agent crashed" in failed  # the failure reason per attempt
    assert "tokens" in failed


def test_mask_task_files_survives_non_utf8_bytes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Masking works on raw bytes: a non-UTF-8 byte in one attempt file must
    not abort the pass and leave the key readable in files globbed after
    it."""
    secret = "sk-mask-me-456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    artifacts = RunArtifacts(tmp_path / "artifacts")
    task_dir = artifacts.task_dir("TASK-201")
    binary = task_dir / "attempt-01.log"
    binary.write_bytes(b"\xff\xfe raw " + secret.encode("utf-8") + b"\n")
    later = task_dir / "attempt-02.log"
    later.write_text(f"key: {secret}\n", encoding="utf-8")

    artifacts.mask_task_files(task_dir)

    assert secret.encode("utf-8") not in binary.read_bytes()
    assert b"***" in binary.read_bytes()
    assert secret not in later.read_text(encoding="utf-8")


def test_attempt_log_keeps_coder_transcript(spec_repo, make_spec_coder, tmp_path):
    """BEH-24: attempt-NN.log holds the coder's prompts and replies (drained
    through ``take_transcript``), not only the step markers."""
    coder = make_spec_coder()

    report = _run(
        spec_repo, coder, tmp_path, reviewer=StaticReviewer(),
        only_task="TASK-201",
    )

    assert report.exit_code == 0
    log = (report.run_dir / "TASK-201" / "attempt-01.log").read_text("utf-8")
    assert "--- prompt (write_test, attempt 01) ---" in log
    assert "--- prompt (code, attempt 01) ---" in log
    assert len(report.tasks) == 1  # only_task really narrowed the run

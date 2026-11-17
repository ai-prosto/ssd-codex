"""BEH-16 / BEH-17: the code-review gate by a separate reviewer agent."""

from __future__ import annotations

import builtins
import dataclasses
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sdd_codex import review as review_module
from sdd_codex.config import parse_config
from sdd_codex.errors import ReviewError
from sdd_codex.review import (
    VERDICT_BLOCK,
    VERDICT_COMMIT,
    CodexCliReviewer,
    ReviewRequest,
    ReviewVerdict,
    parse_verdict,
    reviewer_prompt,
)
from sdd_codex.task_loop import STEP_GREEN, STEP_REVIEW, run_task_attempt

COMMIT_REPLY = '{"verdict": "commit", "comments": []}'
BLOCK_REPLY = '{"verdict": "block", "comments": ["use a constant", "add a docstring"]}'


@dataclass
class RecordingReviewer:
    """A fake reviewer agent: records every request, answers from a script."""

    reply: str = COMMIT_REPLY
    requests: list[ReviewRequest] = field(default_factory=list)
    events: list[tuple[str, str]] | None = None

    def __call__(self, request: ReviewRequest) -> str:
        self.requests.append(request)
        if self.events is not None:
            self.events.append(("reviewer", "review"))
        return self.reply


@pytest.fixture
def no_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """BEH-16: no human confirmation is ever requested in the gate."""

    def refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("the review gate asked a human for confirmation")

    monkeypatch.setattr(builtins, "input", refuse)


def test_reviewer_is_separate_automatic_agent(task_repo, make_coder, no_human) -> None:
    """BEH-16: before the commit a *separate* reviewer role runs automatically:
    its own model from the config, a clean thread without coder history, the
    ``git diff HEAD`` plus task record and both test outputs on input, and a
    strict JSON verdict stored in attempt-NN.review.json."""
    config = parse_config(
        {"openai": {"models": {"coder": "coder-model", "reviewer": "reviewer-model"}}}
    )
    events: list[tuple[str, str]] = []
    coder = make_coder(events=events)
    reviewer = RecordingReviewer(events=events)

    result = run_task_attempt(
        task_repo.task,
        config,
        task_repo.repo,
        coder,
        task_repo.task_dir,
        reviewer=reviewer,
    )

    assert result.accepted and result.commit
    # The review ran automatically, once, after coding and before the commit.
    assert events == [
        ("coder", "write_test"),
        ("coder", "code"),
        ("reviewer", "review"),
    ]
    request = reviewer.requests[0]
    # Its own role model from the config, not the coder's.
    assert request.model == "reviewer-model"
    # The reviewer sees the repo, the diff against HEAD, the task record
    # and both test outputs...
    assert request.task.id == task_repo.task.id
    assert request.repo == task_repo.repo
    assert "src/out.txt" in request.diff and "+hello" in request.diff
    assert "missing or wrong src/out.txt" in request.red_output
    assert "ok" in request.green_output
    # ...and nothing else: the request has no field for the coder's thread,
    # so the review runs on a clean slate by construction.
    assert {f.name for f in dataclasses.fields(ReviewRequest)} == {
        "task",
        "attempt",
        "model",
        "repo",
        "diff",
        "red_output",
        "green_output",
    }
    # The verdict is strict JSON on disk.
    verdict_file = task_repo.task_dir / "attempt-01.review.json"
    assert result.review_path == verdict_file
    assert json.loads(verdict_file.read_text(encoding="utf-8")) == {
        "verdict": "commit",
        "comments": [],
    }
    assert result.review_verdict == VERDICT_COMMIT


def test_block_restarts_task_with_comments_as_retry(
    task_repo, make_coder, no_human
) -> None:
    """BEH-17: a ``block`` verdict closes the attempt without a commit, the
    comments travel into the next attempt's coder prompts, and the attempt
    counts as a retry exactly like a failed test."""
    reviewer = RecordingReviewer(reply=BLOCK_REPLY)

    blocked = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        make_coder(),
        task_repo.task_dir,
        reviewer=reviewer,
    )

    assert not blocked.accepted
    assert blocked.step_reached == STEP_REVIEW
    assert not blocked.commit
    assert task_repo.commit_count() == 1  # no commit was made
    assert task_repo.porcelain() == []  # tree rolled back, ready for the retry
    assert blocked.review_verdict == VERDICT_BLOCK
    assert blocked.review_comments == ("use a constant", "add a docstring")
    assert "block" in blocked.failure_reason
    assert "use a constant" in blocked.failure_reason
    verdict_file = task_repo.task_dir / "attempt-01.review.json"
    assert json.loads(verdict_file.read_text(encoding="utf-8")) == {
        "verdict": "block",
        "comments": ["use a constant", "add a docstring"],
    }

    # Counts as a retry exactly like a failed test: the orchestrator sees
    # the same not-accepted attempt result with a failure reason.
    failed_test = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        make_coder(out_content="wrong\n"),
        task_repo.task_dir,
        attempt=2,
    )
    assert failed_test.step_reached == STEP_GREEN
    assert (blocked.accepted, bool(blocked.failure_reason)) == (
        failed_test.accepted,
        bool(failed_test.failure_reason),
    )

    # The comments land in every coder prompt of the next attempt.
    retry_coder = make_coder()
    retry = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        retry_coder,
        task_repo.task_dir,
        attempt=3,
        reviewer=RecordingReviewer(),
        review_comments=blocked.review_comments,
    )
    assert retry.accepted
    assert retry_coder.calls == ["write_test", "code"]
    for request in retry_coder.requests:
        assert request.review_comments == ("use a constant", "add a docstring")


def test_unrecognised_reply_counts_as_block(task_repo, make_coder) -> None:
    """BEH-17: a reply that is not the strict JSON verdict blocks the attempt."""
    reviewer = RecordingReviewer(reply="LGTM!")

    result = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        make_coder(),
        task_repo.task_dir,
        reviewer=reviewer,
    )

    assert not result.accepted
    assert result.step_reached == STEP_REVIEW
    assert not result.commit
    assert task_repo.commit_count() == 1
    data = json.loads(
        (task_repo.task_dir / "attempt-01.review.json").read_text(encoding="utf-8")
    )
    assert data["verdict"] == "block"
    assert any("LGTM!" in comment for comment in data["comments"])
    # The reason travels into the next prompt like regular review comments.
    assert result.review_comments == tuple(data["comments"])


def test_crashing_reviewer_is_a_failed_attempt(task_repo, make_coder) -> None:
    """A reviewer agent that fails to run closes the attempt like any other
    crashed step: no commit, tree rolled back, nothing raised."""

    def crashing(request: ReviewRequest) -> str:
        raise ReviewError("codex exploded")

    result = run_task_attempt(
        task_repo.task,
        task_repo.config,
        task_repo.repo,
        make_coder(),
        task_repo.task_dir,
        reviewer=crashing,
    )

    assert not result.accepted
    assert result.step_reached == STEP_REVIEW
    assert "codex exploded" in result.failure_reason
    assert task_repo.commit_count() == 1
    assert task_repo.porcelain() == []


@pytest.mark.parametrize(
    "raw",
    [
        "LGTM!",  # not JSON at all
        '["commit"]',  # not an object
        '{"verdict": "ship it"}',  # unknown verdict value
        '{"verdict": "commit", "comments": [], "severity": "low"}',  # unknown key
        '{"comments": ["x"]}',  # verdict missing
        '{"verdict": "block", "comments": "fix"}',  # comments not a list
        '{"verdict": "block", "comments": [1]}',  # comment not a string
    ],
)
def test_parse_verdict_rejects_unrecognised_replies(raw: str) -> None:
    verdict = parse_verdict(raw)
    assert verdict.verdict == VERDICT_BLOCK
    assert not verdict.approved
    assert len(verdict.comments) == 1
    assert "unrecognised reviewer reply" in verdict.comments[0]


def test_parse_verdict_accepts_strict_json() -> None:
    assert parse_verdict(COMMIT_REPLY) == ReviewVerdict(VERDICT_COMMIT, ())
    assert parse_verdict(BLOCK_REPLY) == ReviewVerdict(
        VERDICT_BLOCK, ("use a constant", "add a docstring")
    )
    # "comments" may be omitted; the verdict alone is still strict JSON.
    assert parse_verdict('{"verdict": "commit"}').approved


def _request(model: str = "reviewer-model") -> ReviewRequest:
    from sdd_codex.format.tasks import Task

    task = Task(id="TASK-101", name="Demo task", file="tasks.md", line=1, position=0)
    return ReviewRequest(
        task=task,
        attempt=1,
        model=model,
        repo=Path("/work/repo"),
        diff="diff --git a/x b/x",
        red_output="red out",
        green_output="green out",
    )


def test_reviewer_prompt_contains_all_inputs_and_json_contract() -> None:
    prompt = reviewer_prompt(_request())
    assert "TASK-101: Demo task" in prompt
    assert "diff --git a/x b/x" in prompt
    assert "red out" in prompt and "green out" in prompt
    assert '"verdict"' in prompt and '"commit" | "block"' in prompt


def test_codex_cli_reviewer_spawns_fresh_exec_with_reviewer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default reviewer runs ``codex exec`` — a fresh thread per call —
    in the repo, with the reviewer-role model, the prompt on stdin (a diff
    as an argv element would hit the OS argument-size limit), and returns
    the raw stdout."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=COMMIT_REPLY, stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    reply = CodexCliReviewer(reasoning_effort="high")(_request())

    assert reply == COMMIT_REPLY
    ((command, kwargs),) = calls
    assert command[:4] == ["codex", "exec", "--cd", "/work/repo"]
    assert command[4:6] == ["--model", "reviewer-model"]
    assert "model_reasoning_effort=high" in command
    assert command[-1] == "-"  # the prompt comes through stdin, not argv
    assert "TASK-101: Demo task" in kwargs["input"]
    assert "diff --git a/x b/x" in kwargs["input"]


def test_codex_cli_reviewer_reports_usage_for_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-15: a review is a paid call like any coder step — its ``tokens
    used`` summary reaches the budget tracker through ``take_usage``."""
    from sdd_codex.budget import Usage

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=COMMIT_REPLY, stderr="tokens used: 4,321\n"
        )

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    reviewer = CodexCliReviewer()

    reviewer(_request())

    assert reviewer.take_usage() == [
        Usage(model="reviewer-model", output_tokens=4321)
    ]
    assert reviewer.take_usage() == []  # drained once, then cleared


def test_codex_cli_reviewer_failures_raise_review_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ReviewError, match="not found in PATH"):
        CodexCliReviewer()(_request())

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def failing_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(ReviewError, match="exited with code 2: boom"):
        CodexCliReviewer()(_request())

    def timing_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timing_out)
    with pytest.raises(ReviewError, match="timed out after 5s"):
        CodexCliReviewer(timeout_sec=5)(_request())

    def unrunnable(command, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "run", unrunnable)
    with pytest.raises(ReviewError, match="cannot run"):
        CodexCliReviewer()(_request())


def test_long_unrecognised_reply_is_truncated_in_the_comment() -> None:
    verdict = parse_verdict("x" * 1000)
    assert verdict.verdict == VERDICT_BLOCK
    assert "…" in verdict.comments[0]
    assert len(verdict.comments[0]) < 600


def test_review_module_has_no_import_time_side_effects() -> None:
    """The gate module is pure on import: no subprocess, no network."""
    assert review_module.CODEX_BINARY == "codex"


def test_review_path_naming(tmp_path: Path) -> None:
    assert review_module.review_path(tmp_path, 7) == tmp_path / "attempt-07.review.json"

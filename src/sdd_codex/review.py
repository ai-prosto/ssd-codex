"""The code-review gate: a separate reviewer agent judges the diff.

Design decision D-12: before the commit step the runner calls a *separate*
reviewer role — its own model from the config, a clean thread without any of
the coder's history. The reviewer receives the ``git diff HEAD``, the task
record and the red and green test outputs, and must answer with the strict
JSON ``{"verdict": "commit" | "block", "comments": [...]}``. The verdict is
stored in ``attempt-NN.review.json``. No human confirmation is ever asked
(BEH-16): ``commit`` is the only path to a commit; ``block`` — or any
unrecognised reply — closes the attempt, its comments travel into the next
attempt's prompt, and the attempt counts as a retry exactly like a failed
test (BEH-17).

The reviewer agent is injectable, like the coder: any callable taking a
:class:`ReviewRequest` and returning the raw reply text. The default
:class:`CodexCliReviewer` spawns the ``codex`` CLI; every ``codex exec``
run is a fresh conversation, so the clean-thread guarantee holds by
construction.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sdd_codex.errors import ReviewError
from sdd_codex.format.tasks import Task
from sdd_codex.git import Git

VERDICT_COMMIT = "commit"
VERDICT_BLOCK = "block"

CODEX_BINARY = "codex"


@dataclass(frozen=True)
class ReviewRequest:
    """Everything the reviewer receives — and nothing else.

    There is deliberately no field for the coder's transcript or thread:
    the reviewer judges the diff on a clean slate (D-12).
    """

    task: Task
    attempt: int
    model: str
    repo: Path
    diff: str
    red_output: str
    green_output: str


ReviewerAgent = Callable[[ReviewRequest], str]


@dataclass(frozen=True)
class ReviewVerdict:
    """The parsed verdict of one review."""

    verdict: str  # VERDICT_COMMIT or VERDICT_BLOCK
    comments: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.verdict == VERDICT_COMMIT

    def to_json(self) -> str:
        return json.dumps(
            {"verdict": self.verdict, "comments": list(self.comments)},
            ensure_ascii=False,
            indent=2,
        )


def parse_verdict(raw: str) -> ReviewVerdict:
    """Parse the reviewer's reply as the strict JSON verdict.

    Anything else — invalid JSON, a non-object, unknown or missing keys,
    wrongly typed values — is an unrecognised reply and yields a ``block``
    verdict carrying the reason, per BEH-17.
    """

    def unrecognised(reason: str) -> ReviewVerdict:
        reply = raw.strip()
        if len(reply) > 500:
            reply = reply[:500] + "…"
        return ReviewVerdict(
            VERDICT_BLOCK,
            (f"unrecognised reviewer reply ({reason}): {reply!r}",),
        )

    try:
        data = json.loads(raw)
    except ValueError:
        return unrecognised("not valid JSON")
    if not isinstance(data, dict):
        return unrecognised("not a JSON object")
    unknown = sorted(set(data) - {"verdict", "comments"})
    if unknown:
        return unrecognised(f"unknown keys {', '.join(unknown)}")
    verdict = data.get("verdict")
    if verdict not in (VERDICT_COMMIT, VERDICT_BLOCK):
        return unrecognised(
            f'"verdict" must be "{VERDICT_COMMIT}" or "{VERDICT_BLOCK}"'
        )
    comments = data.get("comments", [])
    if not isinstance(comments, list) or not all(
        isinstance(comment, str) for comment in comments
    ):
        return unrecognised('"comments" must be a list of strings')
    return ReviewVerdict(verdict, tuple(comments))


def review_path(task_dir: str | Path, attempt: int) -> Path:
    """Where the verdict of attempt ``attempt`` lives."""
    return Path(task_dir) / f"attempt-{attempt:02d}.review.json"


def task_record(task: Task) -> str:
    """The task record as the reviewer sees it."""
    lines = [f"{task.id}: {task.name}"]
    if task.description:
        lines.append(task.description)
    for item in task.checklist:
        mark = "x" if item.done else " "
        lines.append(f"- [{mark}] {item.text}")
    if task.touches:
        lines.append(f"Touches: {', '.join(task.touches)}")
    return "\n".join(lines)


def reviewer_prompt(request: ReviewRequest) -> str:
    """The full prompt of one review call: diff, task record, test outputs."""
    return (
        "You are a code reviewer gating a commit. Review the diff below "
        "against the task record and the test outputs.\n"
        "Reply with STRICT JSON only, no other text:\n"
        '{"verdict": "commit" | "block", "comments": ["..."]}\n'
        'Use "commit" only when the diff correctly implements the task; '
        'otherwise use "block" and explain in comments.\n\n'
        f"## Task record\n{task_record(request.task)}\n\n"
        f"## Red test output (before coding)\n{request.red_output}\n\n"
        f"## Green test output (after coding)\n{request.green_output}\n\n"
        f"## Diff (git diff HEAD)\n{request.diff}\n"
    )


def run_review(
    task: Task,
    attempt: int,
    model: str,
    git: Git,
    reviewer: ReviewerAgent,
    task_dir: str | Path,
    red_output: str,
    green_output: str,
) -> ReviewVerdict:
    """Run the review gate for one attempt and persist its verdict.

    Takes the diff against HEAD itself, calls the reviewer agent, parses the
    strict JSON reply (an unrecognised reply becomes ``block``) and writes
    the verdict to ``attempt-NN.review.json``. An exception out of the
    reviewer propagates: the attempt loop treats it as a failed attempt,
    on equal footing with a failed test.
    """
    request = ReviewRequest(
        task=task,
        attempt=attempt,
        model=model,
        repo=git.repo,
        diff=git.diff_head_with_untracked(),
        red_output=red_output,
        green_output=green_output,
    )
    verdict = parse_verdict(reviewer(request))
    path = review_path(task_dir, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(verdict.to_json() + "\n", encoding="utf-8")
    return verdict


class CodexCliReviewer:
    """Default reviewer: spawns the ``codex`` CLI on the reviewer's model.

    Each call is a separate ``codex exec`` process — a fresh thread with no
    coder history — using ``request.model`` (the ``reviewer`` role model
    from the config).
    """

    def __init__(self, reasoning_effort: str = "medium", timeout_sec: int = 1800) -> None:
        self.reasoning_effort = reasoning_effort
        self.timeout_sec = timeout_sec

    def __call__(self, request: ReviewRequest) -> str:
        if shutil.which(CODEX_BINARY) is None:
            raise ReviewError(
                f"'{CODEX_BINARY}' CLI not found in PATH; run 'sdd-codex doctor'"
            )
        command = [
            CODEX_BINARY,
            "exec",
            "--cd",
            str(request.repo),
            "--model",
            request.model,
            "-c",
            f"model_reasoning_effort={self.reasoning_effort}",
            # "-" reads the prompt from stdin: the prompt embeds the whole
            # diff, which as an argv element would hit the OS argument-size
            # limit and show up in the process list.
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                input=reviewer_prompt(request),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReviewError(
                f"reviewer timed out after {self.timeout_sec}s"
            ) from exc
        except OSError as exc:
            raise ReviewError(f"cannot run '{CODEX_BINARY}': {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ReviewError(
                f"'{CODEX_BINARY}' exited with code {result.returncode}: {detail}"
            )
        return result.stdout

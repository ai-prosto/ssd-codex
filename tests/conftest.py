from __future__ import annotations

import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sdd_codex.config import Config
from sdd_codex.format.tasks import Task, parse_tasks
from sdd_codex.task_loop import CoderRequest

# The verify script the scripted coder writes at the write_test step: red
# while src/out.txt is missing or wrong, green once the code step wrote it.
CHECK_FAILING = """\
import sys
from pathlib import Path

out = Path("src/out.txt")
if not out.is_file() or out.read_text(encoding="utf-8") != "hello\\n":
    print("missing or wrong src/out.txt")
    sys.exit(1)
print("ok")
"""

TASKS_MD = """\
## Milestone 1: demo

### TASK-101: Demo task
P2 | TODO   Est: 0.5d

Make the demo output file.

**Checklist:**
- [ ] produce out.txt

**Touches:** src/
**Test:** {command}
"""


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@dataclass
class TaskRepo:
    """A real git repository with one parsed demo task."""

    repo: Path
    task: Task
    config: Config
    task_dir: Path

    def git(self, *args: str) -> str:
        return run_git(self.repo, *args)

    def commit_count(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD").strip())

    def porcelain(self) -> list[str]:
        return self.git(
            "status", "--porcelain", "--untracked-files=all"
        ).splitlines()


@dataclass
class ScriptedCoder:
    """A fake coder agent: writes files per step and records every request."""

    repo: Path
    test_body: str = CHECK_FAILING
    out_content: str = "hello\n"
    tamper_test: bool = False
    extra_file: str | None = None
    helper_at_write_test: str | None = None
    calls: list[str] = field(default_factory=list)
    requests: list[CoderRequest] = field(default_factory=list)
    events: list[tuple[str, str]] | None = None

    def __call__(self, request: CoderRequest) -> None:
        self.calls.append(request.step)
        self.requests.append(request)
        if self.events is not None:
            self.events.append(("coder", request.step))
        if request.step == "write_test":
            check = self.repo / "tests" / "check.py"
            check.parent.mkdir(parents=True, exist_ok=True)
            check.write_text(self.test_body, encoding="utf-8")
            if self.helper_at_write_test:
                helper = self.repo / self.helper_at_write_test
                helper.parent.mkdir(parents=True, exist_ok=True)
                helper.write_text("helper\n", encoding="utf-8")
            return
        out = self.repo / "src" / "out.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.out_content, encoding="utf-8")
        if self.tamper_test:
            check = self.repo / "tests" / "check.py"
            check.write_text(self.test_body + "# tampered\n", encoding="utf-8")
        if self.extra_file:
            (self.repo / self.extra_file).write_text("stray\n", encoding="utf-8")


@pytest.fixture
def task_repo(tmp_path: Path) -> TaskRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "test")
    command = f"{shlex.quote(sys.executable)} tests/check.py"
    (repo / "tasks.md").write_text(TASKS_MD.format(command=command), encoding="utf-8")
    (repo / ".gitignore").write_text(".sdd-codex/\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    doc = parse_tasks((repo / "tasks.md").read_text(encoding="utf-8"), repo / "tasks.md")
    assert not doc.issues
    return TaskRepo(
        repo=repo,
        task=doc.tasks[0],
        config=Config(),
        task_dir=tmp_path / "artifacts" / "TASK-101",
    )


@pytest.fixture
def make_coder(task_repo: TaskRepo):
    def factory(**kwargs: object) -> ScriptedCoder:
        return ScriptedCoder(repo=task_repo.repo, **kwargs)  # type: ignore[arg-type]

    return factory


# --- A ready spec set with two tasks, for the orchestration tests -----------

SPEC_REQUIREMENTS = """\
# demo — requirements

### REQ-001: Produce the outputs
The program writes the demo output files.
"""

SPEC_DESIGN = """\
# demo — design

### DESIGN-001: Two files
Two scripts implement REQ-001.
"""

SPEC_TASKS = """\
## Milestone 1: demo

### TASK-201: First demo task
P2 | TODO   Est: 0.5d

Write the first output file.

**Checklist:**
- [ ] produce src/out_201.txt

**Touches:** src/
**Test:** {py} tests/check_201.py

### TASK-202: Second demo task
P2 | TODO   Est: 0.5d

Write the second output file.

**Checklist:**
- [ ] produce src/out_202.txt

**Depends on:** [TASK-201]
**Touches:** src/
**Test:** {py} tests/check_202.py
"""


@dataclass
class SpecRepo:
    """A real git repository holding a ready two-task spec set."""

    repo: Path
    config: Config

    def git(self, *args: str) -> str:
        return run_git(self.repo, *args)

    def commit_count(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD").strip())

    def spec_set(self):
        from sdd_codex.specset import load_spec_set

        return load_spec_set(self.repo / "spec")


@pytest.fixture
def spec_repo(tmp_path: Path) -> SpecRepo:
    repo = tmp_path / "repo"
    (repo / "spec").mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "test")
    py = shlex.quote(sys.executable)
    (repo / "spec" / "requirements.md").write_text(SPEC_REQUIREMENTS, encoding="utf-8")
    (repo / "spec" / "design.md").write_text(SPEC_DESIGN, encoding="utf-8")
    (repo / "spec" / "tasks.md").write_text(SPEC_TASKS.format(py=py), encoding="utf-8")
    (repo / ".gitignore").write_text(".sdd-codex/\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    return SpecRepo(repo=repo, config=Config())


# The verify script a SpecCoder writes: red while the output file is missing
# or wrong; on failure it also echoes the OPENAI_API_KEY from the environment
# so the masking of the artifacts (BEH-24) is observable.
CHECK_TEMPLATE = """\
import os
import sys
from pathlib import Path

out = Path("src/out_{num}.txt")
if not out.is_file() or out.read_text(encoding="utf-8") != "hello\\n":
    print("missing or wrong src/out_{num}.txt")
    print("env key: " + os.environ.get("OPENAI_API_KEY", ""))
    sys.exit(1)
print("ok")
"""


@dataclass
class SpecCoder:
    """A fake coder for multi-task runs, keyed by the task id.

    ``crash`` names tasks whose every step raises (the realistic agent
    failure); ``wrong_until`` maps a task id to the first attempt number
    whose code step writes the correct content — earlier attempts write a
    wrong file, so their green run fails. ``usage_per_call`` makes the coder
    report that many output tokens per call through ``take_usage``.
    """

    repo: Path
    crash: frozenset[str] = frozenset()
    wrong_until: dict[str, int] = field(default_factory=dict)
    usage_per_call: int = 0
    model: str = "fake-model"
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    _usage: list = field(default_factory=list)

    def take_usage(self) -> list:
        usage, self._usage = self._usage, []
        return usage

    def __call__(self, request: CoderRequest) -> None:
        from sdd_codex.budget import Usage

        self.calls.append((request.task.id, request.step, request.attempt))
        if self.usage_per_call:
            self._usage.append(
                Usage(model=self.model, output_tokens=self.usage_per_call)
            )
        task_id = request.task.id
        if task_id in self.crash:
            raise RuntimeError(f"agent crashed on {task_id}")
        num = task_id.rsplit("-", 1)[1]
        if request.step == "write_test":
            check = self.repo / "tests" / f"check_{num}.py"
            check.parent.mkdir(parents=True, exist_ok=True)
            check.write_text(CHECK_TEMPLATE.format(num=num), encoding="utf-8")
            return
        good_from = self.wrong_until.get(task_id, 1)
        content = "hello\n" if request.attempt >= good_from else "wrong\n"
        out = self.repo / "src" / f"out_{num}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")


@dataclass
class StaticReviewer:
    """A fake reviewer returning a fixed raw reply."""

    reply: str = '{"verdict": "commit", "comments": []}'
    requests: list = field(default_factory=list)

    def __call__(self, request) -> str:
        self.requests.append(request)
        return self.reply


@pytest.fixture
def make_spec_coder(spec_repo: SpecRepo):
    def factory(**kwargs: object) -> SpecCoder:
        return SpecCoder(repo=spec_repo.repo, **kwargs)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to open a network connection."""

    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during a pure parsing step")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)

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

**Touches:** src/out.txt
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


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to open a network connection."""

    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during a pure parsing step")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)

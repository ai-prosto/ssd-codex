"""Parser for ``tasks.md`` in the spec-runner format.

Recognised structure (see spec-runner ``spec/FORMAT.md``):

* optional YAML frontmatter;
* ``## Milestone ...`` headings — tasks below inherit the milestone name;
* ``### TASK-NNN: Name`` headers;
* a priority/status line, with emoji (``🟡 P2 | ⬜ TODO   Est: 1d``) or
  without (``P2 | TODO   Est: 1d``);
* free-text description;
* ``**Checklist:**`` with ``- [ ]`` / ``- [x]`` items at column 0;
* ``**Depends on:** / **Blocks:** / **Traces to:**`` bracketed references;
* ``**Touches:**``, ``**Verifies:**`` (inline or bulleted block form),
  ``**Test:**``, ``**Mode:**``, ``**Scenarios:**``.

Unknown lines are preserved on the task (``extra_lines``) and never error.
All problems are collected as :class:`SpecIssue` values with file, line and
task id — parsing is pure and makes no network or API calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdd_codex.errors import SpecIssue
from sdd_codex.format.docs import split_frontmatter

TASK_HEADER_RE = re.compile(r"^### (TASK-\d+):\s+(\S.*?)\s*$")
TASK_HEADER_LOOSE_RE = re.compile(r"^###\s*(TASK[^\s:]*)")
MILESTONE_RE = re.compile(r"^##\s+(?!#)(.+?)\s*$")
META_RE = re.compile(
    r"^(?:(?P<prio_emoji>[^\w\s|]+)\s*)?(?P<priority>[Pp]\d)\s*\|"
    r"\s*(?:(?P<status_emoji>[^\w\s|]+)\s*)?(?P<status>[A-Za-z_]+)"
    r"(?:\s+Est:\s*(?P<estimate>\S+))?\s*$"
)
CHECKLIST_RE = re.compile(r"^- \[([ x])\] (.+)$")
FIELD_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z -]*?):\*\*\s*(.*?)\s*$")
BRACKET_REF_RE = re.compile(r"\[([A-Z][A-Z0-9]*-\d+)\]")
BULLET_RE = re.compile(r"^- (?!\[)(\S.*?)\s*$")

VALID_STATUSES = {"todo", "in_progress", "review", "done", "blocked"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}

# Fields whose value is a comma-separated list of bracketed task/doc ids.
_REF_FIELDS = {"depends on": "depends_on", "blocks": "blocks", "traces to": "traces_to"}


@dataclass
class ChecklistItem:
    text: str
    done: bool


@dataclass
class Task:
    id: str
    name: str
    file: str
    line: int
    position: int
    milestone: str = ""
    priority: str = "p0"
    status: str = "todo"
    estimate: str = ""
    description: str = ""
    checklist: list[ChecklistItem] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    traces_to: list[str] = field(default_factory=list)
    touches: list[str] = field(default_factory=list)
    verifies: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    test_command: str = ""
    mode: str = ""
    extra_lines: list[str] = field(default_factory=list)


@dataclass
class TaskDocument:
    file: str
    tasks: list[Task]
    frontmatter: dict[str, Any] | None
    issues: list[SpecIssue] = field(default_factory=list)

    def task_ids(self) -> set[str]:
        return {task.id for task in self.tasks}


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_tasks(text: str, file: str | Path) -> TaskDocument:
    """Parse ``tasks.md`` text, collecting all format issues."""
    file = str(file)
    issues: list[SpecIssue] = []
    frontmatter_text, body, start_line = split_frontmatter(text)

    frontmatter: dict[str, Any] | None = None
    if frontmatter_text is not None:
        import yaml

        try:
            loaded = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            issues.append(SpecIssue(file, 1, None, f"invalid YAML frontmatter: {exc}"))
        else:
            frontmatter = loaded if isinstance(loaded, dict) else {}

    tasks: list[Task] = []
    seen_ids: dict[str, int] = {}
    milestone = ""
    current: Task | None = None
    description_lines: list[str] = []
    seen_structured_field = False
    pending_list_field: str | None = None

    def flush_description() -> None:
        nonlocal description_lines
        if current is not None and description_lines:
            current.description = "\n".join(description_lines).strip()
        description_lines = []

    for offset, line in enumerate(body.splitlines()):
        line_no = start_line + offset
        stripped = line.rstrip()

        header = TASK_HEADER_RE.match(stripped)
        if header:
            flush_description()
            task_id = header.group(1)
            if task_id in seen_ids:
                issues.append(
                    SpecIssue(
                        file,
                        line_no,
                        task_id,
                        f"duplicate task id (first defined at line {seen_ids[task_id]})",
                    )
                )
            else:
                seen_ids[task_id] = line_no
            current = Task(
                id=task_id,
                name=header.group(2),
                file=file,
                line=line_no,
                position=len(tasks),
                milestone=milestone,
            )
            tasks.append(current)
            seen_structured_field = False
            pending_list_field = None
            continue

        loose = TASK_HEADER_LOOSE_RE.match(stripped)
        if loose and stripped.startswith("###"):
            # A line that clearly tries to be a task header but does not
            # match the required ``### TASK-NNN: name`` shape.
            flush_description()
            task_id = loose.group(1) if re.fullmatch(r"TASK-\d+", loose.group(1)) else None
            issues.append(
                SpecIssue(
                    file,
                    line_no,
                    task_id,
                    f"malformed task header: {stripped!r} "
                    "(expected '### TASK-NNN: name')",
                )
            )
            current = None
            pending_list_field = None
            continue

        milestone_match = MILESTONE_RE.match(stripped)
        if milestone_match:
            flush_description()
            milestone = milestone_match.group(1)
            current = None
            pending_list_field = None
            continue

        if current is None:
            continue

        if pending_list_field is not None:
            bullet = BULLET_RE.match(stripped)
            if bullet:
                getattr(current, pending_list_field).append(bullet.group(1))
                continue
            if stripped:
                pending_list_field = None

        meta = META_RE.match(stripped)
        if meta and not seen_structured_field and not current.checklist:
            priority = meta.group("priority").lower()
            status = meta.group("status").lower()
            if priority not in VALID_PRIORITIES:
                issues.append(
                    SpecIssue(file, line_no, current.id, f"invalid priority {priority!r}")
                )
            if status not in VALID_STATUSES:
                issues.append(
                    SpecIssue(file, line_no, current.id, f"invalid status {status!r}")
                )
            current.priority = priority
            current.status = status
            current.estimate = meta.group("estimate") or ""
            continue

        checklist = CHECKLIST_RE.match(line)
        if checklist:
            flush_description()
            current.checklist.append(
                ChecklistItem(text=checklist.group(2), done=checklist.group(1) == "x")
            )
            continue

        fld = FIELD_RE.match(stripped)
        if fld:
            flush_description()
            seen_structured_field = True
            name = fld.group(1).strip().lower()
            value = fld.group(2)
            if name in _REF_FIELDS:
                setattr(current, _REF_FIELDS[name], BRACKET_REF_RE.findall(value))
            elif name == "touches":
                current.touches = _split_csv(value)
            elif name == "verifies":
                if value:
                    current.verifies = _split_csv(value)
                else:
                    pending_list_field = "verifies"
            elif name == "scenarios":
                current.scenarios = _split_csv(value)
            elif name == "test":
                current.test_command = value
            elif name == "mode":
                current.mode = value.lower()
            elif name == "checklist":
                pass
            else:
                # Unknown bold field: preserved, ignored.
                current.extra_lines.append(line)
            continue

        if seen_structured_field or current.checklist:
            if stripped:
                current.extra_lines.append(line)
        else:
            description_lines.append(line)

    flush_description()
    return TaskDocument(file=file, tasks=tasks, frontmatter=frontmatter, issues=issues)


def validate_dependencies(doc: TaskDocument) -> list[SpecIssue]:
    """Check that ``Depends on`` references exist and form no cycle."""
    issues: list[SpecIssue] = []
    ids = doc.task_ids()
    for task in doc.tasks:
        for dep in task.depends_on:
            if dep.startswith("TASK-") and dep not in ids:
                issues.append(
                    SpecIssue(
                        doc.file,
                        task.line,
                        task.id,
                        f"depends on unknown task {dep}",
                    )
                )
    issues.extend(_find_cycles(doc))
    return issues


def _find_cycles(doc: TaskDocument) -> list[SpecIssue]:
    by_id = {task.id: task for task in doc.tasks}
    state: dict[str, int] = {}  # 0 unvisited / 1 in stack / 2 done
    stack: list[str] = []
    issues: list[SpecIssue] = []
    reported: set[frozenset[str]] = set()

    def visit(task_id: str) -> None:
        state[task_id] = 1
        stack.append(task_id)
        for dep in by_id[task_id].depends_on:
            if dep not in by_id:
                continue
            if state.get(dep, 0) == 0:
                visit(dep)
            elif state.get(dep) == 1:
                cycle = stack[stack.index(dep) :] + [dep]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    task = by_id[task_id]
                    issues.append(
                        SpecIssue(
                            doc.file,
                            task.line,
                            task.id,
                            "dependency cycle: " + " -> ".join(cycle),
                        )
                    )
        stack.pop()
        state[task_id] = 2

    for task in doc.tasks:
        if state.get(task.id, 0) == 0:
            visit(task.id)
    return issues


def execution_order(doc: TaskDocument) -> list[Task]:
    """Topological order by ``Depends on``, ties broken by file position.

    Call only on a document whose :func:`validate_dependencies` is clean;
    tasks that are part of a cycle are omitted from the result.
    """
    by_id = {task.id: task for task in doc.tasks}
    remaining_deps = {
        task.id: {dep for dep in task.depends_on if dep in by_id}
        for task in doc.tasks
    }
    ordered: list[Task] = []
    done: set[str] = set()
    while len(ordered) < len(doc.tasks):
        ready = [
            task
            for task in doc.tasks
            if task.id not in done and not (remaining_deps[task.id] - done)
        ]
        if not ready:
            break  # cycle — validate_dependencies reports it
        next_task = min(ready, key=lambda task: task.position)
        ordered.append(next_task)
        done.add(next_task.id)
    return ordered

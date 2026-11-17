"""BEH-01 / BEH-02: the spec-runner format parser and its early error reporting."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sdd_codex.errors import SpecFormatError
from sdd_codex.format import execution_order, parse_doc, parse_tasks
from sdd_codex.format.tasks import validate_dependencies
from sdd_codex.specset import load_spec_set, validate_spec_set

REQUIREMENTS = """\
---
spec_stage: requirements
status: approved
version: 1
---
# Requirements

## 2. Functional Requirements

#### REQ-001: User can log in
**Priority**: P0

**Acceptance Criteria**:
- [ ] POST /api/login returns a JWT on valid credentials
- [ ] Invalid credentials return 401

**Traces to:** [DESIGN-002], [TASK-001]

#### REQ-002: Rate limiting
Plain prose body, no fixed structure.
"""

DESIGN = """\
# Design

#### DESIGN-001: Login endpoint
Implements REQ-001.

```text
### TASK-999: a heading inside a code fence is not a task
```

#### DESIGN-002: Token signing
Covers REQ-001 as well.
"""

TASKS = """\
---
spec_stage: tasks
status: approved
---
## Milestone 1: Authentication

### TASK-001: Implement login endpoint
🟡 P2 | ⬜ TODO   Est: 2d

Implement the /api/login endpoint with JWT token generation.

**Checklist:**
- [x] Create route handler
- [ ] Add JWT signing

**Traces to:** [REQ-001], [DESIGN-002]
**Touches:** src/auth/login.py, docs/
**Verifies:** tests/test_login.py::test_ok, tests/test_login.py::test_locked
**Frobnicate:** an unknown field that must be preserved, not rejected

### TASK-002: Add rate limiting
P1 | IN_PROGRESS   Est: 1d

**Depends on:** [TASK-001]
**Traces to:** [REQ-002]
**Test:** uv run pytest tests/test_rate.py -q
**Mode:** verify_first

### TASK-003: No metadata line at all
"""


def write_spec_set(spec_dir: Path, tasks: str = TASKS) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text(REQUIREMENTS, encoding="utf-8")
    (spec_dir / "design.md").write_text(DESIGN, encoding="utf-8")
    (spec_dir / "tasks.md").write_text(tasks, encoding="utf-8")
    return spec_dir


def test_parses_ready_spec_runner_set(tmp_path: Path) -> None:
    """BEH-01: a ready spec-runner set parses fully; unknown lines survive."""
    spec_dir = write_spec_set(tmp_path / "spec")

    spec_set = load_spec_set(spec_dir)

    # Requirements and design blocks are read, with frontmatter and references.
    assert set(spec_set.requirements.blocks) == {"REQ-001", "REQ-002"}
    assert spec_set.requirements.frontmatter == {
        "spec_stage": "requirements",
        "status": "approved",
        "version": 1,
    }
    req = spec_set.requirements.blocks["REQ-001"]
    assert req.name == "User can log in"
    assert "DESIGN-002" in req.traces_to and "TASK-001" in req.traces_to
    assert "Invalid credentials return 401" in req.raw
    assert set(spec_set.design.blocks) == {"DESIGN-001", "DESIGN-002"}

    # All tasks and their fields are read.
    tasks = {task.id: task for task in spec_set.tasks.tasks}
    assert set(tasks) == {"TASK-001", "TASK-002", "TASK-003"}

    t1 = tasks["TASK-001"]
    assert t1.name == "Implement login endpoint"
    assert t1.milestone == "Milestone 1: Authentication"
    assert (t1.priority, t1.status, t1.estimate) == ("p2", "todo", "2d")  # emoji line
    assert "JWT token generation" in t1.description
    assert [(item.text, item.done) for item in t1.checklist] == [
        ("Create route handler", True),
        ("Add JWT signing", False),
    ]
    assert t1.traces_to == ["REQ-001", "DESIGN-002"]
    assert t1.touches == ["src/auth/login.py", "docs/"]
    assert t1.verifies == [
        "tests/test_login.py::test_ok",
        "tests/test_login.py::test_locked",
    ]
    # The unknown field raised no error and is preserved as written.
    assert any("Frobnicate" in line for line in t1.extra_lines)

    t2 = tasks["TASK-002"]
    assert (t2.priority, t2.status) == ("p1", "in_progress")  # plain line, no emoji
    assert t2.depends_on == ["TASK-001"]
    assert t2.test_command == "uv run pytest tests/test_rate.py -q"
    assert t2.mode == "verify_first"

    t3 = tasks["TASK-003"]
    assert (t3.priority, t3.status) == ("p0", "todo")  # defaults

    # Dependency order: TASK-001 before TASK-002, file order otherwise.
    assert [task.id for task in spec_set.execution_order] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
    ]


def test_reports_format_and_dependency_errors_without_api_calls(
    tmp_path: Path, no_network: None
) -> None:
    """BEH-02: broken header, unknown dependency and cycle are each refused
    with file, line and task id — and no OpenAI call is ever made."""
    # Case 1: broken task header (no colon / name).
    broken_header = "### TASK-001: Fine task\n\n### TASK-002 missing colon\n"
    spec_dir = write_spec_set(tmp_path / "broken", tasks=broken_header)
    issues = validate_spec_set(spec_dir)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.file.endswith("tasks.md")
    assert issue.line == 3
    assert issue.task_id == "TASK-002"
    assert "malformed task header" in issue.message
    assert "tasks.md:3: TASK-002:" in str(issue)

    # Case 2: dependency on a task id that does not exist.
    missing_dep = "### TASK-001: Only task\nP1 | TODO\n\n**Depends on:** [TASK-999]\n"
    spec_dir = write_spec_set(tmp_path / "missing", tasks=missing_dep)
    issues = validate_spec_set(spec_dir)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.file.endswith("tasks.md")
    assert issue.line == 1
    assert issue.task_id == "TASK-001"
    assert "unknown task TASK-999" in issue.message

    # Case 3: dependency cycle.
    cycle = (
        "### TASK-001: First\nP1 | TODO\n\n**Depends on:** [TASK-002]\n\n"
        "### TASK-002: Second\nP1 | TODO\n\n**Depends on:** [TASK-001]\n"
    )
    spec_dir = write_spec_set(tmp_path / "cycle", tasks=cycle)
    issues = validate_spec_set(spec_dir)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.file.endswith("tasks.md")
    assert issue.line is not None
    assert issue.task_id in {"TASK-001", "TASK-002"}
    assert "dependency cycle" in issue.message
    assert "TASK-001" in issue.message and "TASK-002" in issue.message

    # load_spec_set refuses the same way instead of returning a partial set.
    with pytest.raises(SpecFormatError) as excinfo:
        load_spec_set(spec_dir)
    assert "dependency cycle" in str(excinfo.value)

    # No paid call could have happened: nothing network-related was touched
    # (the no_network guard would have failed the test) and no OpenAI/Codex
    # client module was ever imported.
    assert not any(
        name == "openai" or name.startswith(("openai.", "codex"))
        for name in sys.modules
    )


def test_missing_files_reported_by_name(tmp_path: Path) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "tasks.md").write_text("### TASK-001: A\n", encoding="utf-8")
    issues = validate_spec_set(spec_dir)
    missing = {Path(issue.file).name for issue in issues}
    assert missing == {"requirements.md", "design.md"}
    assert all("not found" in issue.message for issue in issues)


def test_duplicate_task_id_and_bad_status_are_issues(tmp_path: Path) -> None:
    text = "### TASK-001: First\nP9 | WIP\n\n### TASK-001: Again\nP1 | TODO\n"
    doc = parse_tasks(text, "tasks.md")
    messages = [issue.message for issue in doc.issues]
    assert any("duplicate task id" in message for message in messages)
    assert any("invalid priority 'p9'" in message for message in messages)
    assert any("invalid status 'wip'" in message for message in messages)


def test_code_fences_in_tasks_md_are_inert() -> None:
    """Headers, meta lines, fields and checklist items inside a code fence
    are description text, not structure."""
    text = (
        "### TASK-001: Real task\n"
        "P1 | TODO\n\n"
        "Example of the format:\n"
        "```markdown\n"
        "### TASK-999: not a task\n"
        "P9 | BOGUS\n"
        "- [x] not a checklist item\n"
        "**Depends on:** [TASK-777]\n"
        "## Not a milestone\n"
        "```\n"
        "After the fence.\n"
    )
    doc = parse_tasks(text, "tasks.md")
    assert doc.issues == []
    assert [task.id for task in doc.tasks] == ["TASK-001"]
    task = doc.tasks[0]
    assert task.checklist == []
    assert task.depends_on == []
    assert "### TASK-999: not a task" in task.description
    assert "After the fence." in task.description
    assert validate_dependencies(doc) == []


def test_outer_fence_with_inner_backticks_stays_fenced() -> None:
    """A 3-backtick example inside a 4-backtick fence does not close it."""
    text = (
        "# Design\n\n"
        "#### DESIGN-001: Fences\n"
        "````markdown\n"
        "```\n"
        "# REQ-099: inside a fence\n"
        "```\n"
        "````\n\n"
        "#### DESIGN-002: After\n"
        "Body.\n"
    )
    doc = parse_doc(text, "design.md")
    assert doc.issues == []
    assert set(doc.blocks) == {"DESIGN-001", "DESIGN-002"}


def test_nested_deeper_id_heading_is_reported() -> None:
    text = "## REQ-001: Outer\nBody.\n\n### REQ-002: Nested deeper\n"
    doc = parse_doc(text, "requirements.md")
    assert set(doc.blocks) == {"REQ-001"}
    assert len(doc.issues) == 1
    assert "REQ-002" in doc.issues[0].message
    assert "nested" in doc.issues[0].message


def test_execution_order_follows_dependencies_not_file_order() -> None:
    text = (
        "### TASK-002: Depends on later task\nP1 | TODO\n\n"
        "**Depends on:** [TASK-001]\n\n"
        "### TASK-001: Must run first\nP1 | TODO\n"
    )
    doc = parse_tasks(text, "tasks.md")
    assert validate_dependencies(doc) == []
    assert [task.id for task in execution_order(doc)] == ["TASK-001", "TASK-002"]


def test_execution_order_omits_cycle_members() -> None:
    text = (
        "### TASK-001: A\nP1 | TODO\n\n**Depends on:** [TASK-002]\n\n"
        "### TASK-002: B\nP1 | TODO\n\n**Depends on:** [TASK-001]\n\n"
        "### TASK-003: Free\nP1 | TODO\n"
    )
    doc = parse_tasks(text, "tasks.md")
    assert any("dependency cycle" in i.message for i in validate_dependencies(doc))
    assert [task.id for task in execution_order(doc)] == ["TASK-003"]


def test_long_dependency_chain_has_no_recursion_error() -> None:
    parts = ["### TASK-0: Root\nP1 | TODO\n"]
    for n in range(1, 1500):
        parts.append(
            f"\n### TASK-{n}: Step\nP1 | TODO\n\n**Depends on:** [TASK-{n - 1}]\n"
        )
    doc = parse_tasks("".join(parts), "tasks.md")
    assert validate_dependencies(doc) == []
    ordered = execution_order(doc)
    assert [task.id for task in ordered[:2]] == ["TASK-0", "TASK-1"]
    assert len(ordered) == 1500


def test_unbracketed_reference_is_an_issue() -> None:
    text = (
        "### TASK-001: A\nP1 | TODO\n\n**Depends on:** TASK-002\n\n"
        "### TASK-002: B\nP1 | TODO\n"
    )
    doc = parse_tasks(text, "tasks.md")
    assert doc.tasks[0].depends_on == []
    assert len(doc.issues) == 1
    issue = doc.issues[0]
    assert issue.task_id == "TASK-001"
    assert "must be bracketed: [TASK-002]" in issue.message


def test_tasks_frontmatter_must_be_mapping() -> None:
    doc = parse_tasks("---\n- a\n- b\n---\n### TASK-001: A\n", "tasks.md")
    assert any("frontmatter must be a YAML mapping" in i.message for i in doc.issues)


def test_uppercase_checklist_touches_block_and_single_meta() -> None:
    text = (
        "### TASK-001: A\n"
        "P1 | IN_PROGRESS\n\n"
        "P2 | DONE\n\n"
        "- [X] uppercase done\n"
        "**Touches:**\n"
        "- src/a.py\n"
        "- src/b.py\n"
    )
    doc = parse_tasks(text, "tasks.md")
    task = doc.tasks[0]
    # The first meta line wins; a later meta-shaped line is plain text.
    assert (task.priority, task.status) == ("p1", "in_progress")
    assert [(i.text, i.done) for i in task.checklist] == [("uppercase done", True)]
    assert task.touches == ["src/a.py", "src/b.py"]


def test_missing_file_does_not_hide_other_issues(tmp_path: Path) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "requirements.md").write_text(REQUIREMENTS, encoding="utf-8")
    (spec_dir / "tasks.md").write_text("### TASK-001 missing colon\n", encoding="utf-8")
    issues = validate_spec_set(spec_dir)
    messages = [issue.message for issue in issues]
    assert any("not found" in message for message in messages)  # design.md
    assert any("malformed task header" in message for message in messages)


def test_unreadable_file_is_an_issue_not_a_crash(tmp_path: Path) -> None:
    spec_dir = write_spec_set(tmp_path / "spec")
    (spec_dir / "design.md").write_bytes(b"\xff\xfe broken \xff")
    issues = validate_spec_set(spec_dir)
    assert any(
        issue.file.endswith("design.md") and "cannot read file" in issue.message
        for issue in issues
    )


def test_verifies_block_form_and_blocks_field() -> None:
    text = (
        "### TASK-001: Block form\n"
        "P1 | TODO\n\n"
        "**Verifies:**\n"
        "- tests/test_a.py::test_one[a,b]\n"
        "- tests/test_b.py::test_two\n"
        "**Blocks:** [TASK-002]\n\n"
        "### TASK-002: Other\n"
    )
    doc = parse_tasks(text, "tasks.md")
    assert doc.issues == []
    task = doc.tasks[0]
    assert task.verifies == [
        "tests/test_a.py::test_one[a,b]",
        "tests/test_b.py::test_two",
    ]
    assert task.blocks == ["TASK-002"]

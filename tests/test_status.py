"""BEH-15: the DONE mark is written into ``tasks.md`` itself, in place."""

from __future__ import annotations

from pathlib import Path

import pytest

from sdd_codex.format.status import mark_done_in_text, mark_task_done
from sdd_codex.format.tasks import parse_tasks

TASKS_MD = """\
---
spec: demo
---

## Milestone 1: demo

### TASK-001: With emoji
🟡 P2 | ⬜ TODO   Est: 1d

Description line kept as is.

**Checklist:**
- [ ] item

### TASK-002: Plain style
P1 | TODO   Est: 2d

**Touches:** src/x.py
"""


def test_marks_task_done_in_tasks_md(tmp_path: Path) -> None:
    """BEH-15: the status token flips to DONE (with ✅ when the line uses
    emoji), everything else stays byte-for-byte, and the parsed status lets a
    re-run skip the task."""
    path = tmp_path / "tasks.md"
    path.write_text(TASKS_MD, encoding="utf-8")

    mark_task_done(path, "TASK-001")
    text = path.read_text(encoding="utf-8")
    # Byte-for-byte: the only difference is the one status token.
    assert text == TASKS_MD.replace(
        "🟡 P2 | ⬜ TODO   Est: 1d", "🟡 P2 | ✅ DONE   Est: 1d"
    )

    mark_task_done(path, "TASK-002")
    text = path.read_text(encoding="utf-8")
    assert "P1 | DONE   Est: 2d" in text  # no emoji style: plain DONE

    # Marking an already-DONE task changes nothing (idempotent re-runs).
    before = path.read_text(encoding="utf-8")
    mark_task_done(path, "TASK-001")
    assert path.read_text(encoding="utf-8") == before

    # The mark is readable back by the parser, so DONE tasks are skippable.
    doc = parse_tasks(path.read_text(encoding="utf-8"), path)
    assert not doc.issues
    assert [task.status for task in doc.tasks] == ["done", "done"]


def test_unknown_task_or_missing_status_line_raise() -> None:
    with pytest.raises(LookupError, match="TASK-999"):
        mark_done_in_text(TASKS_MD, "TASK-999")

    headers_only = "### TASK-001: No meta line\n\n### TASK-002: Next\nP2 | TODO\n"
    with pytest.raises(LookupError, match="no status line"):
        mark_done_in_text(headers_only, "TASK-001")


def test_crlf_line_endings_survive_byte_for_byte(tmp_path: Path) -> None:
    crlf = "### TASK-001: Demo\r\nP2 | TODO   Est: 1d\r\n\r\nBody.\r\n"
    path = tmp_path / "tasks.md"
    path.write_bytes(crlf.encode("utf-8"))
    mark_task_done(path, "TASK-001")
    assert path.read_bytes() == crlf.replace("TODO", "DONE").encode("utf-8")

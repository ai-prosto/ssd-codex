"""Loading and validating a ready spec-runner set of three files.

A spec set is ``requirements.md``, ``design.md`` and ``tasks.md`` in one
directory, produced either by ``sdd-codex spec`` or by another application.
Everything here is pure file parsing: all format and dependency errors are
reported before any paid agent call is possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdd_codex.errors import SpecFormatError, SpecIssue
from sdd_codex.format.docs import SpecDoc, parse_doc
from sdd_codex.format.tasks import (
    Task,
    TaskDocument,
    execution_order,
    parse_tasks,
    validate_dependencies,
)

REQUIREMENTS_FILE = "requirements.md"
DESIGN_FILE = "design.md"
TASKS_FILE = "tasks.md"


@dataclass
class SpecSet:
    """A parsed and validated spec set."""

    spec_dir: Path
    requirements: SpecDoc
    design: SpecDoc
    tasks: TaskDocument

    @property
    def execution_order(self) -> list[Task]:
        """Tasks in dependency order, ties broken by file position."""
        return execution_order(self.tasks)


def validate_spec_set(spec_dir: str | Path) -> list[SpecIssue]:
    """Parse the three files and return every format/dependency issue."""
    _, issues = _load(Path(spec_dir))
    return issues


def load_spec_set(spec_dir: str | Path) -> SpecSet:
    """Load a ready spec set, raising :class:`SpecFormatError` on any issue."""
    spec_set, issues = _load(Path(spec_dir))
    if issues:
        raise SpecFormatError(issues)
    assert spec_set is not None
    return spec_set


def _load(spec_dir: Path) -> tuple[SpecSet | None, list[SpecIssue]]:
    issues: list[SpecIssue] = []
    texts: dict[str, str] = {}
    for name in (REQUIREMENTS_FILE, DESIGN_FILE, TASKS_FILE):
        path = spec_dir / name
        if not path.is_file():
            issues.append(SpecIssue(str(path), None, None, "file not found"))
            continue
        texts[name] = path.read_text(encoding="utf-8")
    if issues:
        return None, issues

    requirements = parse_doc(texts[REQUIREMENTS_FILE], spec_dir / REQUIREMENTS_FILE)
    design = parse_doc(texts[DESIGN_FILE], spec_dir / DESIGN_FILE)
    tasks = parse_tasks(texts[TASKS_FILE], spec_dir / TASKS_FILE)

    issues.extend(requirements.issues)
    issues.extend(design.issues)
    issues.extend(tasks.issues)
    issues.extend(validate_dependencies(tasks))

    spec_set = SpecSet(
        spec_dir=spec_dir, requirements=requirements, design=design, tasks=tasks
    )
    return spec_set, issues

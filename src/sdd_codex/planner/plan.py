"""The ``spec`` command core: description → three spec-runner files.

Design decision D-07: the planner is a prompt package under
``planner/skill/`` (system prompt plus the three file templates). The
program description arrives as a positional argument or via ``--from-file``;
the result is exactly three files in ``paths.spec_dir``. After the planner
answers, the contents are run through the spec-runner parser, and any format
mismatch is a command error — nothing is written silently.

The planner itself is injectable: any callable that takes a
:class:`PlannerRequest` and returns :class:`SpecFiles`. The default
implementation runs ``codex`` as a CLI subprocess (see ``codex.py``); tests
substitute a stub.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sdd_codex.config import Config
from sdd_codex.errors import PlannerError, SpecFormatError, SpecIssue
from sdd_codex.format.docs import parse_doc
from sdd_codex.format.tasks import parse_tasks, validate_dependencies
from sdd_codex.specset import DESIGN_FILE, REQUIREMENTS_FILE, TASKS_FILE

SPEC_FILE_NAMES = (REQUIREMENTS_FILE, DESIGN_FILE, TASKS_FILE)

FILE_MARKER_RE = re.compile(
    r"^=== FILE: (requirements\.md|design\.md|tasks\.md) ===\s*$", re.MULTILINE
)

_SKILL_FILES = (
    "system_prompt.md",
    "requirements.template.md",
    "design.template.md",
    "tasks.template.md",
)


@dataclass(frozen=True)
class SpecFiles:
    """The three file contents a planner must produce."""

    requirements: str
    design: str
    tasks: str

    def by_name(self) -> dict[str, str]:
        return {
            REQUIREMENTS_FILE: self.requirements,
            DESIGN_FILE: self.design,
            TASKS_FILE: self.tasks,
        }


@dataclass(frozen=True)
class PlannerRequest:
    """Everything a planner agent receives for one ``spec`` invocation."""

    description: str
    prompt: str
    model: str
    reasoning_effort: str


Planner = Callable[[PlannerRequest], SpecFiles]


def _skill_text(name: str) -> str:
    package = resources.files("sdd_codex.planner") / "skill" / name
    return package.read_text(encoding="utf-8")


def build_planner_prompt(description: str) -> str:
    """Assemble the planner prompt: system prompt, templates, description."""
    parts = [_skill_text("system_prompt.md")]
    for name in _SKILL_FILES[1:]:
        parts.append(f"=== TEMPLATE: {name} ===\n{_skill_text(name)}")
    parts.append(f"=== PROGRAM DESCRIPTION ===\n{description}")
    return "\n\n".join(parts)


def split_planner_output(text: str) -> SpecFiles:
    """Split planner output into the three files by ``=== FILE: ... ===`` markers."""
    sections: dict[str, str] = {}
    markers = list(FILE_MARKER_RE.finditer(text))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        sections[marker.group(1)] = text[marker.end() : end].strip("\n") + "\n"
    missing = [name for name in SPEC_FILE_NAMES if name not in sections]
    if missing:
        raise PlannerError(
            "planner output is missing section(s): "
            + ", ".join(missing)
            + " (expected '=== FILE: <name> ===' markers)"
        )
    return SpecFiles(
        requirements=sections[REQUIREMENTS_FILE],
        design=sections[DESIGN_FILE],
        tasks=sections[TASKS_FILE],
    )


def validate_spec_files(files: SpecFiles, spec_dir: Path) -> list[SpecIssue]:
    """Run the spec-runner parser over in-memory contents, before any write."""
    issues: list[SpecIssue] = []
    requirements = parse_doc(files.requirements, spec_dir / REQUIREMENTS_FILE)
    issues.extend(requirements.issues)
    design = parse_doc(files.design, spec_dir / DESIGN_FILE)
    issues.extend(design.issues)
    tasks = parse_tasks(files.tasks, spec_dir / TASKS_FILE)
    issues.extend(tasks.issues)
    issues.extend(validate_dependencies(tasks))
    if not tasks.tasks:
        issues.append(
            SpecIssue(
                str(spec_dir / TASKS_FILE),
                None,
                None,
                "no '### TASK-NNN: name' entries found",
            )
        )
    return issues


def plan_spec(
    description: str,
    config: Config,
    planner: Planner,
    base_dir: Path | None = None,
) -> Path:
    """Ask the planner for a spec set, validate it, write exactly three files.

    Returns the spec directory. Raises :class:`SpecFormatError` when the
    planner's output does not conform to the spec-runner format; in that case
    nothing is written.
    """
    request = PlannerRequest(
        description=description,
        prompt=build_planner_prompt(description),
        model=config.openai.models.planner,
        reasoning_effort=config.openai.reasoning_effort,
    )
    files = planner(request)
    spec_dir = (base_dir or Path.cwd()) / config.paths.spec_dir
    issues = validate_spec_files(files, spec_dir)
    if issues:
        raise SpecFormatError(issues)
    spec_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.by_name().items():
        (spec_dir / name).write_text(content, encoding="utf-8")
    return spec_dir

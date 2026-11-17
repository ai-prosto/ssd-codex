"""Parser for requirements.md / design.md in the spec-runner format.

A document is optional YAML frontmatter followed by markdown. The parser
anchors on two firm signals only (the rest of the body varies freely):

1. a heading at any depth matching ``REQ-NNN`` / ``NFR-NNN`` / ``DESIGN-NNN``;
2. a block runs from its heading until the next heading whose level is the
   same or higher (fewer-or-equal ``#``).

Everything in between is preserved verbatim in the block's ``raw`` text, so
unknown lines never cause errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sdd_codex.errors import SpecIssue

BLOCK_HEADING_RE = re.compile(r"^(#{1,6})\s+((?:REQ|NFR|DESIGN)-\d+)(?::\s*(.*?))?\s*$")
REF_RE = re.compile(r"\b((?:REQ|NFR|DESIGN|TASK)-\d+)\b")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


class FenceTracker:
    """Tracks fenced code blocks across lines.

    A fence opens with three or more backticks or tildes and closes only with
    a line of at least as many of the same character and no trailing text, so
    a 3-backtick example inside a 4-backtick fence stays fenced.
    """

    def __init__(self) -> None:
        self._open: tuple[str, int] | None = None  # (fence char, run length)

    def fenced(self, line: str) -> bool:
        """Consume ``line``; True if it is a fence delimiter or fenced content."""
        match = FENCE_RE.match(line)
        if self._open is None:
            if match:
                self._open = (match.group(1)[0], len(match.group(1)))
                return True
            return False
        if match:
            char, length = match.group(1)[0], len(match.group(1))
            if (
                char == self._open[0]
                and length >= self._open[1]
                and not match.group(2).strip()
            ):
                self._open = None
        return True


@dataclass
class DocBlock:
    """One id-keyed block (requirement or design decision)."""

    id: str
    name: str
    level: int
    line: int
    raw: str

    @property
    def traces_to(self) -> list[str]:
        """Ids referenced inside the block, excluding its own id."""
        seen: list[str] = []
        for ref in REF_RE.findall(self.raw):
            if ref != self.id and ref not in seen:
                seen.append(ref)
        return seen


@dataclass
class SpecDoc:
    """A parsed requirements.md or design.md."""

    file: str
    frontmatter: dict[str, Any] | None
    blocks: dict[str, DocBlock]
    raw: str
    issues: list[SpecIssue] = field(default_factory=list)


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Split leading YAML frontmatter from ``text``.

    Returns ``(frontmatter_text, body, body_start_line)`` where
    ``body_start_line`` is the 1-based line number of the first body line.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, 1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return frontmatter, body, index + 2
    return None, text, 1


def parse_doc(text: str, file: str | Path) -> SpecDoc:
    """Parse a requirements/design document into id-keyed blocks."""
    file = str(file)
    issues: list[SpecIssue] = []
    frontmatter_text, body, start_line = split_frontmatter(text)

    frontmatter: dict[str, Any] | None = None
    if frontmatter_text is not None:
        try:
            loaded = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            issues.append(SpecIssue(file, 1, None, f"invalid YAML frontmatter: {exc}"))
        else:
            if loaded is None or isinstance(loaded, dict):
                frontmatter = loaded or {}
            else:
                issues.append(
                    SpecIssue(file, 1, None, "frontmatter must be a YAML mapping")
                )

    blocks: dict[str, DocBlock] = {}
    lines = body.splitlines()
    open_block: DocBlock | None = None
    block_lines: list[str] = []
    fences = FenceTracker()

    def close_block() -> None:
        nonlocal open_block, block_lines
        if open_block is not None:
            open_block.raw = "\n".join(block_lines)
            open_block = None
        block_lines = []

    for offset, line in enumerate(lines):
        line_no = start_line + offset
        in_fence = fences.fenced(line)
        heading = None if in_fence else BLOCK_HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            block_id = heading.group(2)
            if open_block is not None and level <= open_block.level:
                close_block()
            if open_block is None:
                if block_id in blocks:
                    issues.append(
                        SpecIssue(file, line_no, None, f"duplicate id {block_id}")
                    )
                open_block = DocBlock(
                    id=block_id,
                    name=heading.group(3) or "",
                    level=level,
                    line=line_no,
                    raw="",
                )
                blocks[block_id] = open_block
                block_lines = [line]
                continue
            issues.append(
                SpecIssue(
                    file,
                    line_no,
                    None,
                    f"id heading {block_id} is nested deeper than open block "
                    f"{open_block.id} and does not start a block",
                )
            )
        elif (
            not in_fence
            and open_block is not None
            and line.startswith("#")
            and re.match(r"^#{1,6}\s", line)
        ):
            level = len(line) - len(line.lstrip("#"))
            if level <= open_block.level:
                close_block()
        if open_block is not None:
            block_lines.append(line)
    close_block()

    return SpecDoc(
        file=file, frontmatter=frontmatter, blocks=blocks, raw=text, issues=issues
    )

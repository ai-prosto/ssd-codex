"""In-place edit of a task's status line in ``tasks.md``.

Design decision D-18: after the commit-worthy state is reached, the status
line of the task is edited in place (``⬜ TODO`` → ``✅ DONE``; without an
emoji — ``TODO`` → ``DONE``), keeping the rest of the line and the whole
file byte-for-byte. The edit joins the same commit as the task's code, so
the mark is visible in the source set and a re-run skips ``DONE`` tasks.
"""

from __future__ import annotations

from pathlib import Path

from sdd_codex.format.tasks import META_RE, TASK_HEADER_RE

DONE_STATUS = "DONE"
DONE_EMOJI = "✅"


def mark_done_in_text(text: str, task_id: str) -> str:
    """Return ``text`` with the status of ``task_id`` set to ``DONE``.

    Everything except the status token (and its emoji, when present) is
    preserved byte-for-byte. Marking an already-``DONE`` task is a no-op.
    Raises :class:`LookupError` when the task or its status line is absent.
    """
    lines = text.splitlines(keepends=True)
    start: int | None = None
    for index, line in enumerate(lines):
        header = TASK_HEADER_RE.match(line.rstrip())
        if header and header.group(1) == task_id:
            start = index
            break
    if start is None:
        raise LookupError(f"task {task_id} not found in tasks.md")

    for index in range(start + 1, len(lines)):
        raw = lines[index]
        ending = ""
        while raw and raw[-1] in "\r\n":
            ending = raw[-1] + ending
            raw = raw[:-1]
        if raw.lstrip().startswith("#"):
            break  # next task or milestone: no status line in between
        meta = META_RE.match(raw)
        if not meta:
            continue
        if meta.group("status").upper() == DONE_STATUS:
            return text
        status_start, status_end = meta.span("status")
        new_line = raw[:status_start] + DONE_STATUS + raw[status_end:]
        if meta.group("status_emoji"):
            emoji_start, emoji_end = meta.span("status_emoji")
            new_line = new_line[:emoji_start] + DONE_EMOJI + new_line[emoji_end:]
        lines[index] = new_line + ending
        return "".join(lines)
    raise LookupError(f"no status line found for task {task_id}")


def mark_task_done(path: str | Path, task_id: str) -> None:
    """BEH-15: put the ``DONE`` mark into ``tasks.md`` itself, in place."""
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    updated = mark_done_in_text(text, task_id)
    if updated != text:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)

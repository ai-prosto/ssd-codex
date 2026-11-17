"""Masking the OpenAI key in everything sdd-codex writes to disk.

The environment is the only place the key lives (D-04); whenever an agent
or a verify command happens to echo it, the value is replaced with ``***``
at write time. This lives in its own module so that both the artifact
writers (:mod:`sdd_codex.artifacts`) and the attempt loop
(:mod:`sdd_codex.task_loop`) can mask without importing each other.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

MASK = "***"


def _default_secrets() -> list[str]:
    return [os.environ.get("OPENAI_API_KEY") or ""]


def mask_secrets(text: str, secrets: Sequence[str] | None = None) -> str:
    """Replace every secret value in ``text`` with ``***``.

    By default the only secret is the ``OPENAI_API_KEY`` environment value —
    the single credential sdd-codex ever handles (D-04).
    """
    if secrets is None:
        secrets = _default_secrets()
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def mask_secret_bytes(data: bytes, secrets: Sequence[str] | None = None) -> bytes:
    """Like :func:`mask_secrets`, but lossless on arbitrary bytes.

    Used for files whose content may not be valid UTF-8: decoding could
    raise (and abort a run mid-masking), so the secret is replaced on the
    raw bytes instead.
    """
    if secrets is None:
        secrets = _default_secrets()
    for secret in secrets:
        if secret:
            data = data.replace(secret.encode("utf-8"), MASK.encode("ascii"))
    return data

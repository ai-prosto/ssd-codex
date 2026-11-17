"""Spend accounting for one run: tokens, money, and the optional stop.

Design decision D-15: ``budget.max_tokens`` and ``budget.max_usd`` live in
the same config as ``run.max_retries`` and both default to ``null`` — the
spend stop is off until explicitly enabled. The tracker sums the usage
reported by every agent call; money is computed from
``budget.prices_per_mtok``. When an *enabled* limit is exceeded the run
stops exactly like the retry stop of D-14, with the reason ``budget``.
The accumulated spend is always written to ``run.jsonl`` and the summary,
even when the limits are off (BEH-21).
"""

from __future__ import annotations

from dataclasses import dataclass

from sdd_codex.config import BudgetConfig

STOP_BUDGET = "budget"


@dataclass(frozen=True)
class Usage:
    """Token usage of one agent call, as the agent reported it."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetTracker:
    """Accumulates usage over a run and answers whether to stop.

    The tracker never raises on spend: :meth:`stop_reason` returns a reason
    only when a limit is configured (non-``null``) *and* exceeded. With both
    limits off it returns ``None`` no matter the spend — but the totals keep
    accumulating for ``run.jsonl`` and the summary.
    """

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self.config = config or BudgetConfig()
        self._usages: list[Usage] = []

    def add(self, usage: Usage) -> None:
        self._usages.append(usage)

    @property
    def total_tokens(self) -> int:
        return sum(usage.total_tokens for usage in self._usages)

    @property
    def total_usd(self) -> float:
        """Money by ``prices_per_mtok``; a model without a price costs 0."""
        total = 0.0
        for usage in self._usages:
            price = self.config.prices_per_mtok.get(usage.model)
            if price is None:
                continue
            total += (
                usage.input_tokens * price["input"]
                + usage.output_tokens * price["output"]
            ) / 1_000_000
        return total

    def stop_reason(self) -> str | None:
        """A human-readable reason to stop, or ``None`` to keep running."""
        max_tokens = self.config.max_tokens
        if max_tokens is not None and self.total_tokens > max_tokens:
            return (
                f"{STOP_BUDGET}: {self.total_tokens} tokens spent, "
                f"budget.max_tokens is {max_tokens}"
            )
        max_usd = self.config.max_usd
        if max_usd is not None and self.total_usd > max_usd:
            return (
                f"{STOP_BUDGET}: ${self.total_usd:.4f} spent, "
                f"budget.max_usd is {max_usd}"
            )
        return None


def drain_usage(tracker: BudgetTracker, *agents: object) -> None:
    """Collect pending usage from agents that report it.

    An agent may expose ``take_usage() -> Iterable[Usage]`` returning the
    usage events accumulated since the previous drain; agents without the
    method (scripted fakes, simple callables) are silently skipped.
    """
    for agent in agents:
        take = getattr(agent, "take_usage", None)
        if callable(take):
            for usage in take():
                tracker.add(usage)

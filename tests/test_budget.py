"""BEH-21: the spend stop fires only when enabled in the config."""

from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from sdd_codex.artifacts import RunArtifacts
from sdd_codex.budget import BudgetTracker, Usage
from sdd_codex.config import BudgetConfig
from sdd_codex.progress import Progress
from sdd_codex.runner import STATUS_NOT_STARTED, STOP_BUDGET, run_tasks


def test_budget_stop_only_when_configured(spec_repo, make_spec_coder, tmp_path):
    """BEH-21 / D-15: with ``max_tokens``/``max_usd`` set the run stops with
    the reason ``budget``; with the limits at ``null`` it never stops on
    spend — but the accumulated spend still lands in run.jsonl and the
    summary."""
    # The tracker alone: limits off (the default) never stop, whatever the
    # spend; an enabled limit stops once exceeded.
    unlimited = BudgetTracker(BudgetConfig())
    unlimited.add(Usage(model="m", input_tokens=10**9, output_tokens=10**9))
    assert unlimited.stop_reason() is None
    assert unlimited.total_tokens == 2 * 10**9  # ...yet the spend is counted

    capped = BudgetTracker(BudgetConfig(max_tokens=100))
    capped.add(Usage(model="m", output_tokens=150))
    assert capped.stop_reason() is not None
    assert "budget" in capped.stop_reason()

    prices = {"m": {"input": 10.0, "output": 30.0}}
    usd = BudgetTracker(BudgetConfig(max_usd=0.5, prices_per_mtok=prices))
    usd.add(Usage(model="m", input_tokens=50_000, output_tokens=10_000))
    assert usd.total_usd == pytest.approx(0.8)  # 50k*10/1M + 10k*30/1M
    assert "budget" in usd.stop_reason()

    # The whole run, limits enabled: the coder reports 1000 tokens per call,
    # so the first task (two calls) exceeds max_tokens=1500 and the run
    # stops with the reason "budget" before the second task.
    config = replace(
        spec_repo.config, budget=BudgetConfig(max_tokens=1500)
    )
    coder = make_spec_coder(usage_per_call=1000)
    report = run_tasks(
        spec_repo.spec_set(),
        config,
        spec_repo.repo,
        coder,
        artifacts=RunArtifacts(tmp_path / "capped"),
        progress=Progress(io.StringIO()),
    )
    assert report.stop_reason == STOP_BUDGET
    assert report.exit_code != 0
    assert report.tasks[1].status == STATUS_NOT_STARTED

    # The same spend with the limits at null: the run never stops, but the
    # accumulated spend is still recorded in run.jsonl and summary.md.
    # (TASK-201 was committed by the capped run above, so this re-run skips
    # it and only TASK-202 spends: two calls x 1000 tokens.)
    coder = make_spec_coder(usage_per_call=1000)
    report = run_tasks(
        spec_repo.spec_set(),
        spec_repo.config,  # default config: both limits null
        spec_repo.repo,
        coder,
        artifacts=RunArtifacts(tmp_path / "unlimited"),
        progress=Progress(io.StringIO()),
    )
    assert report.stop_reason == ""
    assert report.exit_code == 0
    assert report.total_tokens == 2000
    events = [
        json.loads(line)
        for line in (report.run_dir / "run.jsonl").read_text("utf-8").splitlines()
    ]
    run_event = next(e for e in events if e["event"] == "run")
    assert run_event["total_tokens"] == 2000
    summary = (report.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "2000 tokens" in summary

"""BEH-03 / BEH-04: one config file, models per role, limits off by default."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sdd_codex.config import Config, load_config
from sdd_codex.errors import ConfigError


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sdd-codex.config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_models_per_role_and_no_vendor_key(tmp_path: Path) -> None:
    """BEH-03: each role gets its own model; a vendor/provider or any other
    unknown key is a startup error naming the key."""
    path = write_config(
        tmp_path,
        """\
openai:
  models:
    planner: gpt-5.1
    coder: gpt-5.1-codex
    reviewer: gpt-5.1-codex-mini
  reasoning_effort: high
""",
    )
    config = load_config(path)
    assert config.openai.models.planner == "gpt-5.1"
    assert config.openai.models.coder == "gpt-5.1-codex"
    assert config.openai.models.reviewer == "gpt-5.1-codex-mini"
    assert config.openai.models.for_role("reviewer") == "gpt-5.1-codex-mini"
    assert config.openai.reasoning_effort == "high"

    # There is no vendor/provider key anywhere in the schema.
    def field_names(cls: type) -> set[str]:
        names = set()
        for fld in dataclasses.fields(cls):
            names.add(fld.name)
            if dataclasses.is_dataclass(fld.type):
                names |= field_names(fld.type)  # pragma: no cover
        return names

    assert {"vendor", "provider"}.isdisjoint(field_names(Config))

    # A vendor key is refused by name.
    path = write_config(tmp_path, "openai:\n  vendor: openai\n")
    with pytest.raises(ConfigError, match='"openai.vendor"'):
        load_config(path)

    # A provider key at the top level is refused by name.
    path = write_config(tmp_path, "provider: openai\n")
    with pytest.raises(ConfigError, match='"provider"'):
        load_config(path)

    # Any other unknown key is refused by name too.
    path = write_config(tmp_path, "run:\n  max_retriez: 3\n")
    with pytest.raises(ConfigError, match='"run.max_retriez"'):
        load_config(path)


def test_limits_from_config_budget_disabled_by_default(tmp_path: Path) -> None:
    """BEH-04: without a budget section the spend stop is off (limits null)
    while run.max_retries still comes from the file; explicit limits land in
    the run settings."""
    path = write_config(
        tmp_path,
        """\
run:
  max_retries: 5
""",
    )
    config = load_config(path)
    assert config.run.max_retries == 5
    assert config.budget.max_tokens is None
    assert config.budget.max_usd is None
    assert config.budget.stop_enabled is False

    path = write_config(
        tmp_path,
        """\
run:
  max_retries: 2
budget:
  max_tokens: 2000000
  max_usd: 12.5
  prices_per_mtok:
    gpt-5.1-codex: { input: 1.25, output: 10.0 }
""",
    )
    config = load_config(path)
    assert config.run.max_retries == 2
    assert config.budget.max_tokens == 2_000_000
    assert config.budget.max_usd == 12.5
    assert config.budget.stop_enabled is True
    assert config.budget.prices_per_mtok == {
        "gpt-5.1-codex": {"input": 1.25, "output": 10.0}
    }


def test_defaults_without_config_sections(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, "{}\n"))
    assert config.openai.models.planner == "gpt-5.1-codex"
    assert config.run.max_retries == 3
    assert config.verify.selector_runner == "uv run pytest {selectors} -q"
    assert config.paths.spec_dir == "spec"
    assert config.git.commit_template == "{task_id}: {task_name}"


def test_config_file_and_value_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write_config(tmp_path, "run: [unclosed\n"))

    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        load_config(write_config(tmp_path, "- a list\n"))

    with pytest.raises(ConfigError, match='"run" must be a mapping'):
        load_config(write_config(tmp_path, "run: 3\n"))

    with pytest.raises(ConfigError, match='"run.max_retries" must be an integer'):
        load_config(write_config(tmp_path, "run:\n  max_retries: three\n"))

    with pytest.raises(ConfigError, match='"budget.max_usd" must be a number'):
        load_config(write_config(tmp_path, "budget:\n  max_usd: lots\n"))

    with pytest.raises(ConfigError, match='"openai.models.planner" must be a string'):
        load_config(write_config(tmp_path, "openai:\n  models:\n    planner: 5\n"))

    with pytest.raises(ConfigError, match='"paths.context_files" must be a list'):
        load_config(write_config(tmp_path, "paths:\n  context_files: nope\n"))

    with pytest.raises(ConfigError, match='"budget.prices_per_mtok.m"'):
        load_config(
            write_config(tmp_path, "budget:\n  prices_per_mtok:\n    m: {bad: 1}\n")
        )

    config = load_config(write_config(tmp_path, "{}\n"))
    with pytest.raises(ConfigError, match="unknown agent role"):
        config.openai.models.for_role("janitor")

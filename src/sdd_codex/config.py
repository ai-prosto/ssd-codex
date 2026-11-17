"""Reading and validating ``sdd-codex.config.yaml`` — the single config file.

Design decision D-03: all settings live in one YAML file at the repository
root. A model is set per role (``planner`` / ``coder`` / ``reviewer``); there
is no vendor or provider key anywhere in the schema — OpenAI is hard-coded in
the code. Any unknown key is a startup error naming that key, so the config
cannot silently sprawl.

Design decision D-15: ``budget.max_tokens`` and ``budget.max_usd`` default to
``null`` — the spend stop is off until explicitly enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sdd_codex.errors import ConfigError

DEFAULT_CONFIG_FILENAME = "sdd-codex.config.yaml"

DEFAULT_MODEL = "gpt-5.1-codex"

ROLES = ("planner", "coder", "reviewer")


@dataclass(frozen=True)
class ModelsConfig:
    planner: str = DEFAULT_MODEL
    coder: str = DEFAULT_MODEL
    reviewer: str = DEFAULT_MODEL

    def for_role(self, role: str) -> str:
        if role not in ROLES:
            raise ConfigError(f"unknown agent role {role!r} (expected one of {ROLES})")
        return getattr(self, role)


@dataclass(frozen=True)
class OpenAIConfig:
    models: ModelsConfig = field(default_factory=ModelsConfig)
    reasoning_effort: str = "medium"


@dataclass(frozen=True)
class RunConfig:
    max_retries: int = 3
    attempt_timeout_sec: int = 1800


@dataclass(frozen=True)
class VerifyConfig:
    selector_runner: str = "uv run pytest {selectors} -q"
    timeout_sec: int = 900


@dataclass(frozen=True)
class BudgetConfig:
    max_tokens: int | None = None
    max_usd: float | None = None
    prices_per_mtok: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def stop_enabled(self) -> bool:
        """The spend stop is active only when a limit is explicitly set."""
        return self.max_tokens is not None or self.max_usd is not None


@dataclass(frozen=True)
class PathsConfig:
    spec_dir: str = "spec"
    artifacts_dir: str = ".sdd-codex/runs"
    context_files: tuple[str, ...] = ("spec/requirements.md", "spec/design.md")


@dataclass(frozen=True)
class GitConfig:
    commit_template: str = "{task_id}: {task_name}"


@dataclass(frozen=True)
class Config:
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    run: RunConfig = field(default_factory=RunConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    git: GitConfig = field(default_factory=GitConfig)


def load_config(path: str | Path) -> Config:
    """Load and validate the config file; raise :class:`ConfigError` on any problem."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: config must be a YAML mapping")
    return parse_config(data, source=str(path))


def parse_config(data: dict[str, Any], source: str = DEFAULT_CONFIG_FILENAME) -> Config:
    """Build a :class:`Config` from a mapping, rejecting unknown keys by name."""
    root = _Section(data, source, prefix="")
    openai_section = root.section("openai")
    models_section = openai_section.section("models")
    models = ModelsConfig(
        planner=models_section.string("planner", DEFAULT_MODEL),
        coder=models_section.string("coder", DEFAULT_MODEL),
        reviewer=models_section.string("reviewer", DEFAULT_MODEL),
    )
    models_section.finish()
    openai = OpenAIConfig(
        models=models,
        reasoning_effort=openai_section.string("reasoning_effort", "medium"),
    )
    openai_section.finish()

    run_section = root.section("run")
    run = RunConfig(
        max_retries=run_section.integer("max_retries", 3),
        attempt_timeout_sec=run_section.integer("attempt_timeout_sec", 1800),
    )
    run_section.finish()

    verify_section = root.section("verify")
    verify = VerifyConfig(
        selector_runner=verify_section.string(
            "selector_runner", "uv run pytest {selectors} -q"
        ),
        timeout_sec=verify_section.integer("timeout_sec", 900),
    )
    verify_section.finish()

    budget_section = root.section("budget")
    budget = BudgetConfig(
        max_tokens=budget_section.optional_integer("max_tokens"),
        max_usd=budget_section.optional_number("max_usd"),
        prices_per_mtok=budget_section.prices("prices_per_mtok"),
    )
    budget_section.finish()

    paths_section = root.section("paths")
    paths = PathsConfig(
        spec_dir=paths_section.string("spec_dir", "spec"),
        artifacts_dir=paths_section.string("artifacts_dir", ".sdd-codex/runs"),
        context_files=tuple(
            paths_section.string_list(
                "context_files", ["spec/requirements.md", "spec/design.md"]
            )
        ),
    )
    paths_section.finish()

    git_section = root.section("git")
    git = GitConfig(
        commit_template=git_section.string("commit_template", "{task_id}: {task_name}")
    )
    git_section.finish()

    root.finish()
    return Config(
        openai=openai, run=run, verify=verify, budget=budget, paths=paths, git=git
    )


class _Section:
    """One mapping level of the config; tracks consumed keys."""

    def __init__(self, data: dict[str, Any], source: str, prefix: str) -> None:
        self._data = data
        self._source = source
        self._prefix = prefix
        self._consumed: set[str] = set()

    def _path(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _take(self, key: str) -> Any:
        self._consumed.add(key)
        return self._data.get(key)

    def section(self, key: str) -> _Section:
        value = self._take(key)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be a mapping"
            )
        return _Section(value, self._source, prefix=f"{self._path(key)}.")

    def string(self, key: str, default: str) -> str:
        value = self._take(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be a string"
            )
        return value

    def integer(self, key: str, default: int) -> int:
        value = self._take(key)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be an integer"
            )
        return value

    def optional_integer(self, key: str) -> int | None:
        value = self._take(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be an integer or null"
            )
        return value

    def optional_number(self, key: str) -> float | None:
        value = self._take(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be a number or null"
            )
        return float(value)

    def string_list(self, key: str, default: list[str]) -> list[str]:
        value = self._take(key)
        if value is None:
            return list(default)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be a list of strings"
            )
        return value

    def prices(self, key: str) -> dict[str, dict[str, float]]:
        value = self._take(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(
                f"{self._source}: key \"{self._path(key)}\" must be a mapping"
            )
        prices: dict[str, dict[str, float]] = {}
        for model, price in value.items():
            if (
                not isinstance(price, dict)
                or set(price) - {"input", "output"}
                or not all(
                    isinstance(amount, (int, float)) and not isinstance(amount, bool)
                    for amount in price.values()
                )
            ):
                raise ConfigError(
                    f"{self._source}: key \"{self._path(key)}.{model}\" must be a "
                    'mapping with numeric "input" and "output"'
                )
            prices[str(model)] = {name: float(amount) for name, amount in price.items()}
        return prices

    def finish(self) -> None:
        unknown = sorted(set(self._data) - self._consumed)
        if unknown:
            names = ", ".join(f'"{self._path(key)}"' for key in unknown)
            raise ConfigError(f"{self._source}: unknown config key {names}")

"""sdd-codex — a spec-driven task runner for Codex CLI agents.

This package parses spec sets in the spec-runner markdown format and will
execute their tasks through coding agents run as CLI subprocesses.
"""

from sdd_codex.config import Config, load_config, parse_config
from sdd_codex.errors import (
    CliError,
    ConfigError,
    PlannerError,
    SpecFormatError,
    SpecIssue,
)
from sdd_codex.specset import SpecSet, load_spec_set, validate_spec_set

__all__ = [
    "CliError",
    "Config",
    "ConfigError",
    "PlannerError",
    "SpecFormatError",
    "SpecIssue",
    "SpecSet",
    "load_config",
    "load_spec_set",
    "parse_config",
    "validate_spec_set",
]

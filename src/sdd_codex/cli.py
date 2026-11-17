"""Command-line entry point: one binary, independent subcommands.

Design decision D-02: creating a spec and running it are two separate
subcommands of one CLI. ``sdd-codex spec`` finishes right after writing the
three spec files and executes nothing; execution starts only with
``sdd-codex run``. There is no shared state between them — they communicate
only through files on disk.

``validate`` parses a ready set and prints format errors without any API
call; ``doctor`` (D-20) reports what is missing from the environment before
the first paid call.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from sdd_codex.config import DEFAULT_CONFIG_FILENAME, Config, load_config
from sdd_codex.errors import CliError, ConfigError, PlannerError, SpecFormatError
from sdd_codex.planner import CodexCliPlanner, Planner, plan_spec
from sdd_codex.planner.codex import CODEX_BINARY
from sdd_codex.specset import load_spec_set, validate_spec_set

PROG = "sdd-codex"

DESCRIPTION_HINT = (
    "a program description is required: pass it as an argument "
    f'({PROG} spec "<description>") or with --from-file path.md'
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Spec-driven task runner for Codex CLI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    spec = subparsers.add_parser(
        "spec",
        help="let the planner create the three spec files, then exit",
    )
    spec.add_argument(
        "description",
        nargs="?",
        default=None,
        help="program description (alternatively use --from-file)",
    )
    spec.add_argument(
        "--from-file",
        dest="from_file",
        metavar="path.md",
        help="read the program description from a file",
    )
    spec.add_argument("--config", help="path to sdd-codex.config.yaml")
    spec.set_defaults(handler=_cmd_spec)

    run = subparsers.add_parser(
        "run",
        help="execute tasks from a ready spec set",
    )
    run.add_argument("--task", help="run only this task id (e.g. TASK-003)")
    run.add_argument("--config", help="path to sdd-codex.config.yaml")
    run.set_defaults(handler=_cmd_run)

    validate = subparsers.add_parser(
        "validate",
        help="parse the spec set and print format errors, no API calls",
    )
    validate.add_argument("--config", help="path to sdd-codex.config.yaml")
    validate.set_defaults(handler=_cmd_validate)

    doctor = subparsers.add_parser(
        "doctor",
        help="check codex in PATH, the API key in the environment, git repo",
    )
    doctor.add_argument("--config", help="path to sdd-codex.config.yaml")
    doctor.set_defaults(handler=_cmd_doctor)

    return parser


def _load_cli_config(config_arg: str | None) -> Config:
    """Load the config named on the command line, or defaults when absent."""
    if config_arg is not None:
        return load_config(config_arg)
    default = Path(DEFAULT_CONFIG_FILENAME)
    if default.is_file():
        return load_config(default)
    return Config()


def _resolve_description(args: argparse.Namespace) -> str:
    """BEH-05: the description comes as an argument or from a file, not both."""
    if args.description is not None and args.from_file is not None:
        raise CliError(
            "pass the description either as an argument or with --from-file, not both",
            exit_code=2,
        )
    if args.from_file is not None:
        path = Path(args.from_file)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError(f"cannot read --from-file {path}: {exc}") from exc
    elif args.description is not None:
        text = args.description
    else:
        raise CliError(DESCRIPTION_HINT, exit_code=2)
    description = text.strip()
    if not description:
        raise CliError(DESCRIPTION_HINT, exit_code=2)
    return description


def _cmd_spec(args: argparse.Namespace, planner: Planner | None) -> int:
    """BEH-06/BEH-07: write exactly three validated files, execute nothing."""
    description = _resolve_description(args)
    config = _load_cli_config(args.config)
    if planner is None:
        planner = CodexCliPlanner(timeout_sec=config.run.attempt_timeout_sec)
    spec_dir = plan_spec(description, config, planner)
    for name in ("requirements.md", "design.md", "tasks.md"):
        print(f"wrote {spec_dir / name}")
    print(f"spec set created in {spec_dir}; run '{PROG} run' to execute it")
    return 0


def _cmd_run(args: argparse.Namespace, planner: Planner | None) -> int:
    """Execute a ready set (D-08). Execution itself lands with TASK-003..005."""
    config = _load_cli_config(args.config)
    load_spec_set(config.paths.spec_dir)
    raise CliError(
        "the spec set parsed cleanly, but task execution is not implemented "
        "yet (coming with TASK-003..TASK-005)"
    )


def _cmd_validate(args: argparse.Namespace, planner: Planner | None) -> int:
    config = _load_cli_config(args.config)
    issues = validate_spec_set(config.paths.spec_dir)
    if issues:
        for issue in issues:
            print(str(issue), file=sys.stderr)
        return 1
    print(f"spec set in {config.paths.spec_dir!r} is valid")
    return 0


def _git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _cmd_doctor(args: argparse.Namespace, planner: Planner | None) -> int:
    """D-20: one screen saying what is missing, before any paid call."""
    config_error: str | None = None
    try:
        _load_cli_config(args.config)
    except ConfigError as exc:
        config_error = str(exc)
    checks = [
        (
            f"'{CODEX_BINARY}' CLI in PATH",
            shutil.which(CODEX_BINARY) is not None,
            f"install the codex CLI so {PROG} can spawn agent subprocesses",
        ),
        (
            "OPENAI_API_KEY in environment",
            bool(os.environ.get("OPENAI_API_KEY")),
            "export OPENAI_API_KEY (or configure a codex session)",
        ),
        (
            "inside a git repository",
            _git_root(Path.cwd()) is not None,
            f"run {PROG} from a git work tree (commits go to the current branch)",
        ),
        (
            f"{args.config or DEFAULT_CONFIG_FILENAME} loads",
            config_error is None,
            config_error or "",
        ),
    ]
    all_ok = True
    for name, ok, remedy in checks:
        mark = "ok" if ok else "MISSING"
        print(f"[{mark:>7}] {name}")
        if not ok:
            all_ok = False
            print(f"          -> {remedy}")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None, *, planner: Planner | None = None) -> int:
    """Run the CLI; returns the process exit code.

    ``planner`` lets tests substitute the agent; ``None`` means the real
    Codex CLI planner.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args, planner)
    except CliError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return exc.exit_code
    except (ConfigError, SpecFormatError, PlannerError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `| head`); exit without a traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

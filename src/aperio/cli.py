"""Aperio command-line interface.

Commands:
    aperio --version                print the version and exit
    aperio init <project-directory>   initialize (or safely repair) a project
    aperio status <project-directory> report project state (read-only)

Exit codes:
    0  success
    1  operational error (unsafe path, failed init, unreadable state, ...)
    2  usage error (handled by argparse)
"""

from __future__ import annotations

import argparse
import sys

from aperio import __version__
from aperio.errors import AperioError
from aperio.project import (
    APERIO_DIR_NAME,
    InitResult,
    StatusReport,
    formatted_schema_version,
    init_project,
    status_project,
)

_PROG = "aperio"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Aperio: model-agnostic, multi-agent software development "
            "orchestrator (Feature 01: project foundation)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{_PROG} {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="initialize an Aperio project (creates .aperio/); safe to re-run",
    )
    init_parser.add_argument(
        "project_directory", help="directory in which to create the project"
    )

    status_parser = subparsers.add_parser(
        "status",
        help="report project state (read-only)",
    )
    status_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )

    return parser


def _print_init(result: InitResult) -> None:
    if result.already_initialized:
        assert result.state is not None
        print(
            f"Aperio project already initialized at {result.target} "
            f"(schema v{result.state.schema_version})"
        )
    else:
        print(f"Aperio project initialized at {result.target}")

    for rel in result.created:
        print(f"  created  {rel}")
    for rel in result.present:
        print(f"  present  {rel}")
    if result.already_initialized and not result.created:
        print("  nothing to do: all context files present, nothing overwritten")


def _print_status(report: StatusReport) -> None:
    state = report.state
    print(f"Project:     {state.name}")
    print(f"Location:    {report.target}")
    print(f"Schema:      {formatted_schema_version(state)}")
    print(f"Phase:       {state.phase}")
    print(f"Created:     {state.created_at}")
    print(f"Updated:     {state.updated_at}")
    print(f"aperio:      {state.aperio_version}")
    print()
    print("Context files:")
    for rel, exists in report.files.items():
        marker = "present" if exists else "MISSING"
        print(f"  {marker:<8} {APERIO_DIR_NAME}/{rel}")
    missing = [rel for rel, exists in report.files.items() if not exists]
    if missing:
        print()
        print(
            "Some context files are missing; "
            f"'aperio init {report.target}' can repair them without overwriting."
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            _print_init(init_project(args.project_directory))
        elif args.command == "status":
            _print_status(status_project(args.project_directory))
        else:  # pragma: no cover - argparse enforces required subcommand
            parser.error(f"unknown command: {args.command!r}")
    except AperioError as exc:
        print(f"{_PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code

    return 0

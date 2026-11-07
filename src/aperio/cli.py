"""Aperio command-line interface.

Commands:
    aperio --version                  print the version and exit
    aperio init <dir>                 initialize (or safely repair) a project
    aperio status <dir>               report project state (read-only)
    aperio memory <dir> [--json]      read portable project memory (Feature 02)
    aperio decision-add <dir> ...     append a decision (append-only)
    aperio handoff-write <dir> --file <handoff.json>
                                      replace the handoff with a validated one
    aperio phase-set <dir> <phase>    update the project phase (compare-and-swap)

Exit codes:
    0  success
    1  operational error (unsafe path, failed init, unreadable state, ...)
    2  usage error (handled by argparse)
"""

from __future__ import annotations

import argparse
import json
import sys

from aperio import __version__
from aperio.decisions import append_decision
from aperio.errors import AperioError, ValidationError
from aperio.handoff import (
    HANDOFF_FIELDS,
    HandoffContent,
    write_handoff,
)
from aperio.inputs import read_json_fields
from aperio.memory import load_context
from aperio.paths import resolve_target
from aperio.progress import update_phase
from aperio.project import (
    APERIO_DIR_NAME,
    InitResult,
    StatusReport,
    aperio_dir_for,
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
            "orchestrator (Feature 01: project foundation; Feature 02: "
            "portable agent memory and handoff)."
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

    memory_parser = subparsers.add_parser(
        "memory",
        help="read portable project memory: state, brief, decisions, handoff",
    )
    memory_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    memory_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full context as machine-readable JSON",
    )

    decision_parser = subparsers.add_parser(
        "decision-add",
        help="append one decision to .aperio/decisions.md (append-only)",
    )
    decision_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    decision_parser.add_argument(
        "--file",
        help="JSON file with title/context/decision/consequences "
        "(alternative to the individual flags)",
    )
    decision_parser.add_argument("--title", help="single-line decision title")
    decision_parser.add_argument("--context", help="why this decision came up")
    decision_parser.add_argument("--decision", help="what was decided")
    decision_parser.add_argument(
        "--consequences", help="what this commits the project to"
    )

    handoff_parser = subparsers.add_parser(
        "handoff-write",
        help="replace .aperio/handoff.md with a validated structured handoff",
    )
    handoff_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    handoff_parser.add_argument(
        "--file",
        required=True,
        help=f"JSON file with the fields: {', '.join(HANDOFF_FIELDS)}",
    )

    phase_parser = subparsers.add_parser(
        "phase-set",
        help="update the project phase (compare-and-swap, lost-update safe)",
    )
    phase_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    phase_parser.add_argument("phase", help="new project phase (single line)")

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


def _print_memory(context, as_json: bool) -> None:
    if as_json:
        print(json.dumps(context.to_dict(), indent=2, ensure_ascii=False))
        return
    state = context.state
    decision_count = len(context.decisions) if context.decisions is not None else 0
    print(f"Project:     {state.name}")
    print(f"Location:    {context.target}")
    print(f"Schema:      {formatted_schema_version(state)}")
    print(f"Phase:       {state.phase}")
    print(f"Updated:     {state.updated_at}")
    print(f"Decisions:   {decision_count}")
    if context.handoff is None:
        handoff_state = "MISSING"
    elif context.handoff.structured:
        handoff_state = "structured v1"
    elif context.handoff.structured is None:
        handoff_state = f"newer format v{context.handoff.version} (raw text preserved)"
    else:
        handoff_state = "legacy free-form"
    print(f"Handoff:     {handoff_state}")
    if context.missing:
        print(f"Missing:     {', '.join(context.missing)} (run 'aperio init' to repair)")
    if context.malformed_entries:
        print(
            f"Damaged:     {len(context.malformed_entries)} malformed decision "
            "entries (reported, never fatal - see docs/architecture.md)"
        )
    print()
    print("Full machine-readable context: aperio memory <dir> --json")


def _cmd_decision_add(args) -> None:
    flags_used = [
        name
        for name in ("title", "context", "decision", "consequences")
        if getattr(args, name) is not None
    ]
    if args.file is not None and flags_used:
        raise ValidationError(
            "decision-add: --file and the individual flags are mutually "
            f"exclusive (flags given: {', '.join('--' + f for f in flags_used)})"
        )
    if args.file is not None:
        fields = read_json_fields(
            args.file,
            ("title", "context", "decision", "consequences"),
        )
    else:
        missing = [
            name
            for name in ("title", "context", "decision", "consequences")
            if getattr(args, name) is None
        ]
        if missing:
            raise ValidationError(
                "decision-add: provide --file or all four flags "
                f"(missing: {', '.join('--' + name for name in missing)})"
            )
        fields = {
            "title": args.title,
            "context": args.context,
            "decision": args.decision,
            "consequences": args.consequences,
        }
    target = resolve_target(args.project_directory, for_writing=True)
    entry = append_decision(
        aperio_dir_for(target),
        title=fields["title"],
        context=fields["context"],
        decision=fields["decision"],
        consequences=fields["consequences"],
    )
    print(f"appended {entry.id} to {APERIO_DIR_NAME}/decisions.md")


def _cmd_handoff_write(args) -> None:
    fields = read_json_fields(args.file, HANDOFF_FIELDS)
    target = resolve_target(args.project_directory, for_writing=True)
    write_handoff(aperio_dir_for(target), HandoffContent(**fields))
    print(f"handoff written to {APERIO_DIR_NAME}/handoff.md (7 sections validated)")


def _cmd_phase_set(args) -> None:
    result = update_phase(args.project_directory, args.phase)
    if result.updated:
        print(f"phase: {result.previous_phase} -> {result.new_phase}")
    else:
        print(f"phase unchanged: {result.new_phase}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            _print_init(init_project(args.project_directory))
        elif args.command == "status":
            _print_status(status_project(args.project_directory))
        elif args.command == "memory":
            _print_memory(load_context(args.project_directory), args.as_json)
        elif args.command == "decision-add":
            _cmd_decision_add(args)
        elif args.command == "handoff-write":
            _cmd_handoff_write(args)
        elif args.command == "phase-set":
            _cmd_phase_set(args)
        else:  # pragma: no cover - argparse enforces required subcommand
            parser.error(f"unknown command: {args.command!r}")
    except AperioError as exc:
        print(f"{_PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code

    return 0

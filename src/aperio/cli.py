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
from pathlib import Path

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
from aperio.planmodel import ready_task_ids
from aperio.planstore import (
    approve_plan,
    create_plan,
    plan_exists,
    plan_summary,
    read_plan,
    revise_plan,
    update_task,
)
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
        help=f"JSON file with the fields: {', '.join(HANDOFF_FIELDS)}; "
        'optionally "revises": <current revision> to guard against '
        "competing writers",
    )
    handoff_parser.add_argument(
        "--force",
        action="store_true",
        help="deliberately replace the current handoff without a revision "
        "declaration (discards its content, including unreadable newer "
        "formats)",
    )

    phase_parser = subparsers.add_parser(
        "phase-set",
        help="update the project phase (compare-and-swap, lost-update safe)",
    )
    phase_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    phase_parser.add_argument("phase", help="new project phase (single line)")

    plan_create_parser = subparsers.add_parser(
        "plan-create",
        help="create the project plan from a structured JSON file (draft, revision 1)",
    )
    plan_create_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_create_parser.add_argument(
        "--file",
        required=True,
        help="JSON file with the plan content (objective, expected_outcome, "
        "scope, exclusions, assumptions, risks, strategy, "
        "acceptance_criteria, verification_strategy, tasks)",
    )

    plan_show_parser = subparsers.add_parser(
        "plan-show",
        help="show the current plan, its approval state, and task readiness",
    )
    plan_show_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_show_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the full plan document as machine-readable JSON",
    )

    plan_approve_parser = subparsers.add_parser(
        "plan-approve",
        help="record plan approval (operational marker after genuine review)",
    )
    plan_approve_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_approve_parser.add_argument(
        "--revision",
        type=int,
        help="approve this specific plan_version (default: the current one)",
    )
    plan_approve_parser.add_argument(
        "--by",
        help="label recording who approved (not cryptographic proof)",
    )

    plan_revise_parser = subparsers.add_parser(
        "plan-revise",
        help="replace the plan content with a new draft revision",
    )
    plan_revise_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    plan_revise_parser.add_argument(
        "--file",
        required=True,
        help="JSON file with the new plan content (lifecycle state of "
        "surviving tasks is carried over automatically)",
    )
    plan_revise_parser.add_argument(
        "--revises",
        type=int,
        required=True,
        help="the plan_version being revised (stale revisions are refused)",
    )

    task_list_parser = subparsers.add_parser(
        "task-list",
        help="list plan tasks with their status and derived readiness",
    )
    task_list_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    task_list_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit tasks with derived readiness as machine-readable JSON",
    )

    task_status_parser = subparsers.add_parser(
        "task-status",
        help="perform a validated task-status transition (execution tracking)",
    )
    task_status_parser.add_argument(
        "project_directory", help="directory of an initialized Aperio project"
    )
    task_status_parser.add_argument("task_id", help="task id, e.g. TASK-001")
    task_status_parser.add_argument(
        "--status",
        help="new status: pending (unblock), in-progress (start/reopen), "
        "blocked, or completed (required unless --file provides it)",
    )
    task_status_parser.add_argument(
        "--file",
        help="JSON file with the optional payload fields (owner, reason, "
        "evidence, limitations, result) - alternative to the flags",
    )
    task_status_parser.add_argument("--owner", help="agent claiming the task (required to start)")
    task_status_parser.add_argument("--reason", help="reason (required to block)")
    task_status_parser.add_argument("--evidence", help="verification evidence (required to complete)")
    task_status_parser.add_argument(
        "--limitations", help="verification limitations (required to complete)"
    )
    task_status_parser.add_argument(
        "--result", help="verification result: passed or failed (completion requires passed)"
    )
    task_status_parser.add_argument(
        "--reopen",
        action="store_true",
        help="deliberately reopen a completed task (clears its verification "
        "block - record durable history with 'aperio decision-add' first)",
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
    fields = read_json_fields(
        args.file, HANDOFF_FIELDS, optional_int_fields=("revises",)
    )
    revises = fields.pop("revises", None)
    target = resolve_target(args.project_directory, for_writing=True)
    revision = write_handoff(
        aperio_dir_for(target),
        HandoffContent(**fields),
        expected_revision=revises,
        force=args.force,
    )
    print(
        f"handoff written to {APERIO_DIR_NAME}/handoff.md "
        f"(7 sections validated, revision {revision})"
        + (" [forced]" if args.force else "")
    )


def _cmd_phase_set(args) -> None:
    result = update_phase(args.project_directory, args.phase)
    if result.updated:
        print(f"phase: {result.previous_phase} -> {result.new_phase}")
    else:
        print(f"phase unchanged: {result.new_phase}")


def _cmd_plan_create(args) -> None:
    content = _read_plan_input(args.file)
    target = resolve_target(args.project_directory, for_writing=True)
    document = create_plan(aperio_dir_for(target), content)
    print(
        f"plan created at {APERIO_DIR_NAME}/plan.json "
        f"(revision {document['plan_version']}, draft; "
        f"{len(document['tasks'])} task(s)). Execution tracking requires "
        "approval: aperio plan-approve"
    )


def _cmd_plan_show(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    if args.as_json:
        print(json.dumps(read_plan(aperio_dir), indent=2, ensure_ascii=False))
        return
    summary = plan_summary(aperio_dir)
    if summary is None:
        print("No plan in this project; create one with 'aperio plan-create'.")
        return
    if summary.get("unsupported_schema"):
        print(
            f"Plan present but written in unsupported plan-schema version "
            f"{summary.get('schema_version')}; aperio cannot display or "
            "modify it. Inspect .aperio/plan.json manually."
        )
        return
    print(f"Plan:        revision {summary['plan_version']} ({summary['approval_status']})")
    if summary["approval_status"] == "approved":
        print(f"Approved:    revision {summary['approved_revision']} (provenance stamp - see docs)")
    else:
        print("Approval:    required before execution tracking")
    print(f"Objective:   {summary['objective']}")
    print(f"Tasks:       {summary['task_counts']}")
    print(f"Ready:       {', '.join(summary['ready_task_ids']) or '(none)'}")
    if summary["blocked_task_ids"]:
        print(f"Blocked:     {', '.join(summary['blocked_task_ids'])}")
    print()
    print("Full document: aperio plan-show <dir> --json")


def _cmd_plan_approve(args) -> None:
    target = resolve_target(args.project_directory, for_writing=True)
    document = approve_plan(
        aperio_dir_for(target), revision=args.revision, by=args.by
    )
    approval = document["approval"]
    print(
        f"plan approval recorded: revision {approval['approved_revision']} "
        f"approved at {approval['approved_at']}"
        + (f" by {approval['approved_by']}" if approval["approved_by"] else "")
        + " (operational marker; genuine human review is the operator's "
        "responsibility)"
    )


def _cmd_plan_revise(args) -> None:
    content = _read_plan_input(args.file)
    target = resolve_target(args.project_directory, for_writing=True)
    document = revise_plan(aperio_dir_for(target), content, revises=args.revises)
    print(
        f"plan revised: now revision {document['plan_version']} "
        f"({len(document['tasks'])} task(s); approval reset to draft - "
        "renewed approval required before execution tracking)"
    )


def _cmd_task_list(args) -> None:
    target = resolve_target(args.project_directory, for_writing=False)
    aperio_dir = aperio_dir_for(target)
    if not plan_exists(aperio_dir):
        print("No plan in this project; create one with 'aperio plan-create'.")
        return
    plan = read_plan(aperio_dir)
    approved = plan["approval"]["status"] == "approved"
    ready = set(ready_task_ids(plan))  # dependency-ready
    executable = ready if approved else set()
    if args.as_json:
        listing = [
            dict(
                task,
                is_ready=task["id"] in ready,
                is_executable=task["id"] in executable,
            )
            for task in plan["tasks"]
        ]
        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return
    if not approved:
        print(
            "Plan approval: DRAFT - dependency-ready tasks are NOT "
            "executable until the plan is approved (aperio plan-approve)."
        )
    for task in sorted(plan["tasks"], key=lambda t: t["id"]):
        if task["id"] in executable:
            marker = "ready"
        elif task["id"] in ready:
            marker = "dep-ready"
        else:
            marker = f"{task['status']:>11}"
        owner = f" (owner: {task['owner']})" if task["owner"] else ""
        print(f"  [{marker}] {task['id']}: {task['title']}{owner}")


def _read_plan_input(path: str) -> dict:
    """Read a plan create/revise input file: strict content validation
    happens in the model layer; here we only parse the JSON file."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read plan input file {path}: {exc.strerror or exc}"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            f"plan input file {path} is not valid UTF-8; re-save it as UTF-8"
        ) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"plan input file {path} is not valid JSON (line {exc.lineno}: {exc.msg})"
        ) from None


def _cmd_task_status(args) -> None:
    if args.file is not None:
        payload = read_json_fields(
            args.file,
            ("status",),
            optional_str_fields=(
                "owner",
                "reason",
                "evidence",
                "limitations",
                "result",
            ),
        )
        flag_values = [args.owner, args.reason, args.evidence, args.limitations, args.result]
        if any(value is not None for value in flag_values):
            raise ValidationError(
                "task-status: --file and the individual payload flags are "
                "mutually exclusive"
            )
        status = payload.pop("status")
        if args.status is not None and args.status != status:
            raise ValidationError(
                "task-status: --status and the payload file's 'status' disagree"
            )
        values = payload
    else:
        if args.status is None:
            raise ValidationError(
                "task-status: --status is required (or provide --file with "
                "a 'status' field)"
            )
        status = args.status
        values = {
            "owner": args.owner,
            "reason": args.reason,
            "evidence": args.evidence,
            "limitations": args.limitations,
            "result": args.result,
        }
    target = resolve_target(args.project_directory, for_writing=True)
    if args.reopen and status != "in-progress":
        raise ValidationError(
            "task-status: --reopen is only valid with --status in-progress "
            "(it deliberately reopens a completed task)"
        )
    if args.reopen:
        try:
            previous = read_plan(aperio_dir_for(target))
        except StateError:
            previous = None
        if previous is not None:
            prior = next(
                (t for t in previous["tasks"] if t["id"] == args.task_id), None
            )
            if prior is not None and prior["status"] == "completed":
                verification = prior["verification"]
                print(
                    "REOPEN: superseded verification of "
                    f"{args.task_id} - evidence: "
                    f"{verification.get('evidence')!r}; limitations: "
                    f"{verification.get('limitations')!r}; result: "
                    f"{verification.get('result')!r}; verified_at: "
                    f"{verification.get('verified_at')!r}. Record durable "
                    "history with 'aperio decision-add' if needed."
                )
    task = update_task(
        aperio_dir_for(target),
        args.task_id,
        new_status=status,
        owner=values.get("owner"),
        reason=values.get("reason"),
        evidence=values.get("evidence"),
        limitations=values.get("limitations"),
        result=values.get("result"),
        reopen=args.reopen,
    )
    verification = task["verification"]
    extra = ""
    if task["status"] == "completed":
        extra = f" (verified {verification['verified_at']})"
    elif verification["result"] == "failed":
        extra = " (failed verification recorded)"
    owner = f", owner: {task['owner']}" if task["owner"] else ""
    print(f"task {task['id']}: {task['status']}{owner}{extra}")


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
        elif args.command == "plan-create":
            _cmd_plan_create(args)
        elif args.command == "plan-show":
            _cmd_plan_show(args)
        elif args.command == "plan-approve":
            _cmd_plan_approve(args)
        elif args.command == "plan-revise":
            _cmd_plan_revise(args)
        elif args.command == "task-list":
            _cmd_task_list(args)
        elif args.command == "task-status":
            _cmd_task_status(args)
        else:  # pragma: no cover - argparse enforces required subcommand
            parser.error(f"unknown command: {args.command!r}")
    except AperioError as exc:
        print(f"{_PROG}: error: {exc}", file=sys.stderr)
        return exc.exit_code

    return 0

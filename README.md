# aperio-next

**Aperio** is a model-agnostic, multi-agent software development orchestrator.
This repository contains **Features 01–03**: the project foundation
(bootstrap and inspect portable project state), portable agent memory
(read shared context, record decisions, hand off work), and project
planning with a dependency-validated task graph and evidence-based
execution tracking.

Anything not listed below is **planned, not implemented**. See
[docs/roadmap.md](docs/roadmap.md) for what is coming and
[docs/architecture.md](docs/architecture.md) for design boundaries.

## Status

- Implemented (Feature 01): `aperio --version`, `aperio init <project-directory>`, `aperio status <project-directory>`
- Implemented (Feature 02): `aperio memory [--json]`, `aperio decision-add`, `aperio handoff-write`, `aperio phase-set`
- Implemented (Feature 03): `aperio plan-create`, `aperio plan-show [--json]`, `aperio plan-approve`, `aperio plan-revise`, `aperio task-list [--json]`, `aperio task-status`
- Not implemented (by design, yet): agent execution/orchestration, autonomous coding, project interviews, agent integrations

## Requirements

- Python 3.11+
- No third-party runtime dependencies (standard library only)

## Install (development)

```sh
python -m pip install -e ".[dev]"
```

## Usage

```sh
# Show the installed version
aperio --version

# Initialize an Aperio project in a directory (creates .aperio/)
aperio init ./my-project

# Report project state; strictly read-only
aperio status ./my-project

# Read the full shareable context (state, brief, decisions, handoff)
aperio memory ./my-project           # human summary
aperio memory ./my-project --json    # machine-readable, stable schema

# Record a decision (append-only; prints the assigned DEC-NNNN id)
aperio decision-add ./my-project --title "Use stdlib only" \
  --context "Dependencies add supply-chain risk" \
  --decision "Standard library only at runtime" \
  --consequences "More hand-written code for some tasks"
# ...or from a JSON file with title/context/decision/consequences:
aperio decision-add ./my-project --file decision.json

# Hand off work: replaces .aperio/handoff.md with a validated structured
# handoff from a JSON file with the seven required fields
# (current_objective, completed_work, work_in_progress,
#  next_recommended_action, blockers, verification_evidence,
#  verification_limitations)
aperio handoff-write ./my-project --file handoff.json

# Competing agents: when the current handoff is at revision N (shown in
# its marker and in `memory --json`), declare it to replace it — a stale
# declaration is refused instead of silently overwriting newer work:
#   { ..., "revises": 3 }
# Deliberate replacement without a declaration (discards the current
# handoff, including unreadable newer formats) is opt-in:
aperio handoff-write ./my-project --file handoff.json --force

# Update the project phase (serialized under a coordination lock, with a
# compare-and-swap: competing aperio updates cannot silently overwrite
# each other; non-cooperating tools are detected before publishing, though
# the filesystem cannot rule out a write in the final rename instant —
# see docs/architecture.md for the exact guarantees)
aperio phase-set ./my-project implementation

# Planning and execution tracking (Feature 03)
aperio plan-create ./my-project --file plan.json    # create the plan (draft, revision 1)
aperio plan-show ./my-project                       # approval state, tasks, readiness
aperio plan-show ./my-project --json                # full plan document
aperio plan-approve ./my-project --by "team lead"   # record approval (operational
                                                    # marker after genuine human review)
aperio plan-revise ./my-project --file plan-v2.json --revises 1
                                                    # new draft revision (resets approval)
aperio task-list ./my-project                       # tasks with derived readiness
aperio task-status ./my-project TASK-001 --status in-progress --owner agent-a
aperio task-status ./my-project TASK-001 --status completed \
  --evidence "all tests green" --limitations "none noted" --result passed
```

### `aperio init`

Creates a project-owned `.aperio/` context directory:

| Path | Purpose | Shared between agents? |
|------|---------|------------------------|
| `.aperio/state.json` | Versioned project-state schema (single source of truth) | yes |
| `.aperio/brief.md` | Project brief — what is being built and why | yes |
| `.aperio/decisions.md` | Append-only decision log (machine-parseable entries) | yes |
| `.aperio/handoff.md` | Structured handoff for the next agent/session | yes |
| `.aperio/.gitignore` | Keeps `local/` and transient temp files out of git | yes |
| `.aperio/local/` | Machine-local and sensitive data (ignored by git) | **no — never** |

Safety rules:

- Re-running `init` is safe: it repairs **only missing** files and never overwrites existing content.
- If `state.json` exists but is corrupt or from an unsupported schema version, `init` aborts instead of overwriting.
- Unsafe paths are rejected: symlinks at the target (live or dangling), non-directory targets, the filesystem root, empty paths.

### `aperio status`

Reads `.aperio/state.json` and reports project name, schema version, phase, timestamps,
and which context files exist. It **never writes or creates anything**. Missing context
files are reported so a follow-up `init` can repair them.

### `aperio memory` (Feature 02)

One call returns the shareable context an agent needs to continue without
the previous agent's conversation: validated state, brief, parsed decision
history, and the handoff — with typed metadata (`missing`, malformed-entry
reports, duplicate-ID reports). `--json` emits a stable machine-readable
schema. It never reads `.aperio/local/` and never captures environment
variables.

### Decision log (Feature 02)

`decision-add` appends a fully validated entry (`DEC-NNNN` id — unique
under single-writer use; concurrent appends may duplicate IDs, which are
reported — UTC timestamp, title, context, decision, consequences) in a
strict, injection-hardened Markdown grammar. Existing bytes are never
rewritten; a torn entry from a crash is reported as malformed metadata
when the tear lands after the heading, never fatal and never edited. See
[docs/architecture.md](docs/architecture.md) for the normative grammar and
the per-artifact concurrency guarantees.

### Handoff (Feature 02)

`handoff-write` replaces `handoff.md` with a validated structured handoff
(seven required non-empty fields, section-injection rejected). Reading
supports three shapes: structured v1, legacy free-form files, and newer
formats (returned as raw text, never fatal). Phase updates via `phase-set`
use compare-and-swap so concurrent modifications that land before the
final check are detected instead of silently lost.

### Planning & execution tracking (Feature 03)

`plan-create` stores a versioned plan (`.aperio/plan.json`, plan-schema v1)
with objective, outcome, scope/exclusions, assumptions/risks, strategy,
acceptance criteria, verification strategy, and a task graph. Tasks carry
unique `TASK-NNNN` ids, deliverables, acceptance criteria, and
dependencies; **readiness is derived** (pending + all dependencies
completed), never stored. Transitions (`pending → in-progress →
completed`, with `blocked` as an explicit side state) are validated:
starting requires claiming ownership and satisfied dependencies;
completion requires recorded verification evidence, limitations, and a
`passed` result; reopening is explicit and refused while dependents rely
on the completion. Execution tracking requires an **approved** plan —
`plan-approve` records an operational marker (never cryptographic proof;
genuine human review is the operator's responsibility) — and any
`plan-revise` resets approval to draft. Revisions declare the
`plan_version` they replace; stale declarations are refused. Plan revisions,
approvals, and task transitions run under a coordination lock with
single-snapshot byte-CAS persistence (plan creation is create-only via
an exclusive hard-link publish). See [docs/architecture.md](docs/architecture.md).

## How agents discover current project state

1. Run `aperio memory <project-directory> --json` (or `aperio status`, or
   read `.aperio/state.json` directly).
2. Check `schema_version` first; treat unknown versions as "written by a newer Aperio" and stop.
3. Read the brief, the append-only decision log, the handoff, and the plan
   summary (all included in `memory --json`): approval state, task counts,
   and which tasks are ready or blocked.
4. Record progress through the APIs: `decision-add`, `handoff-write`,
   `phase-set`, `task-status`.
5. Never store credentials anywhere under `.aperio/` except `.aperio/local/`, which is
   git-ignored and not portable by definition.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

Conventions and quality requirements: [docs/conventions.md](docs/conventions.md).

## License

[MIT](LICENSE)

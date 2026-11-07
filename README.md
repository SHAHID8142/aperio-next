# aperio-next

**Aperio** is a model-agnostic, multi-agent software development orchestrator.
This repository contains **Features 01 and 02**: the project foundation
(bootstrap and inspect portable project state) and portable agent memory
(read shared context, record decisions, hand off work — so agents can
continue without each other's conversation history).

Anything not listed below is **planned, not implemented**. See
[docs/roadmap.md](docs/roadmap.md) for what is coming and
[docs/architecture.md](docs/architecture.md) for design boundaries.

## Status

- Implemented (Feature 01): `aperio --version`, `aperio init <project-directory>`, `aperio status <project-directory>`
- Implemented (Feature 02): `aperio memory [--json]`, `aperio decision-add`, `aperio handoff-write`, `aperio phase-set`
- Not implemented (by design, yet): task orchestration, task planning, project interviews, agent integrations

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

# Update the project phase (compare-and-swap: concurrent changes are
# detected, nothing is silently overwritten)
aperio phase-set ./my-project implementation
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

## How agents discover current project state

1. Run `aperio memory <project-directory> --json` (or `aperio status`, or
   read `.aperio/state.json` directly).
2. Check `schema_version` first; treat unknown versions as "written by a newer Aperio" and stop.
3. Read the brief, the append-only decision log, and the handoff (all
   included in `memory --json`).
4. Record progress through the APIs: `decision-add`, `handoff-write`,
   `phase-set`.
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

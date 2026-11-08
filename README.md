# aperio-next

**Aperio** is a universal, agent-agnostic reliability and context layer for
AI-assisted knowledge work. It prepares portable context, preserves
provenance, and supports optional verification and handoff for any agent
performing design, research, writing, analysis, business, conversational,
or software work.

This repository contains **Features 01–07** and **M1**:

- portable project state, decision memory, and structured handoffs;
- dependency-validated planning, execution tracking, and independent review;
- validated export/import, integrity checks, and schema-upgrade foundations;
- provider-neutral non-interactive CLI adapters for Codex and Claude;
- a universal, read-only context pack that works as a one-off task or over
  an existing project.

Software development is an **optional domain profile**, not the definition
of the product.

Anything not listed below is **planned, not implemented**. See
[docs/roadmap.md](docs/roadmap.md) for what is coming and
[docs/architecture.md](docs/architecture.md) for design boundaries.

## Status

- Implemented (Feature 01): `aperio --version`, `aperio init <project-directory>`, `aperio status <project-directory>`
- Implemented (Feature 02): `aperio memory [--json]`, `aperio decision-add`, `aperio handoff-write`, `aperio phase-set`
- Implemented (Feature 03): `aperio plan-create`, `aperio plan-show [--json]`, `aperio plan-approve`, `aperio plan-revise`, `aperio task-list [--json]`, `aperio task-status`
- Implemented (Feature 04): `aperio agent-list`, `aperio run-preview`, `aperio run-start [--authorize-exec]`, `aperio run-status`, `aperio runs-list`, `aperio run-cancel`, `aperio runs-reconcile`, `aperio run-report`, `aperio run-handoff`
- Implemented (Feature 05): `aperio export [--dry-run] [--json]`, `aperio import [--replace] [--dry-run]`, `aperio integrity [--archive] [--json]`, `aperio upgrade [--apply] [--json]`
- Implemented (Feature 06): `aperio roles`, `aperio policy-init`, `aperio policy-show`, `aperio policy-assign`, `aperio policy-permissions`, `aperio review-submit`, `aperio review-decide`, `aperio review-list`, `aperio review-status`
- Implemented (Feature 07): `codex-cli` and `claude-cli` through `aperio agent-list`, `agent-doctor`, `run-preview`, and `run-start --authorize-exec [--authorize-write]` (documented non-interactive CLIs; read-only by default; no GUI-app automation)
- Implemented (M1): `aperio context-pack [workspace]` with `general` (default) and optional `software` profiles, Markdown/JSON output, deterministic byte budgeting, trust/provenance labels, imported/proposed context labels, and strict read-only operation
- Implemented (M2): `aperio profiles`, `aperio goal-check`, and `aperio goal-pack` — a versioned universal goal contract (goal-schema v1), six data-only task profiles, optional work decomposition, and goal→context-pack conversion that works with no project, provider, or model API
- Implemented (M3): `aperio mem-propose`, `mem-review`, `mem-list`, `mem-show`, `mem-recall`, `mem-export`, `mem-import` — a standalone universal memory store (memory-schema v1) with explicit review states, deterministic retrieval, optional context-pack integration (`--memory-store`), and a versioned portable exchange format
- Not implemented (by design, yet): autonomous loops, sandboxed execution, Cursor/ZCode GUI integrations, cloud sync, automatic learning

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

# Universal goal contract — validate an assignment for any domain.
# No project, provider, or model API is involved.
aperio goal-check goal.json
aperio profiles
aperio goal-pack goal.json > pack.md

# Universal memory — works with no project at all.
# The store file is always an explicit path you choose.
aperio mem-propose --store ~/notes/work.mem --type requirement \
  --title "Primary brand colour" \
  --content "Primary brand colour is deep green #0B5D3B." \
  --scope objective --scope-value "spring campaign poster" --tag brand
aperio mem-review ~/notes/work.mem MEM-1a2b3c4d5e6f --accept
aperio mem-recall ~/notes/work.mem --objective "spring campaign poster"
aperio goal-pack goal.json --memory-store ~/notes/work.mem > pack.md
aperio mem-export ~/notes/work.mem --output handoff.json
aperio mem-import ~/other/work.mem handoff.json

# Universal context pack — works with no Aperio project at all.
# The default profile is domain-neutral.
aperio context-pack --objective "Design a poster for a community garden" \
  --constraint "Use accessible contrast" \
  --quality-criterion "Legible at small sizes" \
  --expected-output "One visual direction"

# Research, writing, business analysis, and general tasks use the same form:
aperio context-pack --objective-file research-question.txt \
  --constraint "Cite every factual claim" --json

# Optional: assemble the same kind of pack from an initialized project.
# This read-only mode includes only shareable artifacts, never .aperio/local/.
aperio context-pack ./my-project --objective "Prepare the design review" \
  --profile general --budget 8192

# The optional software profile keeps the software workflow explicit:
aperio context-pack ./my-project --profile software \
  --objective "Review the implementation plan"

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

# Agent execution (Feature 04)
aperio agent-list                                   # list built-in adapters
aperio run-preview ./my-project TASK-001 --agent manual   # deterministic preview
aperio run-start ./my-project TASK-001 --agent manual     # prepare handoff (never executes)
aperio run-start ./my-project TASK-001 --agent local-process \
  --config .aperio/local/adapter.json --authorize-exec    # REAL subprocess (explicit consent)
aperio run-start ./my-project TASK-001 --agent local-process \
  --config .aperio/local/adapter.json                    # read-only preview (no process)

# Real AI CLI providers (Feature 07; installed CLI + explicit consent)
aperio agent-doctor ./my-project --agent codex-cli \
  --config .aperio/local/codex.json --json                # passive flags/version, no model call
aperio run-start ./my-project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json --authorize-exec      # read-only
aperio run-start ./my-project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json \
  --authorize-exec --authorize-write                      # workspace-write consent
aperio run-preview ./my-project TASK-001 --agent claude-cli \
  --config .aperio/local/claude.json                      # command + prompt digest only

aperio run-status ./my-project RUN-0001 --json      # inspect one run
aperio runs-list ./my-project --json                # execution history
aperio run-cancel ./my-project RUN-0001 --reason "changed plan"
aperio runs-reconcile ./my-project                  # detect interrupted runs
aperio run-report ./my-project RUN-0001 --file report.json  # consume external result

# Workflow governance (Feature 06; optional, operator-enabled)
aperio roles                                          # role/action matrix
aperio policy-init ./my-project --assign planner-a:planner \
  --assign dev-b:implementer --assign rev-c:reviewer  # explicit enablement
aperio plan-create ./my-project --file plan.json --actor planner-a
aperio plan-approve ./my-project --actor rev-c         # independent approval
aperio task-status ./my-project TASK-001 --status in-progress \
  --owner dev-b --actor dev-b
aperio review-submit ./my-project --task TASK-001 --actor dev-b \
  --scope "acceptance criteria" --evidence "pytest: 42 passed"
aperio review-decide ./my-project REV-0001 --actor rev-c \
  --outcome approved --findings "criteria met"
aperio task-status ./my-project TASK-001 --status completed \
  --evidence "pytest: 42 passed" --limitations none --result passed \
  --actor dev-b
aperio review-status ./my-project --json              # pending + blocking gates

# Cross-machine portability (Feature 05)
aperio export ./my-project --dry-run                  # validate + preview, write nothing
aperio export ./my-project --output ./project.zip     # portable archive (must not exist)
aperio import ./destination ./project.zip --dry-run   # validate the whole archive first
aperio import ./destination ./project.zip             # never overwrites without --replace
aperio integrity ./my-project                         # read-only consistency report
aperio integrity ./my-project --archive ./project.zip # also verify manifest hashes
aperio upgrade ./my-project                           # dry-run schema preview
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

### Workflow governance and independent reviews (Feature 06)

Governance is **opt-in**: a project without `.aperio/policies.json` keeps
Feature 01–05 behavior exactly (no `--actor` required, no completion gate).
`aperio policy-init` enables it explicitly and never replaces an existing
policy.

Three built-in roles describe responsibilities, not AI providers:

| Role | May do |
|---|---|
| `planner` | create/revise plans, approve plans authored by someone else |
| `implementer` | claim tasks, dispatch execution, submit reviews, complete reviewed work |
| `reviewer` | approve plans and decide reviews independently |

Plan authorship is written into the plan document by the same atomic
publication that creates or revises it, and approval fails closed unless
the durable author record and exactly one matching attribution audit event
agree. If an audit append is lost, approval stays blocked until
`aperio plan-sync-authors` appends the missing history; plans created before
governance was enabled require the explicit, recorded `aperio plan-adopt`
procedure. A revision is never approved with unknown, delayed, corrupt, or
conflicting authorship.

Enforcement happens at the Python mutation boundaries (plan create/revise/
approve, task claim/status/completion, execution dispatch, review submit/
decide), not just in argument parsing. Configured actor→role mappings are
the only source of truth: a caller-supplied role label is never trusted,
and an unassigned actor id is an error. Separation of duties is mandatory:
the planner who authored a revision cannot approve it, an implementer
cannot decide their own review, and a new material revision invalidates
the previous approval and every review keyed to it.

Reviews are immutable, append-only records with stable `REV-NNNN` ids. A
review references the task, the material plan revision, the work attempt
and task/evidence revision, the recorded implementer, scope and evidence,
and (after a decision) the independent reviewer, outcome (`approved`,
`changes-requested`, `rejected`), findings, and timestamp. Only the recorded
implementer may submit. A decided review is never edited; further work needs
a new record. Reopening a task, changing its evidence, or submitting a newer
review invalidates an older approval for completion without deleting it: the
latest review for the current material revision must itself be approved and
match the current attempt and evidence revision. Completion must also repeat
exactly the evidence and limitations recorded in that approved review;
changing either after approval requires a fresh submission and independent
decision. Task completion under governance requires *both* recorded
verification evidence and an approved review at the current material
revision — a successful subprocess or external report is never treated as
independent verification.

**Trust boundary:** `--actor` is a declared id, not authenticated identity.
Aperio enforces workflow consistency for those declarations; it does not
prove who ran the command. Real human/agent authentication and sandboxing
are outside this feature and are not claimed.

Governance artifacts travel with Feature 05 archives (`policies.json`,
`reviews.json`, `policy-audit.json`). Import validates them, refuses
orphaned task reviews and future schemas, and keeps attribution intact.
`aperio memory --json` adds a `governance` block: enabled flag, policy
revision, actor assignments, pending reviews, latest outcomes, and the
approval requirements currently blocking completion.

### Real AI CLI providers (Feature 07)

Aperio launches installed, non-interactive coding CLIs through a
provider-neutral subprocess boundary:

| Adapter | Documented interface | Default executable |
|---|---|---|
| `codex-cli` | `codex exec --json` (prompt on stdin, read-only by default) | `codex` |
| `claude-cli` | `claude -p` (prompt on stdin, plan mode by default) | `claude` |

Both accept an optional machine-local JSON config (defaults: the CLI on
`PATH`, the project root, read-only) and require explicit `--authorize-exec`.
A separate `--authorize-write` is required for any write-capable mode. Both
providers default to read-only; without write consent a write request fails
before a run or task claim is created. Without `--authorize-exec`,
`run-start` writes nothing and starts nothing. The shared controls are
strict known-key validation with **no arbitrary provider args**,
project-confined working directory, stdin-only prompts, no shell, minimal
environment by default, bounded structured output, a machine-local log, a
bounded process-group timeout, and neutral execution-record metadata.

```json
{
  "executable": "claude",
  "working_dir": "/absolute/project/path",
  "timeout_seconds": 1800,
  "env_policy": "minimal",
  "allow_shell": false,
  "max_turns": 25,
  "max_budget_usd": 5
}
```

Read-only Claude uses plan mode with only `Read,Grep,Glob`; write consent
uses `acceptEdits` with `Read,Grep,Glob,Edit,Write`; `Bash` requires
`allow_shell: true` plus write consent. Codex read-only uses its
`read-only` sandbox; `workspace-write` requires the same second consent.
Unsafe overrides (`codex --sandbox danger-full-access`, Claude
`bypassPermissions`, raw `args`) are deliberately refused, and an
authorized launch first verifies the installed CLI advertises every required
flag. Codex JSONL and Claude JSON results are parsed into neutral metadata
only. Exit 0 is not verification or completion; the task remains in progress
until evidence and (when governed) independent review are recorded. These
are real local processes, not a sandbox. GUI apps such as ZCode and Cursor
are not integrated. See [docs/feature-07.md](docs/feature-07.md) for every
key, the `agent-doctor` diagnostic, cleanup bounds, and limitations.

### Portable export/import (Feature 05)

`export` writes a versioned ZIP archive of the **shareable** subset of
`.aperio/` only: `state.json`, `brief.md`, `decisions.md`, `handoff.md`,
`plan.json`, a machine-neutral projection of `executions.json`, and the
handoff documents of awaiting-external runs. `.aperio/local/` (local logs,
external reports, credentials), locks, temporary files, and the generated
`.gitignore` are excluded by construction — the exporter never walks the
directory tree. Execution records travel with `pid`, `machine`, and
`local_log_ref` nulled, so a pid from another computer can never be
interpreted locally.

`import` validates the complete archive before touching the destination:
manifest schema, path whitelist (no absolute paths, `..`, duplicates,
hidden names, `local/`), member types (no directories, symlinks, or
encryption), bounded decompressed sizes, and SHA-256 per file. Execution
records may not be `prepared`/`running` and may not carry a `pid`,
`machine`, or `local_log_ref`: cancelling a run signals its recorded pid,
so imported identity is refused rather than trusted. The importer then
validates the **merged** result (archive plus destination files that
remain), refusing plans that orphan retained runs, stale active runs across
a plan replacement, and awaiting-external runs whose handoff is missing.
Existing files are preserved byte-for-byte; identical bytes are accepted as
unchanged, and replacing differing context requires explicit `--replace`
after review. The apply step takes every writer lock in a fixed order,
aborts if the destination changed while preparing, writes `state.json`
last, and rolls its own writes back on failure.

**Honesty note:** manifest hashes detect corruption, not authorship. They
do not authenticate an archive and cannot prove that hand-written briefs,
decisions, or handoffs contain no secrets. Review shareable text before
sending it anywhere.

`integrity` reports — never repairs — schema versions, plan graph and task
lifecycle consistency, malformed/duplicate decision entries, handoff
format, execution-record references, missing or unexpected shareable
files, and (with `--archive`) archive manifest integrity. `upgrade`
previews schema versions per artifact; it applies only explicitly
registered migrations, refuses newer versions, and backs up originals
under `.aperio/local/backups/` before any write. No migrations exist in
this release, so it is a framework, not a conversion path.

### `aperio context-pack` (M1)

The universal context pack is the first milestone of the approved
realignment. It prepares a portable snapshot for **any** agent, with no
software assumption and no project required:

```sh
aperio context-pack --objective "Write a research summary" \
  --constraint "Cite every factual claim" \
  --quality-criterion "Distinguish evidence from interpretation" \
  --expected-output "A two-page brief" --json
```

- `general` is the default profile. `software` is opt-in.
- An optional initialized workspace contributes only shareable artifacts:
  brief, decisions, structured handoff, plan/execution summaries, and
  approved review records. `.aperio/local/` is never read.
- `--imported-context FILE` and `--proposed-context FILE` add
  operator-selected material with explicit `imported` / `proposed` trust
  labels. No filename or local path is copied into the pack.
- The context budget (`--budget BYTES`) applies to selected context-item
  content only. Essential objective/constraints/quality/output fields are
  outside it and are never silently truncated; an item that does not fit
  is omitted whole.
- JSON and Markdown are rendered from the same canonical payload. The
  reproducible content hash excludes generation time and package version.
- Generating a pack never executes the task or controls the receiving
  agent, and does not by itself prove performance improvement.

The generated Markdown quotes project material as **data**, not
instructions, and labels trust as `authoritative`, `verified`, `attested`,
`proposed`, or `imported`. A review is `verified` only while its approval
still applies to the current plan revision, work attempt, and task revision;
stale approvals are excluded from current context rather than presented as
current evidence. See
[docs/context-pack.md](docs/context-pack.md) for the full contract.

### `aperio goal-check` / `aperio goal-pack` / `aperio profiles` (M2)

M2 turns "an assignment" into a validated document any agent can use. A
goal is versioned JSON (`goal-schema v1`): objective, intended outcome,
profile, constraints/requirements, assumptions, uncertainties, risks,
audience, deliverables, quality and acceptance criteria, output contract,
evidence requirements, an optional work decomposition, and
provenance/trust metadata. Only `objective` and `intended_outcome` are
required; anything absent is reported as absent rather than invented.

```sh
# A design brief, validated and reported on (no project needed)
aperio goal-check brief.json

# Convert the same goal into a portable, provider-neutral pack
aperio goal-pack brief.json --json > pack.json

# Optional: add read-only project context (never .aperio/local/)
aperio goal-pack brief.json ./my-project

# Which profiles exist, and what each one suggests?
aperio profiles --json
```

- Requirements and constraints stay separate all the way through (each may
  hold up to 50 entries), so a large goal cannot fail conversion because two
  bounded lists were merged into one.
- `--output FILE` publishes atomically through an exclusively created
  temporary file: no symlink is followed, no existing file is truncated
  without `--force`, concurrent writers cannot overwrite each other, and
  paths inside `.aperio/` or the goal document itself are refused. Where the
  filesystem cannot publish without overwriting (no hard links), the command
  fails closed with an actionable error rather than degrading to a racy
  fallback.
- A goal's declared source governs instruction authority. `agent` and
  `imported` goals are still transferred and inspectable, but their
  requirements, constraints, outcomes, criteria, and work are labelled as
  attributed proposals and never presented as operator instructions.
- Profiles — `general` (default), `design`, `research`, `writing`,
  `business`, `software` — are data only. Their criteria are suggestions,
  shown separately from operator requirements, and never gate approval or
  completion.
- Work decomposition is optional: a one-off question needs no task graph.
  Supplied graphs reuse the plan-schema v1 dependency validator, so cycles,
  self-dependencies, duplicate ids, and unknown dependencies are rejected.
- Goal identity is a SHA-256 over the canonical goal: no timestamp, path, or
  version input, so the same goal has the same identity anywhere.
- Agent- or import-declared provenance makes the goal `proposed` /
  `imported` and prints a notice that it is data to assess, not operator
  instruction.
- Legacy plans, decisions, handoffs, reviews, and archives are untouched;
  no migration, no new mandatory project file.
- Generating a pack is preparation only: it does not execute, verify, or
  complete anything.

See [docs/goal-contract.md](docs/goal-contract.md) for the full contract.

### Universal memory (M3)

A memory store is a single operator-named file that keeps knowledge worth
reusing across sessions and agents: preferences, confirmed requirements,
decisions, observations, limitations, lessons, conventions, proposals, and
outcomes. It needs no project, plan, git repository, provider, or API key.

Every record starts `proposed`. Only an explicit review makes it `attested`
(accepted as authoritative *for its scope*) or `verified`. Verification
requires at least one evidence reference and a declared reviewer, and refuses
a reviewer identical to the declared proposer; identifiers are attribution,
not authentication. Acceptance is not factual proof, imports never raise
trust, and nothing is promoted automatically. Retrieval is deterministic
keyword + scope + review state ranking with explicit exclusion reasons and
whole-item budgeting — no embeddings, no model scores, no network. Validity
windows (`valid_from`/`valid_until`) are evaluated on every read against
today unless `--as-of` is given, so stale knowledge drops out of recall and
packs without any extra flag.

`--memory-store` adds retrieved memory to a context pack as quoted data after
operator context, with its trust label, review state, and identity. Without
it, packs are byte-identical to the previous release.

See [docs/universal-memory.md](docs/universal-memory.md) for the contract,
trust model, retrieval rules, portability format, and limitations.

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
`passed` result; reopening is explicit (`--reopen`) and refused while
dependents rely on the completion. Execution tracking requires an
**approved** plan — `plan-approve` records an operational marker (never
cryptographic proof; genuine human review is the operator's
responsibility) — and any `plan-revise` resets approval to draft.
Revisions declare the `plan_version` they replace; stale declarations are
refused, and material changes to non-pending tasks are refused so recorded
verification evidence can never silently apply to a changed contract.
Plan revisions, approvals, and task transitions run under a coordination
lock with single-snapshot byte-CAS persistence (plan creation is
create-only via an exclusive hard-link publish). See
[docs/architecture.md](docs/architecture.md).

## How agents discover current project state

1. For portable work, run `aperio context-pack ...` (standalone or with a
   project) and give the Markdown/JSON to the agent. Do not treat pack
   generation as execution or approval.
2. Run `aperio memory <project-directory> --json` (or `aperio status`, or
   read `.aperio/state.json` directly).
3. Check `schema_version` first; treat unknown versions as "written by a newer Aperio" and stop.
4. Read the brief, the append-only decision log, the handoff, and the plan
   summary (all included in `memory --json`): approval state, task counts,
   and which tasks are ready or blocked.
5. Record progress through the APIs: `decision-add`, `handoff-write`,
   `phase-set`, `task-status`.
6. Never store credentials anywhere under `.aperio/` except `.aperio/local/`, which is
   git-ignored and not portable by definition.
6. To move a project between machines, use `aperio export` / `aperio import`
   and verify the result with `aperio integrity`. The archive carries the
   shareable context only; `local/` never travels.
7. When governance is enabled, never self-approve a plan revision, self-
   decide a review, or treat a successful process exit as verified
   completion. Declare an actor with `--actor`; the project policy decides
   what that role may do. Reopen or evidence changes require a fresh review,
   and only the recorded implementer may submit.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

Conventions and quality requirements: [docs/conventions.md](docs/conventions.md).

## License

[MIT](LICENSE)

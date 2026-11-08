# Aperio Architecture — Features 01–04 (implemented)

This document describes what Aperio **is today**. Planned capabilities are in
[roadmap.md](roadmap.md) and are never described here as working.

## Mission

Aperio is a **model-agnostic, multi-agent software development orchestrator**:
a runtime in which multiple AI agents (possibly powered by different models)
collaborate on one software project safely, with durable, portable project
memory.

- Feature 01 delivers the foundation: a CLI that bootstraps and inspects a
  portable, versioned project-state directory.
- Feature 02 delivers portable agent memory: a read API over the shareable
  context, append-only decision recording, validated structured handoffs,
  and compare-and-swap phase updates — so an agent can continue work without
  the previous agent's conversation history.
- Feature 03 delivers planning and execution tracking: a versioned plan
  document, a dependency-validated task graph with an approval gate, and a
  rigorous evidence-based task lifecycle.
- Feature 04 delivers the first agent-execution runtime: a documented
  adapter contract with two real adapters (manual handoff, configured
  local-process), versioned execution records, an authorization gate, a
  sequential dispatch scheduler with a dispatch-scope lock, and crash
  reconciliation. Aperio manages and records execution — it does not
  implement model integrations, and ``--authorize-exec`` is a procedural
  human-in-the-loop checkpoint, not a sandbox.

## Explicit non-goals (for the whole project)

- Aperio is **not** a model provider, proxy, or key manager. It never talks to
  model APIs and never handles credentials itself.
- Aperio does not run inside a vendor's sandbox as a hidden dependency; it is
  a plain, inspectable CLI and library.
- Aperio does not replace version control. Git remains the source of truth
  for code history; `.aperio/` is project *memory*, not a VCS.
- Aperio does not make model choices for users, and its project state must
  never embed model-specific or vendor-specific assumptions.
- No databases, dashboards, external SDKs, or cloud storage: plain JSON and
  Markdown are the only persistent formats.

## Package layout (implemented)

```
src/aperio/
├── __init__.py    package version (single source: mirrored by pyproject)
├── __main__.py    python -m aperio entry point
├── cli.py         argparse CLI; catches AperioError, maps to exit codes
├── decisions.py   append-only decision log: strict grammar, parse, append
├── errors.py      AperioError + PathError / NotInitializedError /
│                  StateError / ValidationError
├── handoff.py     structured handoff: validate, render, replace (CAS option)
├── inputs.py      strict input validation (JSON fields, line-separator rules)
├── locks.py       advisory file locks: bounded wait, never stolen
├── memory.py      portable context read API (ProjectContext, load_context)
├── paths.py       safe path resolution policy (see below)
├── planmodel.py   plan/task validation, dependency graph, lifecycle (pure)
├── planstore.py   plan persistence: create/revise/approve/task-status under
│                  plan.lock with single-snapshot byte-CAS
├── adapters.py    agent adapter contract + manual / local-process adapters
├── runtime.py     execution records, eligibility, dispatch, reconciliation
├── progress.py    phase updates (compare-and-swap state updates)
├── project.py     init/status operations, file templates, boundary checks
└── schema.py      versioned state schema: validate, read, atomic write,
                   replace_state_if_unchanged (deliberate update path)
```

Dependency direction is strictly downward: `cli → memory/planning/handoff/
decisions/progress → project/schema/paths/locks → errors`. Nothing imports
upward. The CLI layer is the only module allowed to print.

## Extension boundaries

- **New CLI commands** go in `cli.py` as subparsers; their logic goes in a new
  or existing `project.py`-style operations module. The CLI never contains
  business logic beyond formatting and exit-code mapping.
- **Schema changes** must bump `SCHEMA_VERSION` in `schema.py` and ship a
  reader that still accepts all older versions. Writers never rewrite state
  they do not fully understand (a newer schema aborts with a clear error).
- **New context files** under `.aperio/` extend `CONTEXT_FILES` in
  `project.py`, their template, and the `status` report together.
- **Runtime dependencies are currently zero.** Adding one requires a
  documented reason in `docs/conventions.md` first.

## Safe path policy (implemented)

`paths.resolve_target` enforces, for writing operations:

1. non-empty, NUL-free paths; `~` expansion; absolute form
2. the filesystem root is rejected
3. an existing target must be a directory — a regular file is rejected
4. a symlink at the target is rejected — live *or dangling* (a dangling
   link must not cause the project to be created at its destination); the
   same rejection is enforced for every individual write target inside the
   project (`brief.md`, `decisions.md`, `handoff.md`, `.gitignore`,
   `local/`, and `state.json` itself), so a planted symlink can never
   redirect Aperio's writes outside the project directory
5. symlinked *ancestor* components are followed transparently, because e.g.
   macOS `/tmp` is itself a symlink; the returned path is passed through
   `os.path.realpath`, so the location echoed back to the user is where the
   files will actually live — nothing is hidden

Read-only operations (`status`) apply rules 1-2 only and may read through
symlinks. This asymmetry is deliberate and documented.

Additionally:

- `state.json.tmp` and `handoff.md.tmp` are **reserved** Aperio temporary
  names that are never deleted automatically: ownership of a plain file
  cannot be verified, so *any* occupant — a leftover from a crashed write,
  a directory, or a symlink live or dangling — aborts the operation with an
  actionable error and is resolved by explicit user action. The names are
  git-ignored in generated projects.
- State creation is atomic and non-destructive: the temp file is created
  exclusively (an unexpected file appearing in the race window can never
  be truncated), the publish is an atomic hard-link create-if-not-exists
  (even a state file planted mid-write is not destroyed), an existing
  state file is never overwritten through `write_state`, symlinked
  targets are refused, and on failure only the temp file created by that
  same call is removed — nothing pre-existing is ever auto-deleted.
- A directory or other non-regular occupant at a context-file path (e.g.
  `.aperio/brief.md`) is an explicit error, never a false "present" report.
- I/O failures during writes (permissions, conflicting paths) surface
  as clean, actionable errors rather than tracebacks.

## `.aperio/` project context (implemented)

| Path | Role | Portable? |
|------|------|-----------|
| `state.json` | versioned, machine-readable state; single source of truth | yes |
| `brief.md` | what the project is building and why | yes |
| `decisions.md` | append-only decision log | yes |
| `handoff.md` | current handoff notes (replaced, not appended) | yes |
| `plan.json` | versioned plan + task graph (Feature 03; created via `plan-create`) | yes |
| `.gitignore` | keeps `local/`, temp files, and locks out of any VCS | yes |
| `local/` | machine-local and sensitive data | **no — never shared** |

Schema v1 (`state.json`): `schema_version`, `name`, `created_at`,
`updated_at`, `phase`, `aperio_version` — all required; unknown extra keys
are tolerated for forward compatibility and preserved through supported
updates; unknown *versions* abort rather than being modified.

## Feature 02: portable agent memory (implemented)

### Modules and commands

| Command | Library entry point | Purpose |
|---------|--------------------|---------|
| `aperio memory <dir> [--json]` | `memory.load_context` | read the full shareable context (state, brief, decisions, handoff, plan summary) with typed metadata |
| `aperio decision-add <dir> (--file <json> \| --title --context --decision --consequences)` | `decisions.append_decision` | append one decision; prints the assigned `DEC-NNNN` id |
| `aperio handoff-write <dir> --file <json>` | `handoff.write_handoff` | validate and replace the structured handoff |
| `aperio phase-set <dir> <phase>` | `progress.update_phase` | update the phase with lost-update detection |

`memory --json` emits a stable schema (keys will not be removed or renamed
within a feature; new keys may be added): `reader_version`, `state`,
`brief`, `decisions` (list or null when the file is missing), `handoff`
(structured flag/version/revision/fields/raw), `plan` (Feature-03 summary
or null), `missing`, `skipped_headings`, `malformed_entries`,
`duplicate_decision_ids`. It never contains `.aperio/local/` content,
environment values, or the local filesystem path. A shareable file that is
a live symlink is refused rather than read (a link could point at local or
private data); dangling links are reported as missing for the
brief/decisions/handoff files via `memory`, while a symlinked `state.json`
— dangling or not — is refused outright, as are symlinked
`decisions.md`/`handoff.md`/`plan.json` for the direct library readers.

### Decision log grammar (normative)

Entries are the machine-readable source of truth; the Markdown file is
parsed, never mirrored in JSON. Headings are recognized **only at column
0**:

```
## DEC-0001 — 2026-11-07T14:00:00Z — Single-line title

- **Context:** first line
  <2-space-indented continuation lines>
- **Decision:** ...
- **Consequences:** ...
```

- Separator is U+2014 em-dash; timestamps are ISO-8601 UTC with `Z`.
- Multi-line field values are rendered as 2-space-indented continuation
  lines — a deliberate injection defense: agent-supplied text can never
  place `## ` or `- **` at column 0. Validation additionally rejects every
  other Unicode line-boundary character (CR, NEL, LS/PS, ASCII record
  separators), because consumers like `str.splitlines()` or CommonMark
  treat those as line endings and could otherwise see forged headings.
- The reader is lenient about *damage*: DEC-pattern headings that fail to
  parse are reported in `malformed_entries` (never fatal, never edited);
  non-DEC `##` sections count as skipped user content. File-level problems
  (unreadable, non-UTF-8) are hard errors. A crash-torn *tail* is only
  visible as malformed when the tear lands after the DEC heading; an
  earlier tear may look like prose — it is never edited or removed.
- `append_decision` never rewrites existing bytes: it appends one entry as
  a single `os.write` on an `O_APPEND` descriptor, plus `O_NOFOLLOW` where
  the platform provides it (POSIX); without that flag the protection
  degrades to the islink pre-check. The next ID is max over *all* DEC
  headings (valid or malformed) plus one; file order is never assumed to
  equal ID order.

### Handoff format v1

Line 1 is the title, line 2 is the marker `<!-- aperio:handoff:v1 -->`,
followed by exactly the seven sections: Current objective, Completed work,
Work in progress, Next recommended action, Blockers, Verification evidence,
Verification limitations. All seven fields are required non-empty at write
time ("None noted." when nothing applies) — validation completes before any
filesystem effect, and a field containing a column-0 `## ` line is rejected.
Reader behavior: v1 parsed strictly (a missing/duplicated section is damage
and a hard error; the marker may carry a trailing CR for CRLF-edited
files); a `v2+` marker returns the raw text with `structured=None` (never
fatal); no marker means a legacy free-form handoff (`structured=False`).
Writes replace the file via exclusive temp + rename (a symlink swapped in
after the islink check is replaced as a directory entry — its target is
never followed); `handoff.md.tmp` leftovers are user-resolved conflicts
(occupied either by a concurrent write or by a crashed-write leftover).

### Project boundary validation (all memory operations)

Every memory operation — `memory`, `decision-add`, `handoff-write`,
`phase-set`, and their library entry points — first establishes that it is
operating inside a genuine initialized project:

1. the `.aperio/` directory itself must not be a symlink (a leaf-file check
   alone would pass while every access silently traverses the link outside
   the project boundary);
2. `state.json` must exist and be a supported schema — a directory that
   merely contains pre-existing context files (e.g. an `.aperio/brief.md`)
   is *not* a project, and corrupt or newer-than-supported states abort;
3. write operations additionally use the write-safe path policy for the
   project directory, every target file, and every temp/lock name.

Refusals never modify anything: outside files, user content, and the
refused state are preserved byte-for-byte.

### Concurrency guarantees (accurate, per artifact)

Coordination uses advisory lock files (`state.lock`, `decisions.lock`,
`handoff.lock`; reserved aperio names, git-ignored) with a bounded wait.
**A lock is never stolen**: there is no age-based automatic recovery, so a
lock held by an active (or stalled) writer cannot be removed behind its
back. An occupied lock past the timeout yields a clear conflict naming the
lock file — which records its owner as `pid:timestamp` — and an abandoned
lock is resolved by explicit user action (inspect the owner, remove the
file if that process is gone). Locks are removed only by their verified
owner (content-checked token). The locks coordinate aperio writers with
each other — tools that do not use aperio ignore them entirely.

| Artifact | Mechanism | Guarantee | Limitation |
|----------|-----------|-----------|------------|
| `state.json` create | exclusive temp + hard-link publish | no overwrite, ever | — |
| `state.json` update (`phase-set`) | full read→validate→modify→publish under `state.lock`, plus a byte compare-and-swap against the exact under-lock read | concurrent *aperio* updates serialize and cannot silently overwrite each other; **unknown extra state fields are preserved exactly (meaning and JSON type)**; a non-cooperating tool's write before the compare aborts with nothing modified | the filesystem cannot protect against a non-cooperating write landing between the final compare and the rename; that residual window is documented, not claimed away |
| `decisions.md` | advisory lock around read-compute-append, then single-syscall `O_APPEND` | unique sequential IDs among aperio writers; existing bytes never rewritten; concurrent appends never interleave | **tools that edit the log without aperio's lock bypass uniqueness** — their appends are never overwritten, but their entry IDs may collide with the next aperio-computed ID (reported as duplicates); an occupied lock past the bounded wait produces a clear conflict, never a hang or a steal |
| `handoff.md` | advisory lock + single-snapshot read (revision AND expected bytes from the same read) + revision checks + final byte check | a stale writer (declaring an older revision) conflicts instead of clobbering; newer unsupported formats (v2+) are refused; concurrent aperio writers serialize; the revision decision can never be made on bytes different from the conflict-detection baseline | replacing revision ≥ 1 requires declaring that revision, or the opt-in `force` flag (deliberate content loss); the same non-cooperating-writer residual window applies |

Crash-consistency is best-effort across all artifacts: no `fsync` is
performed, so a power loss can tear an appended tail or lose a not-yet-
flushed rename. Nothing is ever silently destroyed; leftovers surface as
conflicts for explicit user action. Note for projects initialized before
the lock files existed: `init` never rewrites an existing
`.aperio/.gitignore`, so add `state.lock`, `decisions.lock`, and
`handoff.lock` to it manually if those transient files should stay out of
version control.

Schema v1 is preserved by phase updates: only `phase`, `updated_at`, and
`aperio_version` (the rewriting version) change; `name`, `created_at`,
`schema_version` are preserved. Setting the same phase is an idempotent
no-op that does not bump `updated_at`. The decision log is the project's
audit trail — phase transitions are not auto-logged.

## Feature 03: planning, task graph & execution tracking (implemented)

### Plan document (`.aperio/plan.json`, plan-schema v1)

The plan is created explicitly (`aperio plan-create`) — it is **not**
init-created, has no template, and is deliberately **not** part of
`CONTEXT_FILES`/`aperio status` (status reports init-created context;
plan presence is surfaced by `memory`, `plan-show`, and `task-list`).
Stored dict-based: unknown keys found on disk are tolerated and preserved,
while create/revise **input** is strictly validated — unknown and
system-managed keys (schema_version, plan_version, approval, timestamps,
aperio_version, and every task lifecycle key) are rejected at the input
boundary. The `tasks` array order is not semantic; readers sort by id or
topologically. Full schema: see `aperio/planmodel.py` and the README.

### Plan lifecycle and revisions

- `plan-create` (create-only, exclusive hard-link publish): every task
  starts `pending` with no lifecycle history — fabricated completion at
  create time is impossible. Revision 1, approval `draft`.
- `plan-revise --revises N`: full content replacement; `N` must equal the
  current `plan_version` (stale → conflict, nothing modified). Lifecycle
  state (status/owner/blocked_reason/verification) is carried verbatim
  **only for tasks whose contract is unchanged** — a material change
  (objective, deliverables, acceptance criteria, or dependencies) to a
  non-pending task is **refused** with actionable guidance: the previous
  verification evidence must not appear valid for a different contract.
  Resolution is deliberate — return the task to `pending` through
  explicit lifecycle transitions (reopen if completed, then block and
  unblock) or assign a new task id; existing evidence is preserved,
  never discarded automatically. Each transition in the chain bumps
  `plan_version`, and a draft plan additionally requires re-approval
  before execution tracking resumes. The comparison is exact
  equality, so visually-identical-but-differently-normalized edits
  (unicode normalization forms, list ordering) are also treated as
  material — refuse-and-preserve is the safe direction. New tasks start pending;
  **removing a non-pending task is refused**; and a revision may not
  leave an in-progress or completed task with unsatisfied dependencies.
  Every revision resets approval to draft — renewed approval is required
  before execution tracking resumes (conservative by design: no
  "material change" judgment calls about approval). Agents must re-read
  the plan after any revise; in-progress work is stranded until
  re-approval.
- `plan-approve [--revision N] [--by LABEL]`: records the approval stamp
  (`approved_revision` = the approved plan_version). This is an
  **operational marker, not cryptographic proof** of human authorization —
  the operator is responsible for genuine review; aperio never
  auto-approves. Approval never bumps `plan_version`; re-approval
  re-stamps. `approved_revision` is historical provenance ("what was
  approved"), never a freshness predicate — consumers must use
  `approval_status`/`approval_required`.
- `plan_version` bumps on every saved change (revisions AND task-status
  operations), which is what makes stale revise declarations detectable.

### Task graph and lifecycle

Task ids are `TASK-<digits>`, unique, and every `depends_on` must reference
an existing task; self-dependencies and cycles (named in the error) are
rejected at create/revise **and re-validated on every read of the stored
document** (stored-plan validation matches input strictness: content
fields, graph, lifecycle/dependency consistency, and approval
impossibility — malformed on-disk data is damage, reported as StateError,
never a traceback). **Readiness is derived, never stored**: a task is
*dependency-ready* when it is `pending` and all dependencies are
`completed` — blocked or reopened dependencies immediately de-ready
dependents with no stored staleness. Dependency readiness is distinct
from **executable readiness**: a task is executable only when it is
dependency-ready *and* the plan is approved (`executable_task_ids` in the
memory summary and `is_executable` in `task-list --json`; draft-plan
tasks are never presented as authorized to start, and `task-list`
renders `[dep-ready]` until approval). Stored documents with
contradictory lifecycle/dependency states (e.g. a completed task whose
dependency is not completed) cannot arise from legitimate operations and
are rejected as damage.

Stored statuses: `pending`, `in-progress`, `blocked`, `completed`.
Transition table (everything else forbidden; nothing silent):

| From | To | Requirements |
|------|----|--------------|
| pending | in-progress | `owner` (agent claiming) + all dependencies completed (re-checked at operation time) + approved plan |
| pending / in-progress | blocked | `blocked_reason` |
| blocked | pending | unblock: owner and reason cleared |
| in-progress | completed | dependencies completed (re-checked) + `evidence` + `limitations` + `result == "passed"` in the same operation + approved plan |
| completed | in-progress | explicit reopen; clears the verification block; refused while any dependent is in-progress or completed |

`result == "failed"` records a verification attempt on the in-progress
task (owner preserved); a failed verification can never be recorded as
successful completion. Failure records are last-write — durable failure
history belongs in the append-only decision log. Completion requires an
approved plan; the plan being approved is an operational marker whose
genuineness is the operator's responsibility.

### Memory integration

`aperio memory --json` gains the additive `plan` key: `null` when no plan
exists (normal state; not added to `missing`), otherwise a concise summary
— `{present, unsupported_schema, schema_version, plan_version,
approval_status, approved_revision, approval_required, objective,
task_counts, ready_task_ids, executable_task_ids, blocked_task_ids}`.
File-level corruption is fatal (consistent with other shareable files); a
newer plan-schema version degrades gracefully (an `unsupported_schema`
marker, never fatal); live-symlinked plan files are refused; dangling
links read as absent.

## Feature 04: agent execution runtime (implemented)

### Adapter contract (`adapters.py`)

Plain-class `AgentAdapter` with `id`, `display_name`, `available()`
(environment-level capability only), `preview()` (deterministic,
side-effect-free), and `start()` (called by the runtime only in execute
mode — authorization is a runtime concern, never an adapter flag).
Built-in adapters:

- **manual** — for agents aperio cannot invoke. Writes a portable
  run-handoff document (`<!-- aperio:run-handoff:v1 -->` marker,
  continuation-indent rendering so plan content cannot forge column-0
  structure) to `.aperio/runs/<run-id>.handoff.md`; the run stays
  `awaiting-external` until `run-report` records the external outcome.
- **local-process** — runs an explicitly configured executable:
  argv array (no shell), working directory resolved inside the project
  boundary (must exist; never created), minimal environment by default
  (`inherit` documented as user-acknowledged risk), bounded 64 KiB/stream
  capture drained by reader threads with a full machine-local mirror
  under `.aperio/local/runs/`, hard timeout (SIGTERM → 5 s → SIGKILL;
  Windows: TerminateProcess), stdin DEVNULL, child always reaped, argv
  NUL-rejected at validation. Without `--authorize-exec` the dispatch is
  an entirely **read-only preview**: no record is created, the task is
  never claimed, and no process is started.

### Execution records (`.aperio/executions.json`, exec-schema v1)

Single file, dict-based, unknown keys preserved; every mutation under
`executions.lock` with single-snapshot byte-CAS. Run record: `run_id`
(`RUN-NNNN`, sequential under the lock), `task_id`, `plan_version`,
`adapter_id`, `status` (`prepared` / `awaiting-external` / `running` /
`completed` / `failed` / `cancelled` / `interrupted`), timestamps,
`exit_code`, neutral metadata (`diagnostics_note`, `external_reported`),
`handoff_ref` (`runs/<run-id>.handoff.md`, the current reference schema),
`local_log_ref` (`local/runs/<run-id>.log`), `pid` + `machine` (recorded
atomically with the running flip; pids are only interpreted on the
recording machine), `previous_run_id` (retries), `outcome_note`
(cancellation precedence). Cancel-vs-finalize precedence: a cancelled
run is never resurrected; cancel of a finalized run is refused.
`plan_changed_since_start` is DERIVED at read time (never stored).
Future exec-schema versions: refused on writes, degraded summary in
memory; corrupt → fatal StateError.

**Privacy boundary for external results.** `outcome_reported` and
`diagnostics` from `run-report` are untrusted and potentially
secret-bearing. They are written together to one machine-local file
(`local/runs/<run-id>.report.txt`, created exclusively — never appended
to or truncated) and the shareable record receives only a fixed neutral
phrase (`external agent reported success|failure`), the success boolean,
text lengths, and an attribution block (`external_reported`). Raw text,
absolute paths, and private identifiers cannot reach `executions.json` or
`aperio memory --json`.

**Run-handoff reads.** `run-handoff` resolves only the current
`handoff_ref` schema, exactly `runs/<run-id>.handoff.md`, inside
`.aperio/runs/`. Traversal, absolute paths, malformed references, a
symlinked `runs/` directory, and a symlinked leaf are refused with a
clean error; the target of any link is never read or modified.

**Artifact path policy.** All run artifacts validate the whole parent
chain (no symlink at any level, no non-directory occupant) and are
created with `O_CREAT|O_EXCL` (+ `O_NOFOLLOW` where available), so
aperio never truncates or appends to a file it did not just create and
never writes outside the project. The same policy covers `run-report`
diagnostics writes. Check-then-create is not atomic against a hostile
process running as the same user (documented residual hazard); the
exclusive-create flag closes the practical race on the leaf.

### Dispatch, authorization and reconciliation

`run-start`/`runs-dispatch` hold `dispatch.lock` for the whole dispatch
(short bounded wait — concurrent dispatch fails fast; same-workspace
parallelism is refused because non-overlapping scopes cannot be
enforced, so 0.4.0 is sequential by design). Eligibility is
re-evaluated against a fresh plan read inside the lock: approved plan,
task pending/in-progress, dependencies completed, no active run,
adapter available. Real subprocess execution additionally requires
`--authorize-exec` — a procedural human-in-the-loop checkpoint, not a
sandbox; preparation mode (no flag) never spawns a process. Claiming
goes through the Feature-03 lifecycle API with a run-scoped owner
(`adapter:RUN-NNNN`); a claim lost to a competing writer finalizes the
record as failed. Retry = new run with `previous_run_id`; a delayed
claim can never silently reopen a completed task (explicit `--reopen`
required). Reconciliation marks dead-pid runs `interrupted`, cancels
never-started prepared records (including prepared records whose task is
no longer claimed, so they cannot block dispatch), reports orphans and
plan-version drift, and refuses to interpret pids recorded on other
machines — it never invents success and never deletes evidence.

**Startup failure boundary.** Both adapters run inside one dispatch error
boundary. If the adapter fails before it starts (handoff blocked by a
symlinked/occupied/unwritable `runs/` directory, unreadable plan, missing
executable, any other subprocess `OSError`), the run is durably finalized
as `failed` with neutral, non-secret text (exception class only — never
message, argv, or config) and a competing cancellation/finalization is
never overwritten. This run's claim is returned to `pending` only when
ownership (`owner == adapter:RUN-NNNN`) and the in-progress state are
positively verified; otherwise the claim is left untouched and the record
is marked `claim_orphaned` for reconciliation to report. Recovery is
explicit: fix the obstruction and dispatch again, or
`task-status <task> --status blocked --reason ...` then `--status pending`.
A failure after the child process was created keeps the claim for an
explicit retry/verification decision (work may have run).

## Portable export, import and integrity (Feature 05)

**Archive contract (format v1).** One ZIP file, deterministic entry order
and timestamps, containing `manifest.json` plus `files/<rel>` entries:

- `manifest.json`: `format` (`aperio-portable-export`), `format_version`
  (1), `aperio_version`, `created_at`, `schemas` (state/plan/executions/
  handoff versions), and a `files` list with `path`, `size`, and SHA-256.
- Exported relative paths are an explicit whitelist: `state.json`,
  `brief.md`, `decisions.md`, `handoff.md`, `plan.json`,
  `executions.json`, and `runs/RUN-NNNN.handoff.md` for awaiting-external
  runs. There is no directory walk, so unlisted content cannot leak in.
- `.aperio/local/`, lock files, reserved temporary files, and the
  generated `.gitignore` are excluded by construction. The importer
  re-validates every path against the same whitelist, so a tampered
  manifest cannot introduce `local/` or traversal paths.
- Execution records are exported as a portable projection: `pid` and
  `machine` are set to null and `local_log_ref` is dropped. A
  prepared/running run blocks execution-metadata export (a live process
  cannot be handed off); a project with an awaiting-external run exports
  its record and handoff document so another machine can finish it.

**Validation before any write.** Import reads and validates the complete
archive in memory: container sanity (regular file, bounded size, ZIP
only), no directory/symlink/encrypted/duplicate members, manifest shape
and supported `format_version`, per-file size and hash equality, bounded
decompression (zip-bomb protection), UTF-8 text only, supported artifact
schemas, and payload relationships (run→task references exist, included
plan revision is not older than any run, awaiting-external runs have their
exact `runs/<run>.handoff.md`, `local_log_ref` stays under `local/`).

**Destination policy.** Existing files are never overwritten by default:
identical bytes are "unchanged"; differing bytes abort with the conflicting
paths, and `--replace` is the explicit approval to substitute them.
Symlinked or non-regular leaves are refused even with `--replace`. A
destination that is not initialized but already has a non-matching
`.aperio/` is refused (only a recoverable partial import with matching
bytes is completed). `state.json` is written last, so a crash cannot leave
a project that merely claims to be imported. Apply runs under
`.aperio/import.lock`; a failing write rolls back this call's created and
replaced files. Multi-file import is not a filesystem transaction and no
`fsync` is issued: a hard crash can leave a subset, which `integrity`
diagnoses and a re-run finishes (matching bytes are accepted).

**Integrity.** `aperio integrity` is diagnostic only. It checks supported
schema versions, full plan graph/lifecycle validity, decision-log
malformed/duplicate entries, handoff format (newer formats are reported,
never converted), execution records and their references, missing expected
files, unexpected leftovers (reserved temp/lock names are called out
specifically), and optionally an archive's manifest. Errors exit 1;
warnings (missing optional files, malformed history, leftovers) do not.

**Schema upgrade foundation.** Versions are detected independently for
state, plan, executions, and handoff. `aperio upgrade` previews a per-
artifact table; `--apply` only runs explicitly registered JSON migrations,
refuses newer or unmigratable versions, writes a backup (originals +
SHA-256 manifest) under `.aperio/local/backups/`, and restores original
bytes if a write fails. No migration path exists in this release; the
registry is intentionally empty rather than fictional.

**Honesty limits.** SHA-256 in the manifest detects corruption and
accidental modification; it does not authenticate the archive and cannot
prove that manually authored shareable text contains no secrets. Aperio
never claims otherwise, and operators are told to review what they share.

## Cross-agent portability principles (implemented)

1. **Plain text, standard formats.** Everything portable is JSON or
   Markdown, readable without Aperio installed.
2. **Self-describing state.** `schema_version` is the first compatibility
   check any reader performs.
3. **Shareable vs local separation.** `.aperio/local/` is the only place
   sensitive data may live; it is git-ignored and excluded from portable
   context by construction. Templates never invite storing credentials.
4. **Crash-safe writes.** State and handoff are written atomically (temp
   file + rename/publish); decision appends are single-syscall.
5. **Idempotent bootstrap.** `init` repairs only what is missing and never
   overwrites user content, so any agent can safely re-run it.

## How agents discover current project state

1. Run `aperio memory <project-directory> --json` — one call returns the
   validated state, brief, decision history, handoff, plan summary (with
   approval state, task counts, ready/blocked tasks), and metadata. (The
   Feature-01 commands `status` and raw `.aperio/state.json` remain
   equivalent starting points.)
2. Check `schema_version` first: above the supported version → stop, report,
   never modify; below 1 → corrupt/unrecognized → report, never modify.
3. Read `brief.md` → `decisions.md` (append-only history; damage is
   reported in metadata) → `handoff.md` (current handoff) → the plan
   summary (approval state and task readiness).
4. To move the project to another machine, `aperio export` the shareable
   context, `aperio import` it there (review with `--dry-run`; `--replace`
   only after inspecting conflicts), then `aperio integrity` — remember
   that `local/` never travels and hashes are not authentication.
5. Write progress back through the APIs: `aperio decision-add` (append
   only), `aperio handoff-write` (validated replacement, optional CAS),
   `aperio phase-set` (compare-and-swap), `aperio task-status` (validated
   transitions, evidence-required completion). Never touch `local/` unless
   a tool explicitly requires it.

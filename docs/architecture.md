# Aperio Architecture — Features 01 & 02 (implemented)

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
├── inputs.py      strict JSON input files for CLI write commands
├── memory.py      portable context read API (ProjectContext, load_context)
├── paths.py       safe path resolution policy (see below)
├── progress.py    phase updates (compare-and-swap state updates)
├── project.py     init/status operations and file templates
└── schema.py      versioned state schema: validate, read, atomic write,
                   replace_state_if_unchanged (deliberate update path)
```

Dependency direction is strictly downward: `cli → memory/decisions/handoff/
progress → project/schema/paths → errors`. Nothing imports upward. The CLI
layer is the only module allowed to print.

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

## Feature 02: portable agent memory (implemented)

### Modules and commands

| Command | Library entry point | Purpose |
|---------|--------------------|---------|
| `aperio memory <dir> [--json]` | `memory.load_context` | read the full shareable context (state, brief, decisions, handoff) with typed metadata |
| `aperio decision-add <dir> (--file <json> \| --title --context --decision --consequences)` | `decisions.append_decision` | append one decision; prints the assigned `DEC-NNNN` id |
| `aperio handoff-write <dir> --file <json>` | `handoff.write_handoff` | validate and replace the structured handoff |
| `aperio phase-set <dir> <phase>` | `progress.update_phase` | update the phase with lost-update detection |

`memory --json` emits a stable schema (keys will not be removed or renamed
within Feature 02; new keys may be added): `reader_version`, `state`,
`brief`, `decisions` (list or null when the file is missing), `handoff`
(structured flag/version/fields/raw), `missing`, `skipped_headings`,
`malformed_entries`, `duplicate_decision_ids`. It never contains
`.aperio/local/` content, environment values, or the local filesystem
path. A shareable file that is a live symlink is refused rather than read
(a link could point at local or private data); dangling links are reported
as missing.

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

Coordination uses advisory lock files (`decisions.lock`, `handoff.lock`;
reserved aperio names, git-ignored) with a bounded wait and stale-lock
breaking (locks older than 10 s are treated as abandoned; lock files
record `pid:timestamp` and are removed only by their verified owner). The
locks coordinate aperio writers with each other — tools that do not use
aperio ignore them entirely.

| Artifact | Mechanism | Guarantee | Limitation |
|----------|-----------|-----------|------------|
| `state.json` create | exclusive temp + hard-link publish | no overwrite, ever | — |
| `state.json` update (`phase-set`) | compare-and-swap on exact validated bytes | concurrent changes landing before the compare abort with nothing modified; **unknown extra state fields are preserved exactly (meaning and JSON type)** | a write between compare and rename can still win; serialize concurrent updaters |
| `decisions.md` | advisory lock around read-compute-append, then single-syscall `O_APPEND` | unique sequential IDs among aperio writers; existing bytes never rewritten; concurrent appends never interleave | **tools that edit the log without aperio's lock bypass uniqueness** — their appends are never overwritten, but their entry IDs may collide with the next aperio-computed ID (reported as duplicates); a live lock held longer than the bounded wait produces a clear conflict, never a hang |
| `handoff.md` | advisory lock + revision counter (`rev=N` in the marker) + final byte check | a stale writer (declaring an older revision) conflicts instead of clobbering; newer unsupported formats (v2+) are refused; concurrent aperio writers serialize | replacing revision ≥ 1 requires declaring that revision, or the opt-in `force` flag (deliberate content loss); tools outside the lock are caught by the byte check and abort with nothing modified |

Crash-consistency is best-effort across all artifacts: no `fsync` is
performed, so a power loss can tear an appended tail or lose a not-yet-
flushed rename. Nothing is ever silently destroyed; leftovers surface as
conflicts for explicit user action.

Schema v1 is preserved by phase updates: only `phase`, `updated_at`, and
`aperio_version` (the rewriting version) change; `name`, `created_at`,
`schema_version` are preserved. Setting the same phase is an idempotent
no-op that does not bump `updated_at`. The decision log is the project's
audit trail — phase transitions are not auto-logged.

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
   validated state, brief, decision history, handoff, and metadata. (The
   Feature-01 commands `status` and raw `.aperio/state.json` remain
   equivalent starting points.)
2. Check `schema_version` first: above the supported version → stop, report,
   never modify; below 1 → corrupt/unrecognized → report, never modify.
3. Read `brief.md` → `decisions.md` (append-only history; damage is
   reported in metadata) → `handoff.md` (current handoff; `structured`
   flag tells you whether it is machine-parseable).
4. Write progress back through the APIs: `aperio decision-add` (append
   only), `aperio handoff-write` (validated replacement, optional CAS),
   `aperio phase-set` (compare-and-swap). Never touch `local/` unless a
   tool explicitly requires it.

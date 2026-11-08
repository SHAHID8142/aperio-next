# Development Conventions

## Language & runtime

- Python 3.11+ (tested on 3.11, 3.12, 3.13, 3.14 via CI matrix).
- **Standard library only at runtime.** Third-party packages are allowed for
  development/testing tooling (currently: pytest) but a runtime dependency
  requires a written justification in this file before it is introduced.
  Runtime dependencies so far: none.

## Code style

- Small modules with one responsibility; no monolithic files.
- Full type hints on public functions (`from __future__ import annotations`
  at the top of each module).
- Docstrings on every module and every public function; keep them accurate
  to the implemented behavior, not the intended future.
- User-facing errors: raise `AperioError` subclasses with single-line,
  actionable messages; the CLI is the only layer that prints.
- Comments explain *why*, not *what*.

## Safety invariants (enforced by tests — keep them true)

- `init` never overwrites existing files; it only creates missing ones.
- Corrupt or too-new `state.json` aborts operations; it is never rewritten
  automatically.
- `status` never writes: no file creation, no mtime changes.
- Paths are validated before any write; symlinks at write targets are
  rejected — live or dangling — for every write target and temporary name
  (`state.json`, `decisions.md`, `handoff.md`, `state.json.tmp`,
  `handoff.md.tmp`), not only the top-level project path. Conflicting
  paths are never destroyed silently: they abort with actionable errors,
  and the user decides what to remove. Nothing pre-existing is ever
  auto-deleted from a reserved temp name (aperio removes only a temp file
  it created within the same write, on failure), file creation is
  exclusive (`O_CREAT | O_EXCL`) so races cannot clobber content, state
  creation is published via atomic create-if-not-exists, and existing
  state is never overwritten except through the explicit compare-and-swap
  update operation.
- The decision log is append-only: existing bytes are never rewritten;
  torn entries are reported as malformed metadata, never edited. Decision
  and handoff formats are injection-hardened: headings are recognized only
  at column 0, titles are single-line, and continuation lines are indented
  so agent-supplied values cannot forge structure.
- Concurrency guarantees are documented per artifact (state: CAS with a
  narrow residual window; decisions: append with possible duplicate IDs;
  handoff: last-writer-wins, optional CAS) — see docs/architecture.md.
  Crash-consistency is best-effort: no fsync.
- No secrets, tokens, or environment contents are ever persisted into
  `.aperio/` except `.aperio/local/`; templates must remain free of
  credential-shaped placeholders (see `tests/test_no_secrets.py`).
- Governance is opt-in: never require `--actor` or impose review gates on a
  project without an enabled `policies.json`. Enforcement lives at the
  Python mutation boundary, not in CLI parsing, and roles always come from
  the configured actor mapping — never from a caller-supplied label.
- Every governed action appends an audit event with declared actor, role,
  action, target, revision, timestamp, and outcome. Audit and review records
  never contain argv, environment content, raw external diagnostics, or
  absolute paths; actor ids are declarations, not authentication.
- A governed plan revision must carry a durable author entry written with the
  plan and a matching audit event before approval. Missing or conflicting
  attribution blocks approval; recovery is explicit (`plan-adopt` for legacy
  plans, `plan-sync-authors` for a lost audit append), never guessed.
- Reviews are immutable and append-only and bind to the task attempt, task
  revision, and recorded implementer; a decided review is never edited,
  a new attempt gets a new `REV-NNNN`, and completion requires recorded
  evidence plus an approved independent review at the current material
  revision. Completion repeats exactly the approved review's evidence and
  limitations; substituted evidence or changed limitations need a new
  review, and work with a recorded failed verification cannot be submitted.
- Whole-project operations acquire writer locks in the fixed global order
  `import`, `dispatch`, `plan`, `executions`, `state`, `handoff`,
  `decisions`, `reviews`, `policy`, and single-writer commands take exactly
  one of those locks.
  Never hold a later lock while waiting for an earlier one; import rejects a
  destination that changed while it was preparing rather than overwriting a
  newer update.
- Portable archives contain only the explicit shareable whitelist:
  `.aperio/local/`, locks, temporary files, and generated scaffolding are
  excluded by construction (not by filtering a directory walk). Imports
  validate the complete archive before any write, never overwrite
  existing bytes without `--replace`, and keep `state.json` last so a
  crash cannot leave a project that claims to be imported. Manifest
  hashes detect corruption only — never describe them as authentication.
- Imported execution records never carry `pid`, `machine`, or
  `local_log_ref`, and no active run may be imported: process identity is
  machine-local evidence that a cancellation could otherwise misuse.
- Subprocess adapters never use a shell. `local-process` and the AI CLI
  providers require `--authorize-exec`; without it dispatch is read-only.
  Provider prompts travel on stdin, not argv, provider configs are
  known-key validated with no arbitrary provider `args`, working
  directories stay inside the project, and unsafe Codex/Claude bypass
  modes are refused. `--authorize-write` is a second, independent consent
  for workspace-write modes; providers default to read-only and a write
  request without that consent fails before any run record or claim.
  Authorized launches must verify the installed CLI advertises the flags
  Aperio relies on. Claude's `--tools` (available tools) and
  `--allowedTools` (auto-approved tools) are derived together from the
  selected mode; Bash is never granted by default. Raw child output,
  provider prompts, and model text never enter shareable records; only
  bounded neutral structured metadata may travel. Exit 0 is never
  verification or completion. These processes are not sandboxed.
- Context packs are read-only compositions. They may read only the
  shareable project surface (never `.aperio/local/`), may read external
  imported/proposed files only when the operator names them, and must not
  copy filenames, absolute local paths, raw provider output, or machine
  logs into the pack. Trust labels describe provenance and review status;
  they are never authentication. Imported/proposed material is never
  promoted to authoritative memory. The byte budget applies to selected
  context content only, and an item is omitted whole rather than truncated.
  Pack generation must never launch an agent or be described as execution,
  approval, or proof of performance improvement.
- Never commit secrets, credentials, tokens, private client information, or
  machine-specific configuration to this repository.

## Testing

- Every feature ships with tests in the same change. No test is marked
  passing unless it actually ran.
- CLI behavior is tested in-process via `aperio.cli.main` plus subprocess
  tests for `python -m aperio` and the console script.
- Run: `python -m pip install -e ".[dev]" && python -m pytest`
- New safety behaviors need both a positive case and an adversarial case
  (what happens when a user/something else tampers with the directory).

## Schema ownership

- Project state (`schema.py`), plan-schema v1 (`planmodel.py`), the context
  pack (`context_pack.py`), and the portable archive v1 (`portable.py`) are
  independently versioned and owned by their modules.
- `goal-schema v1` is owned by `aperio/goals.py` and the profile registry
  (`profile v1`) by `aperio/profiles.py`. Both are universal-layer contracts:
  stdlib only, no provider import, no project dependency, and no write path
  outside an explicitly requested output file.
- Adding a field to a versioned contract means bumping that contract's
  version and keeping a reader for the older form. A newer version is
  refused with a clear error, never silently partially read.

## Versioning

- Single version, declared in `pyproject.toml` and mirrored in
  `aperio/__init__.py`; `tests/test_cli.py` asserts they match.
- Schema version is independent of package version (bump it only when
  `.aperio/state.json`'s meaning changes).

## Git

- Conventional, descriptive commit messages (`feat:`, `fix:`, `docs:`,
  `test:`, `chore:`).
- CI (`.github/workflows/ci.yml`) runs the full test suite on every push to
  `main` and every pull request; it must be green before merge.

# Aperio Architecture — Feature 01 (implemented)

This document describes what Aperio **is today**. Planned capabilities are in
[roadmap.md](roadmap.md) and are never described here as working.

## Mission

Aperio is a **model-agnostic, multi-agent software development orchestrator**:
a runtime in which multiple AI agents (possibly powered by different models)
collaborate on one software project safely, with durable, portable project
memory.

Feature 01 delivers only the foundation: a CLI that bootstraps and inspects a
portable, versioned project-state directory.

## Explicit non-goals (for the whole project)

- Aperio is **not** a model provider, proxy, or key manager. It never talks to
  model APIs and never handles credentials itself.
- Aperio does not run inside a vendor's sandbox as a hidden dependency; it is
  a plain, inspectable CLI and library.
- Aperio does not replace version control. Git remains the source of truth
  for code history; `.aperio/` is project *memory*, not a VCS.
- Aperio does not make model choices for users, and its project state must
  never embed model-specific or vendor-specific assumptions.

## Package layout (implemented)

```
src/aperio/
├── __init__.py    package version (single source: mirrored by pyproject)
├── __main__.py    python -m aperio entry point
├── cli.py         argparse CLI; catches AperioError, maps to exit codes
├── errors.py      AperioError + PathError / NotInitializedError / StateError
├── paths.py       safe path resolution policy (see below)
├── project.py     init/status operations and file templates
└── schema.py      versioned state schema: validate, read, atomic write
```

Dependency direction is strictly downward: `cli → project → schema/paths →
errors`. Nothing imports upward. The CLI layer is the only module allowed to
print.

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

- `state.json.tmp` is a **reserved** Aperio temporary name that is never
  deleted automatically: ownership of a plain file cannot be verified, so
  *any* occupant — a leftover from a crashed write, a directory, or a
  symlink live or dangling — aborts `init` with an actionable error and is
  resolved by explicit user action. The name is git-ignored in generated
  projects.
- State writes are atomic and non-destructive: the temp file is created
  exclusively (an unexpected file appearing in the race window can never
  be truncated), the publish is an atomic hard-link create-if-not-exists
  (even a state file planted mid-write is not destroyed), an existing
  state file is never overwritten through `write_state`, symlinked
  targets are refused, and on failure only the temp file created by that
  same call is removed — nothing pre-existing is ever auto-deleted.
- A directory or other non-regular occupant at a context-file path (e.g.
  `.aperio/brief.md`) is an explicit error, never a false "present" report.
- I/O failures during state writes (permissions, conflicting paths) surface
  as clean, actionable errors rather than tracebacks.

## `.aperio/` project state (implemented)

| Path | Role | Portable? |
|------|------|-----------|
| `state.json` | versioned, machine-readable state; single source of truth | yes |
| `brief.md` | what the project is building and why | yes |
| `decisions.md` | append-only decision log | yes |
| `handoff.md` | current handoff notes (replaced, not appended) | yes |
| `.gitignore` | keeps `local/` out of any VCS | yes |
| `local/` | machine-local and sensitive data | **no — never shared** |

Schema v1 (`state.json`): `schema_version`, `name`, `created_at`,
`updated_at`, `phase`, `aperio_version` — all required; unknown extra keys
are tolerated for forward compatibility; unknown *versions* abort rather
than being modified.

## Cross-agent portability principles (implemented)

1. **Plain text, standard formats.** Everything portable is JSON or
   Markdown, readable without Aperio installed.
2. **Self-describing state.** `schema_version` is the first compatibility
   check any reader performs.
3. **Shareable vs local separation.** `.aperio/local/` is the only place
   sensitive data may live; it is git-ignored and excluded from portable
   context by construction. Templates never invite storing credentials.
4. **Crash-safe writes.** State is written atomically (temp file + rename).
5. **Idempotent bootstrap.** `init` repairs only what is missing and never
   overwrites user content, so any agent can safely re-run it.

## How future agents discover current project state

1. Run `aperio status <project-directory>` (or read `.aperio/state.json`).
2. Check `schema_version` first: above the supported version → stop, report,
   never modify; below 1 → corrupt/unrecognized → report, never modify.
3. Read `brief.md` → `decisions.md` (append-only history) → `handoff.md`
   (current handoff).
4. Write progress back the same way: update `handoff.md`, append to
   `decisions.md`, never touch `local/` unless a tool explicitly requires it.

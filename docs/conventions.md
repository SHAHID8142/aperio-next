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
  rejected — live or dangling — including the individual files and
  directories inside `.aperio/` (including `state.json` itself) and the
  reserved `state.json.tmp` name, not only the top-level project path.
  Conflicting paths are never destroyed silently: they abort with
  actionable errors, and the user decides what to remove. Nothing
  pre-existing is ever auto-deleted from `state.json.tmp` (aperio removes
  only a temp file it created within the same write, on failure), file
  creation is exclusive (`O_CREAT | O_EXCL`) so races cannot clobber
  content, state is published via atomic create-if-not-exists, and
  existing state is never overwritten by a plain write.
- No secrets, tokens, or environment contents are ever persisted into
  `.aperio/` except `.aperio/local/`; templates must remain free of
  credential-shaped placeholders (see `tests/test_no_secrets.py`).
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

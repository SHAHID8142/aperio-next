# Aperio security model

What Aperio protects, how, and what it explicitly does not. This page is
the standing summary of the Chunk 07 security audit; the per-feature
invariants live in `docs/conventions.md`, and the executable evidence is
the test suite (each claim below names where it is pinned).

## Authority and instruction hierarchy

- **Operator instructions are authoritative.** Goal packs render only
  operator-declared goals into instruction sections; agent/import-declared
  goals appear verbatim as *proposed* instructions under an explicit
  heading, with `content.goal.authority: operator|untrusted` keeping JSON
  and Markdown aligned (`tests/test_goals.py`, `tests/test_memory_context_boundary.py`).
- Agent briefs refuse non-operator goals outright (`tests/test_guidance.py`).
- Feedback documents start `proposed`; only an explicit review decision
  accepts one; an accepted lesson enters memory as `proposed` — the M3
  review contract remains the only promotion path
  (`tests/test_feedback.py`).

## Trust derivation and review transitions

- Memory authority is derived from recorded review events by one function
  (`memstore.derive_verification` / `resolve_trust`) used by every reader:
  snapshots, recall, context items, both pack builders, briefs, and the
  exchange validator. A label or state claim the history does not support
  is rewritten down or refused; a claimed state the record's own event log
  contradicts is reported via `verification.replayed_state`
  (`tests/test_memory_state_proof.py`, `tests/test_memory_boundary.py`).
- Review transitions are append-only with an allowlist state machine;
  terminal states cannot flip back; history is never rewritten
  (`tests/test_memstore.py`).
- Verification requires recorded evidence, a declared reviewer, and
  reviewer ≠ declared proposer (`tests/test_memory_gate.py`).

## Prompt injection and structure forging

- Decision-log and handoff formats recognize headings only at column 0,
  single-line titles, and indented continuations
  (`tests/test_boundary.py`).
- Context packs render quoted data through `> ` block quoting
  (`context_pack._quote_data`).
- Agent briefs indent quoted knowledge so no line of quoted content can
  sit at column 0 — a memory carrying forged `## ` headings cannot forge
  brief structure (`tests/test_security_audit.py::test_memory_content_cannot_forge_column_zero_headings`).
- Quoted project content is data, not permission; packs and briefs state
  the boundary in their own text.

## Imported content and portable exchange

- Imports validate the complete document before any write: envelope,
  digests, per-section owning-contract validation, bounded payloads.
  Unknown section kinds and newer format versions fail closed
  (`tests/test_exchange.py`).
- Imported files use content-derived `imported-*` names and never
  overwrite existing bytes; imported goals are reported as untrusted
  input pending review and provenance re-declaration.
- Imported memories always enter as `proposed`, whatever state the file
  declares; unsupported trust claims are listed, never applied
  (`tests/test_memportable.py`, `tests/test_exchange.py`).

## Privacy

- `.aperio/local/`, raw provider output, machine logs, environment
  values, and absolute paths never enter packs, briefs, reports, quality
  findings, exchange sections, or benchmark artifacts
  (`tests/test_no_secrets.py`, `tests/test_security_audit.py::test_generated_artifacts_never_contain_local_paths`).
- `private` memory is excluded from packs, briefs, exchanges, and
  benchmarks by default; including it anywhere requires the deliberate
  `--include-private`/`--max-sensitivity private` pair, and briefs cap
  private out unconditionally (`tests/test_guidance.py`,
  `tests/test_security_audit.py`).
- Sensitivity is a filter, not encryption: stores are plain text, so do
  not put secrets in a memory store (documented in
  `docs/universal-memory.md`).

## Paths, symlinks, publication, and concurrency

- Write targets (including every temp/lock name) refuse live and dangling
  symlinks; ancestor links are followed transparently and the resolved
  path is echoed back (`tests/test_boundary.py`).
- Operator-visible outputs publish through `artifacts.publish_text_atomic`:
  exclusive temp creation, atomic no-clobber publication, `--force` for
  replacement, cleanup of only the call's own temp file, fail-closed when
  hard links are unavailable (`tests/test_goal_output.py`).
- Writers hold advisory locks; CAS compares guard state, handoff, plan,
  executions, and feedback-review `--replace`; import re-checks the
  destination snapshot under all locks
  (`docs/architecture.md`, `tests/test_feedback.py::test_feedback_review_replace_requires_expected_revision`).
- Memory appends open without following symlinks, prove the descriptor,
  re-verify the chain through it, and write complete events only
  (`tests/test_memstore.py`).

## Size limits and corrupted state

- Every new contract bounds its inputs (goal ≤ 64 KiB, deliverable ≤ 1 MiB,
  feedback ≤ 64 KiB, exchange sections capped, benchmark cases ≤ 256 KiB,
  request lists ≤ 100) and refuses oversize rather than truncating.
- Corrupt or too-new state aborts operations and is never rewritten;
  damaged store lines are reported and skipped; a broken chain refuses
  further appends (`tests/test_memstore.py`, `tests/test_memory.py`).

## Provider process handling

- No shell, prompt-on-stdin, no arbitrary provider args, capability probes
  before authorized launch, two independent consents, bounded capture with
  machine-local-only raw output, hard timeouts, owned process-group kill,
  neutral metadata only in shareable records. Exit 0 is never verification
  (`tests/test_provider_adapters.py`, `tests/test_provider_metadata_bounds.py`).
- One shared metadata contract spans adapter and persistence boundaries;
  beyond-contract magnitudes are recorded as honest absence, never as
  values that would fail validation.

## Destructive operations

- `init` repairs only what is missing; nothing pre-existing is ever
  auto-deleted; exchange import never overwrites; `--replace` always
  requires explicit approval or a matching CAS revision; there is no
  public completion bypass (`tests/test_init.py`, `tests/test_governance_safety.py`).

## What Aperio does not claim

- Hashes detect corruption; they do not authenticate authorship.
- Actor identifiers are declarations, not authentication.
- Sensitivity filters are not secrecy mechanisms.
- `--authorize-exec` is a procedural checkpoint, not a sandbox.
- A clean quality report or a passing benchmark is not a certificate of
  correctness; the reports themselves say so.

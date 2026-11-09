# Aperio Next — Master Completion Plan

Durable progress document for the final master directive: complete the
remaining project from the verified baseline to a 1.0 release candidate.
Recovered at every context boundary from this file plus the repository.

## Verified baseline

- **Repository:** https://github.com/SHAHID8142/aperio-next (local checkout:
  `/Volumes/SSD/CODES/Aperio/aperio-next`, branch `main`)
- **Baseline HEAD:** `fdadf0f8f458de8d9f8eec69fe952044a06c1980`
  (`feat(m3): re-derive memory trust at the direct context boundary`)
- **Version:** 0.13.0 (pyproject.toml == `aperio/__init__.py`)
- **Baseline test result:** `pytest` → **1093 passed** (~60 s, local
  `.venv`, Python 3.14.7, macOS arm64)
- **CI status:** GitHub Actions runs fail at start — *"The job was not
  started because your account is locked due to a billing issue."* Zero test
  steps execute in Actions. This is an external infrastructure blocker; all
  verification in this assignment is local and is reported as such (Rule G).
  Every chunk is verified locally with the full suite before its commit.
- **Existing capability:** Features 01–07, M1 (context packs), M2 (goal
  contract + profiles), M3 (universal memory + trust boundary) are
  implemented and documented in `docs/architecture.md`, `docs/conventions.md`,
  `docs/roadmap.md`, `docs/context-pack.md`, `docs/goal-contract.md`,
  `docs/universal-memory.md`, `docs/feature-07.md`.

## Final product requirements (from the master directive)

A universal, agent-agnostic AI performance, context, memory, and reliability
enhancement layer that works for design, research, writing, business,
everyday and optional software work; usable through manual copy/paste and
ordinary files; no mandatory cloud, model API, IDE, database, or
subscription. See directive sections 1, 14, and 16; the global acceptance
criteria are §16 of the directive and are re-verified in Chunk 08.

## Chunk order and status

| Chunk | Deliverable | Status | Commit |
|---|---|---|---|
| 00 | Existing code audit + M3 final trust stabilization | COMPLETE | 4997fe9 |
| 01 | M4 — universal mistake prevention + quality evaluation | COMPLETE | 4d158fd |
| 02 | M5 — controlled feedback and improvement | COMPLETE | 247d4f3 |
| 03 | M6 — universal portable exchange v2 | COMPLETE | 57fff8b |
| 04 | M7 — optional integration + domain adaptation | PENDING | — |
| 05 | Feature 07 provider reliability closure | PENDING | — |
| 06 | M8 — cross-domain benchmark + evaluation | PENDING | — |
| 07 | Integration, security, usability | PENDING | — |
| 08 | Final release engineering, 1.0 RC | PENDING | — |

Chunk statuses: `PENDING`, `IN_PROGRESS`, `BLOCKED`, `FAILED`, `COMPLETE`.
A chunk is marked COMPLETE only after its acceptance criteria actually pass
locally (full regression suite included) and its commit SHA is recorded.

## Confirmed defects

Recorded as they are confirmed; each closed defect lists its regression test.

| ID | Chunk | Severity | Summary | Status |
|---|---|---|---|---|
| D-00-1 | 00 | P1 | Raw/caller-supplied memory records could claim `state="attested"` (or carry the recorded `attested` state) without any recorded `accept` transition and receive attested authority in recall/packs; derivation now requires the recorded transition for every active state, not only `verified` | CLOSED (commit recorded below) |
| D-05-1 | 05 | P1 | (Known) `adapters._bounded_usage()` accepts ≤ 10**12 but `runtime._validate_provider_result()` rejects > 1_000_000_000; Claude `num_turns`/`duration_ms` share mismatch | OPEN → Chunk 05 |

## Outstanding blockers

- **B-CI (external):** GitHub Actions billing lock — Actions executes zero
  steps. Unresolvable locally. Reported honestly in every chunk; final report
  distinguishes local verification from CI verification.

## Exact next action

Maintained at the end of the "Status" section per chunk. Current: **Chunk 04
— M7 optional agent/provider integration and domain adaptation.**

## Chunk notes (evidence log)

Appended per chunk: sources changed, tests added, results, decisions.

### Chunk 00 — COMPLETE (0.13.1, commit 4997fe9b1321eb550322c750e595cddfab5dc5cd)

**Audit findings.** Baseline `fdadf0f` (v0.13.0) reviewed against
`docs/architecture.md`, `docs/conventions.md`, `docs/roadmap.md`,
`docs/context-pack.md`, `docs/goal-contract.md`, `docs/universal-memory.md`,
`docs/feature-07.md`, and the source/test tree. The prior M3 gates
(0.10.1 P1-A/B/C, 0.11.0 effective trust, 0.12.0 entry points, 0.13.0
trust-consumption) are genuinely closed. One confirmed P1 remained, exactly
the suspected one:

- **D-00-1 (P1, CLOSED):** a caller-built record claiming
  `state="attested"` with no recorded `accept` transition received attested
  authority everywhere — derivation, recall ranking (including the
  reviewed-memory global-scope bypass), context items, and both pack
  builders. Closed by requiring the producing transition for every active
  state in the one shared derivation (`memstore.derive_verification` /
  `effective_trust`); unsupported `attested` → `proposed` with
  `verification.basis: unrecorded`; declared labels above the derivation are
  refused; the `verified`-without-proof → `attested` legacy contract is
  unchanged. Second hole closed in the same pass: a claimed state the
  record's own history contradicts now reports
  `verification.replayed_state`, is refused as support, and recall excludes
  records whose replay ends retracted/superseded; `memory_context_items`
  refuses them.
- Audited and found already sound: imported provenance (0.12), direct
  ContextItem construction (0.13 + downgrade surfacing), privacy
  classification (sensitivity/identity fail-closed), expiry/withdrawal,
  deterministic output, content identity, append/file safety and
  concurrency (P1-C). Exchange files deliberately unchanged: attested
  entries import as `proposed` regardless (and an `attested` record can
  have at most one review event, so a history-less attested entry is always
  forged — but imports never promote, so acceptance-as-data stays safe).

**Sources changed:** `src/aperio/memstore.py` (derivation +
`effective_trust`), `src/aperio/memrecall.py` (derived-trust ranking bypass,
replayed-inactive exclusion, `reviewed` from derived trust, warning text),
`src/aperio/context_pack.py` (downgrade surfacing for unsupported state
claims, warning text), `tests/test_memory_state_proof.py` (new, 18 tests),
fixture fixes in `tests/test_memrecall.py` / `tests/test_memory_boundary.py`
(records now carry the accept transition their claimed state requires),
`tests/test_memory_gate.py` (verification dict gains `replayed_state`),
README + docs/universal-memory.md + docs/roadmap.md, version 0.13.1.

**Tests:** 1093 baseline → **1111 passed** locally (0 failures), Python
3.14.7. GitHub Actions: not run (billing lock — see Blockers).

### Chunk 01 — COMPLETE (0.14.0, commit 4d158fdfabf5396ab4fec7260b84686c5ed1db2b)

**Delivered (M4).** New module `src/aperio/quality.py`: one deterministic
engine over a declarative rule registry (quality-rules v1), a stable
11-category mistake taxonomy (taxonomy v1), and honest finding statuses
(`confirmed` / `warning` / `unknown` / `recommendation`). Two read-only
reports from one canonical payload with reproducible `content_sha256` and
content-derived issue ids:

- `aperio preflight GOAL [--memory-store F] [--json] [--output F]` — unclear
  objective, missing acceptance/deliverables/quality criteria, duplicate
  instructions, conflicting stated length bounds, untrusted goal
  provenance, unvalidated assumptions, unaddressed risks, oversized
  instruction lists, and (read-only) expired/unreviewed/conflicting memory.
- `aperio quality-check GOAL --deliverable FILE [--request F]
  [--require-section/--require-text/--forbid-text/--min-words/--max-words/
  --no-output-contract]` — operator-required sections/texts, forbidden
  text, modal constraint extraction ("must include X" / "must not … Y" with
  a negation-context guard), stated length bounds (leading and trailing
  phrasing; 10% tolerance band for approximate targets; pages/characters
  reported, never converted), output-format expectations (JSON parse =
  confirmed), deliverable coverage (warning-only signal), source
  attribution + absolute-claim warnings under evidence requirements, an
  explicit `unknown` status for source support, limitation language, and a
  software-profile verification-evidence rule.
- New versioned `quality-request` (request-schema v1) contract; deliverable
  bounded at 1 MiB UTF-8; `--output` uses the shared atomic publication
  path; reports never contain absolute paths.

**Safety.** No untrusted content executed, no memory writes (byte-identical
store proven by test), no goal/deliverable alteration, no governance
authority — Feature 06 completion gates untouched (asserted by the full
suite). CLI prints via the standard single-line `aperio: error:` failures
for malformed input (goal, deliverable, request all covered).

**Acceptance evidence.** Tests demonstrate at least one real detected error
in each of the six domains (general, design, research, writing, business,
software) at both engine and CLI level, a passing deliverable with zero
confirmed findings per domain pair, unknown-status handling, determinism
(same inputs → same issue ids and content hash), false-positive guards
(negated prohibitions, token coverage, request/file agreement), malformed
inputs, and compatibility (full suite green).

**Sources changed:** `src/aperio/quality.py` (new), `src/aperio/cli.py`
(two subparsers + handlers + dispatch), `tests/test_quality.py` (28),
`tests/test_quality_cli.py` (17), `docs/quality.md` (new), README,
docs/architecture.md, docs/roadmap.md, version 0.14.0.

**Tests:** 1111 → **1160 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 02 — COMPLETE (0.15.0, commit 247d4f3ae86ab8894e0c959cef426599cc35b611)

**Delivered (M5).** New module `src/aperio/feedback.py` + five CLI
commands. Feedback documents (feedback-schema v1) start as proposals with
content-derived identity (FB-…, verified if declared), bounded fields,
append-only review entries, calendar-valid UTC timestamps, and final
decisions (`--replace` is a revision CAS against parallel review). Only an
explicit accepted decision produces a lesson, and the lesson enters the
operator-named M3 store as a NEW PROPOSED memory — the M3 review contract
stays the only memory authority; agent-origin feedback and agent
self-reported evaluations are flagged (`feedback_issues`, stderr), never
silent, and never promote. `feedback-compare` deterministically compares
two quality reports for the same goal: only confirmed/warning findings
count as resolved; new confirmed/warning findings are reported as
potential regressions; the outcome label (improved/regressed/mixed/
unchanged) is computed from those sets and the report states it cannot
attribute causes. Improvement proposals (improvement-proposal v1) target
goal instruction fields and quality-request fields only, apply to a NEW
copy via publish_text_atomic, refuse no-ops/duplicates, and cannot modify
the code-owned profile registry.

**Acceptance evidence.** tests/test_feedback.py walks the full directive
loop end-to-end through the CLI (task → output → quality issue → feedback
→ explicit review → proposed lesson memory → mem-review accept → future
task retrieval → feedback-compare 'improved'), plus the negative case
(worse deliverable → 'regressed' with new findings listed), rejected
feedback refused, agent-origin lessons still enter as proposed, no-op and
duplicate proposals refused, originals byte-identical after apply, CAS
conflict behavior, and malformed-input refusals.

**Sources changed:** `src/aperio/feedback.py` (new), `src/aperio/cli.py`
(5 subparsers + handlers + dispatch), `tests/test_feedback.py` (19),
`docs/feedback.md` (new), README, docs/architecture.md, docs/roadmap.md,
version 0.15.0.

**Tests:** 1160 → **1179 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

### Chunk 03 — COMPLETE (0.16.0, commit 57fff8b8207c7840edf992012d6deb8a1938a7c8)

**Delivered (M6).** New module `src/aperio/exchange.py` + `exchange-export`
/ `exchange-import` CLI. One `aperio-universal-exchange` v1 document whose
sections are already-versioned contracts (goal-schema v1, context-pack v1
envelope with re-verified hash, quality-report v1 envelope, feedback-schema
v1, `aperio-memory-export` v1 for memories, bounded operator notes),
validated by their owning contracts at BOTH export and import,
digest-checked per section and for the whole document, deterministically
ordered, and capped. Selection is explicit (named files; memories need
--memory-id/--memory-tag; private memory needs the deliberate opt-in).
Imports preview by default (`--apply` to write), write
`imported-<kind>-<digest>.json` files that never overwrite existing bytes
(identical → `already-present`; different content at the digest-derived
name → refused conflict), bring memories in as `proposed` through the
ordinary M3 import (duplicates/conflicts/unsupported trust claims
reported), warn on every imported goal as untrusted pending review and
provenance re-declaration, and skip memory sections with a reason when no
--memory-store is given. Newer format versions and unknown section kinds
fail closed. Feature 05 archive v1 and M3 memory-exchange v1 unchanged
(asserted by test).

**Acceptance evidence.** tests/test_exchange.py walks ChatGPT → Aperio →
Claude → Aperio → Gemini/GLM entirely through CLI-produced portable
payloads: the receiving machine's store holds only `proposed` memories,
the unsupported claim (hand-appended unproven verify) arrives declared
`verified` but reads `proposed` with `declared_state` preserved, the
accepted lesson travels and stays proposed, re-import reports duplicate +
already-present, tampering (payload/digest/format_version/unknown kind) is
refused, and determinism holds regardless of build order.

**Fixed during the chunk:** a CLI name collision (`load_exchange_file`
from memportable shadowing the exchange module's loader — aliased) and a
build-side gap (export now runs the same per-section owning-contract
validation import applies).

**Sources changed:** `src/aperio/exchange.py` (new), `src/aperio/cli.py`
(2 subparsers + handlers + dispatch + alias), `tests/test_exchange.py`
(11), `docs/exchange.md` (new), README, docs/architecture.md,
docs/roadmap.md, version 0.16.0.

**Tests:** 1179 → **1189 passed** locally (0 failures). GitHub Actions: not
run (billing lock).

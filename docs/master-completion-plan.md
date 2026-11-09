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
| 00 | Existing code audit + M3 final trust stabilization | COMPLETE | see Chunk 00 note |
| 01 | M4 — universal mistake prevention + quality evaluation | PENDING | — |
| 02 | M5 — controlled feedback and improvement | PENDING | — |
| 03 | M6 — universal portable exchange v2 | PENDING | — |
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

Maintained at the end of the "Status" section per chunk. Current: **Chunk 01
— M4 universal mistake prevention and quality evaluation.**

## Chunk notes (evidence log)

Appended per chunk: sources changed, tests added, results, decisions.

### Chunk 00 — COMPLETE (0.13.1)

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

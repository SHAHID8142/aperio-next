# Aperio Next — Master Implementation Plan v2 (product completion program)

The durable plan for the final product-completion assignment before the
independent external audit. The v1 program (Chunks 00–08, 1.0.0rc1) is
recorded in [master-completion-plan.md](master-completion-plan.md) and is
complete.

## Verified baseline (established fresh, per the directive)

- **HEAD at program start:** `6eca49f5f3fdbbc1fe0bcee84b2c3b73a4c126e3`
  (1.0.0rc1) — confirmed unchanged on `main`; no assumed drift.
- **Baseline defect found and fixed before any feature work:** the
  `test_memory_gate` validity dates used the machine's *local* date while
  the product evaluates in UTC; in UTC+ timezones between local midnight
  and UTC midnight, "expired" records evaluated as current and the export
  test failed. Fixed in UTC frame + widened margins: commit `56535e3`
  (1248 tests green on 3.14.7 and 3.12.14 afterwards, run twice each).
  This was the same one-off flake triaged during the 1.0.0rc1
  verification; it is now understood and closed.
- **CI:** GitHub Actions still fails at job start — *"account is locked
  due to a billing issue"* (external, owner action required). Zero CI
  steps execute; no CI success is claimed anywhere.
- **Capability inventory (exists, do not rebuild):** goal contracts +
  packs (M1/M2), universal memory with trust derivation (M3), quality
  reports (M4), feedback/lessons (M5), universal exchange (M6), agent
  briefs + profiles (M7), benchmark (M8), plan/task lifecycle with
  approval + independent review (F03/F06), execution runtime with
  manual/local-process/codex-cli/claude-cli adapters (F04/F07),
  portable project archive (F05).

## Chunks (dependency order)

| Chunk | Deliverable | Status | Commit |
|---|---|---|---|
| 00 | Baseline + architecture closure | COMPLETE | 56535e3 |
| 01 | Brain Dump Guard + durable intake | COMPLETE | 3ce88b5 |
| 02 | Task continuity: checkpoints + recovery | COMPLETE | this chunk |
| 03 | Goal→plan bridge + priority-aware scheduling | PENDING | — |
| 04 | Controlled multi-agent orchestration (sequential-first) | PENDING | — |
| 05 | Trust-aware portable skill packs (+ builtin packs) | PENDING | — |
| 06 | Website-delivery + 30-day SEO workflows (templates + docs) | PENDING | — |
| 07 | UX: work-status view, read-only loopback UI, host matrix | PENDING | — |
| 08 | E2E A–Z simulation + full audit matrices | PENDING | — |
| 09 | Packaging, docs, release readiness | PENDING | — |

Statuses: PENDING / IN_PROGRESS / BLOCKED / FAILED / COMPLETE. A chunk is
COMPLETE only when its acceptance criteria pass locally (full suite
included) and its SHA is recorded.

## Locked design decisions (recorded before implementation)

1. **Intake (Chunk 01).** Durable append-only capture log
   (`.aperio/inbox.jsonl`, chain-hashed, locked, same append-safety
   patterns as the memory store). Deterministic lexicon classification
   is labeled `classification_basis: heuristic-keyword` — never claimed
   as language understanding; low-confidence items are `ambiguous` and
   keep the raw text. **Emergency authority never derives from message
   content**: urgency language is *detected and reported*, but only the
   operator's explicit `--urgent` flag on the capture invocation (the
   trusted operator channel) records operator-requested urgency.
   Quoted/imported/agent text can never set it. Nothing captured ever
   auto-executes; promotion is a separate explicit operation that flows
   through the existing plan gates.
2. **Checkpoints (Chunk 02).** One CAS document (`.aperio/checkpoint.json`,
   checkpoint-schema v1) derived from durable state (plan, executions,
   inbox, decisions) and refreshed at meaningful transitions (task
   status changes, run reports, inbox operations). Recovery is an
   inspection report: validate, compare against the live workspace,
   separate verified completion (plan lifecycle evidence) from unverified
   agent claims, and require review when ambiguous. Retries are
   idempotent re-derivation; nothing that was never recorded is claimed
   recoverable.
3. **Goal→plan (Chunk 03).** A bridge that converts a goal's *descriptive*
   work decomposition into a plan **proposal**; creating the plan is the
   normal gated `plan-create` (approval draft) — a work breakdown is
   never presented as an approved plan. Plan-schema v2 adds optional
   task `priority` (enum); v1 plans read compatibly (all `normal`);
   nothing historic is reinterpreted. Scheduling orders ready tasks by
   (priority rank, task id) and reports the rationale; all ready tasks
   remain visible in listings (no hidden starvation) and `--ignore-priority`
   restores pure id order.
4. **Orchestration (Chunk 04).** Assessment → orchestration plan
   (orchestration-schema v1) mapping goal work onto REAL plan tasks
   through the existing scheduler/adapters — no second execution engine.
   Sequential dispatch only: parallel mode is not offered because
   non-overlapping write-scope isolation cannot be proven for the
   existing adapters (documented fallback). Specialist context = scoped
   agent briefs; implementer-approves-own-work stays impossible under
   Feature 06 governance; missing specialists degrade to the documented
   manual path.
5. **Skills (Chunk 05).** Operator-named skill directories holding
   SKILL.md-compatible Markdown (bounded, no executable content);
   imports enter `untrusted` until an explicit operator review approves
   them; selection is scope/applicability matching; conflicts are
   reported with versioned resolution rules (operator requirement >
   higher version > explicit conflict declaration); briefs record which
   skills influenced them; exports carry only reviewed skills and never
   private material. Builtin packs ship with provenance
   `aperio-builtin-v1` and operator requirements always outrank them.
6. **Workflows (Chunk 06).** Website-delivery and 30-day-SEO are
   template/skill/walkthrough layers on universal primitives — no new
   engines, no invented client data, no ranking guarantees, measured SEO
   data only with real Search Console/analytics access (otherwise
   documented manual steps and honest unknowns).
7. **UI (Chunk 07).** Minimal OPTIONAL **read-only** loopback server
   (`serve-ui`): binds 127.0.0.1 only, per-launch random token, no POST
   endpoints, no filesystem operations beyond reading project state, no
   remote assets. A read-only view cannot mutate state, which is what
   makes it safe within stdlib-only constraints. The CLI remains the
   primary interface. Host-integration claims are limited to a truthful
   capability matrix.
8. **Testing (Chunk 08).** The mandatory brain-dump/orchestration/skill/
   release matrices are covered by each chunk's suite plus a final A–Z
   simulation from an empty workspace; all legacy tests must keep
   passing; any expectation change must trace to a documented contract
   change.
9. **Version target.** 1.1.0rc1 at program end (release candidate —
   external validation pending), unless a blocker forces otherwise.

## Confirmed defects (this program)

| ID | Chunk | Severity | Summary | Status |
|---|---|---|---|---|
| D-10-1 | 00 | P1 (test) | Gate tests used local-date validity frames vs product UTC evaluation → boundary-window failures | CLOSED (56535e3) |

## Blockers

- **B-CI (external, unchanged):** GitHub Actions billing lock; zero CI
  steps. Reported honestly everywhere.

## Exact next action

Current: **Chunk 03 — Goal→plan bridge + priority-aware scheduling.**

## Chunk notes (evidence log)

Appended per chunk with sources, tests, results, and SHAs.

### Chunk 01 — COMPLETE (commit 3ce88b5, on top of 56535e3)

Brain Dump Guard runtime path: `src/aperio/intake.py` + `capture`/`inbox`/
`inbox-resolve` CLI + docs/brain-dump.md. Append-only chain-hashed intake
log with decide-under-lock semantics (IDs, duplicate checks, and chain
head computed inside the lock — a race found and fixed during the chunk's
own testing). Deterministic lexicon classification labeled
heuristic-keyword everywhere; emergency authority only via --urgent
(text urgency language detected and reported with authority: none);
nothing captured executes; active-task identity durable across plan
revisions (approval state reported separately). 20 tests. One defect
found by the chunk's own tests and fixed before commit: unlocked
chain-head computation allowed concurrent appends to corrupt each other.

### Chunk 02 — COMPLETE (this commit)

`src/aperio/checkpoint.py` + `checkpoint-write`/`checkpoint-show`/
`recover` CLI + automatic refresh hooks at meaningful transitions
(task-status, run-report, phase-set, capture, inbox-resolve — hook
failure never corrupts the transition). Checkpoint = CAS document
(checkpoint-schema v1) derived ONLY from durable artifacts: active task,
task summary with verified_completed (plan-lifecycle evidence) vs
completed_without_evidence, unverified signals, inbox pending/urgent,
next action (urgent > active-runs > in-progress > dispatch > approval
gates), resume eligibility with reasons. Recovery report: validates,
detects drift (plan_version/active task/intake moved), separates
verified from claimed work, gates resume on draft plans/active runs,
never invents completion, never auto-repairs damage. Idempotent
re-derivation keeps revision unless content or reason changed. 14 tests.
Two hook-attribute defects (wrong Namespace attr per handler) found and
fixed during the chunk's own tests. P2 note: one planstore concurrency
test flaked once under full-suite load (passes 3x isolated and 3x in
suite) — added to the de-flake triage.

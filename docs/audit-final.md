# Final audit — Aperio Next 1.2.0rc2

Independent, evidence-driven hardening audit of the real repository. Every
claim below was reproduced on the machine described in **§G**, not copied from
a prior report. Where something could not be checked, it is marked
`NOT_VERIFIED` / `BLOCKED_EXTERNAL` rather than guessed.

- **Audited base:** `main` @ `9773ba4cb213a2358c970c5013627b2d87bb2026` (`1.2.0rc2`)
- **Audit branch:** `audit/1.2-final-hardening` (9 commits on top of base)
- **Reference material:** `audit-inputs/APERIO_NEXT_SOURCE_AUDIT_2026-11-11.md`
  and `audit-inputs/APERIO_NEXT_README_PROPOSAL.md`, treated as hypotheses and
  re-verified against the code (not trusted as-is).

---

## A. Repository state and SHA

| Item | Value |
| --- | --- |
| Base SHA | `9773ba4cb213a2358c970c5013627b2d87bb2026` |
| Branch | `audit/1.2-final-hardening` |
| Base → HEAD | 32 files changed; commits are purely additive. Exact totals: `git diff --shortstat 9773ba4..HEAD`, `git rev-list --count 9773ba4..HEAD` |
| Audited source SHA (README pin target) | `3dc3207` — code + tests, all suites green |
| Code after the audited SHA | docs-only commits; no executable change since `3dc3207` |
| Pre-existing work | preserved; no reset, clean, force-push, or branch deletion |
| Untracked at start | `audit-inputs/` (owner-provided reference material) |

`feature/step01-universal-onboarding` and the v3 completion work are already
integrated into `main` (verified: `aperio next` exists and routes through the
onboarding facade; `git log` shows the integration commits).

## B. Prioritized findings

| ID | Severity | Root cause | Status |
| --- | --- | --- | --- |
| A05 | **CRITICAL** | Undecomposed project-scale goal became one self-certified task | **FIXED** |
| A13 | **CRITICAL** | Approval not bound to the plan; a tampered plan executed `/bin/echo pwned` | **FIXED** |
| A08 | **HIGH** | Raw tool stdout/stderr persisted into shareable session state | **FIXED** |
| A03 | **HIGH** | Output sliced *after* full buffering (memory exhaustion) | **FIXED** |
| A02 | **HIGH** | `os.killpg` called unconditionally on timeout (breaks on Windows) | **FIXED** |
| A01 | **HIGH** | `npx …` labelled as an "install"; no host registration | **FIXED (honesty)** |
| A04 | **MEDIUM** | Any one prerequisite (not all) marked a tool compatible | **FIXED** |
| A07 | **MEDIUM** | Whole directory materialised as a list; whole file read before slicing | **FIXED** |
| A06 | **HIGH (UX)** | No beginner-friendly first-run experience | **FIXED (`aperio start`)** |
| A09/A11 | **MEDIUM** | Docs cited stale 1.1-era counts / broken link | **FIXED** |
| A10 | **MEDIUM** | Passive probe readable as "verified" host integration | **FIXED (6 dimensions)** |
| A13r | **LOW/MED** | Varied error text resets the retry signature | **FIXED (task ceiling)** |
| A14 | **LOW** | Metadata said "Alpha" against an rc2 build | **FIXED** |
| *new* | **HIGH** | Lock TOCTOU: concurrent writers hit a spurious `PathError` (intermittent test failure) | **FIXED** |
| A12r | LOW | Reference-audit finding A12 addressed via A08 redaction | **FIXED** |
| R01 | **HIGH** | `aperio start` crashed on an existing project with no session; intent could be stored without consent | **FIXED** |
| R02 | **HIGH** | Install approval recorded without a fingerprint authorized *any* plan (A13 bypass) | **FIXED** |
| R03 | **HIGH** | Task-size gate applied only to tasks-less goals; one/many oversized tasks, direct plan-create, plan-approve and dispatch all bypassed it | **FIXED** |
| R04 | **HIGH** | Wizard consent failed *open*: with no approval callback it initialized a new project and created `.aperio/` in an existing one | **FIXED (fail closed)** |
| R05 | **HIGH** | Atomicity rule only knew whole-*project* keywords, so a whole section ("Implement the Hero Section") passed as one task | **FIXED** |
| R06 | **HIGH** | `run_captured` could hang far past its deadline: a descendant holding the pipes kept the readers blocked, then `stream.close()` waited on them (9.0s for a 0.2s timeout) | **FIXED (drain deadline)** |
| R07 | **MEDIUM** | Wizard `/back` overwrote the previous genuine answer with `Not sure yet` and never re-offered the question | **FIXED (`withdraw`)** |
| R08 | **MEDIUM** | Bounded work was refused just for naming a product ("Fix a typo in the website") | **FIXED (bounded operation wins)** |
| R09 | **MEDIUM** | Native plan tasks keep criteria in `acceptance_criteria`, which the atomicity check ignored — scope could hide there | **FIXED (both keys read)** |
| R10 | **MEDIUM** | Reading criteria as scope made a *verification* requirement look like extra implementation breadth ("Fix login button CSS" + "Unit tests pass" was refused) | **FIXED (scope vs criteria separated)** |

## C. Reproduction and root cause (key items)

**A05 — atomic decomposition.** `planbridge.plan_proposal_from_goal` synthesized
one task whenever `work.tasks` was absent. Reproduced: `"Build an entire
website"` produced a single `TASK-001`. Fix: `bounded_tasks.assess_goal_boundedness`
classifies scale from the goal text and its declared decomposition and refuses
project-scale work without a proportionate, verifiable breakdown. Verified
against the six mandated scenarios:

| Goal | Result |
| --- | --- |
| Build an entire website | BLOCK |
| Build a complete homepage | BLOCK |
| Build a Hero Section | BLOCK |
| Add CMS-backed Hero content | BLOCK |
| Fix a typo in the README | ALLOW (single task) |
| Update a docstring in cli.py | ALLOW (single task) |

**A13 — approval binding.** Reproduced directly: with the fingerprint gate
removed, an approved plan swapped to `["/bin/echo","pwned"]` executed. Fix:
`record_approval(..., fingerprint=…)` + `install_plan_fingerprint()`; a plan
whose command/source/permissions changed after approval is refused. Test:
`test_a13_tampered_plan_is_refused_after_approval`.

**A08 — secret leakage.** `add_tool_record` appended raw `output_tail` to
`session.json`. Fix: `safeproc.redact_output` strips provider keys, bearer/basic
auth, `key=value` secrets, URL credentials, and the user's home path; only the
redacted tail is persisted (`output_redacted: true`). Tested against realistic
credential shapes.

**A03/A02 — bounded capture and termination.** New `safeproc.run_captured`
drains both pipes on reader threads, enforces per-stream limits *while*
draining, and terminates via a platform-guarded `stop_process_tree`. Verified:
4 MB of stdout with a 2 KB limit keeps 2 KB and counts all bytes; a 1 s timeout
kills the child; on simulated `nt`, `killpg` is never called.

**A04 — prerequisites.** `build_install_plan` used `next(... in prerequisites)`,
so `node` present / `npm` missing read as compatible. Fix:
`_prerequisites_satisfied` requires **all** prerequisites, and `npx` was added
to the MCP candidates because the install command *is* `npx`.

**A07 — bounded scanning.** `list(os.scandir(...))` materialised whole
directories; `read_bytes()[:MAX]` read whole files. Fix: `os.scandir` used as a
context manager with the entry cap checked before recording; reads go through
`read_bytes_bounded` (closes the stat/read TOCTOU, preserves exact bytes for
hashing).

**New — lock TOCTOU.** `acquire_lock` did `path.exists()` then
`path.is_file()`; a concurrent release could unlink between them, so the check
raised a spurious `PathError: … a non-file occupant exists`. This caused the
intermittent `test_concurrent_captures_serialize_and_survive` failure that
appeared in the **pre-change baseline** (~1 in 10 full runs). Fix: one
`os.lstat`; a vanished path is treated as free. Verified with **480 concurrent
captures, zero failures**, and three consecutive clean full-suite runs.

## D. Files changed (resolved defects)

| File | Change |
| --- | --- |
| `src/aperio/safeproc.py` | **new** — bounded capture, redaction, cross-platform stop, bounded reads; R06 deadline-bounded drain |
| `src/aperio/bounded_tasks.py` | **new** — A05 scale/decomposition assessment; R03/R05 task-, plan- and section-level atomicity |
| `src/aperio/wizard.py` | **new** — A06 opt-in interactive wizard |
| `src/aperio/onboarding_tools.py` | A01/A03/A04/A08/A13 — safe run, all-prereqs, redaction, fingerprint |
| `src/aperio/onboarding_session.py` | A13 — fingerprint-bound `record_approval`, `approved_plan_fingerprint`; R07 `withdraw_answer` |
| `src/aperio/onboarding_discovery.py` | A07 — incremental scandir |
| `src/aperio/onboarding_reconstruct.py` | A07 — bounded byte read |
| `src/aperio/onboarding.py` | R07 — `withdraw()` clears state derived from a withdrawn answer |
| `src/aperio/planbridge.py` | A05 — gate at the bridge |
| `src/aperio/planmodel.py` | R03 — task-size check in `validate_plan_input` (create/revise) |
| `src/aperio/planstore.py` | R03 — task-size check in `approve_plan` |
| `src/aperio/runtime.py` | R03 — task-size check in dispatch eligibility |
| `src/aperio/wizard.py` | R01/R04 — init consent; `_consent_granted` fails closed when the callback is missing or raises; R07 `/back` withdraws and re-prompts |
| `src/aperio/cli.py` | R02 — record the reviewed plan fingerprint, not a bare scope |
| `src/aperio/recovery.py` | A13r — task-level ceiling |
| `src/aperio/hostcompat.py` | A10 — six verification dimensions |
| `src/aperio/cli.py` | A06 — `aperio start` command |
| `src/aperio/locks.py` | lock TOCTOU fix |
| `pyproject.toml` | A14 — metadata |
| `tests/conftest.py` | cross-version collection fix |
| `tests/test_audit_hardening.py` | **new** — 52 regression tests |
| `tests/test_onboarding_tools.py` | one test now records a fingerprint-bound approval |
| `tests/test_planbridge_priority.py` | updated to the new decomposition policy |
| `README.md` | full rewrite |
| `docs/{install,quickstart,onboarding,cli-reference,limitations,release-notes,brain-dump}.md` | synced |

## E–F. Test evidence

| | Count |
| --- | --- |
| Baseline suite (`9773ba4`, per prior handoff, reproduced locally) | 1,601 |
| Hardening tests before the final remediation | 1,637 total |
| New regression tests, first remediation | 17 (wizard ×3, install approval ×4, task-size ×10) |
| New regression tests, second remediation | 12 (wizard fail-closed ×4, section atomicity ×8) |
| New regression tests, Codex review rounds | 17 (`/back` ×3, drain deadline ×1, product mentions ×8, criteria keys ×2, criteria breadth ×3) |
| Tests updated for the new policy | 3 (`test_planbridge_priority`, `test_onboarding_tools`, hero A05 fixtures) |
| **Final full suite** | **1,683 passed, 0 skipped** |

Per-interpreter full-suite results (macOS arm64):

| Python | Result |
| --- | --- |
| 3.11 | 1,683 passed |
| 3.12 | 1,683 passed |
| 3.13 | 1,683 passed |
| 3.14 | 1,683 passed |

### Regression reproduction for the final remediation (R01–R03)

Each new test was written before its fix and failed first:

| Path | Reproduced failure | Now |
| --- | --- | --- |
| Wizard, existing project, no session | `StateError: cannot create lock file … onboarding/session.lock` | consent asked first; no mutation on decline |
| Install approval, legacy unbound | unbound approval authorized an arbitrary plan | refused: re-approve the exact plan |
| Install approval, CLI path | CLI stored scope-only approval | CLI stores the reviewed fingerprint |
| Goal, one oversized task | bridge accepted it | blocked at goal assessment |
| Goal, multiple oversized tasks | bridge accepted them | blocked, each named |
| `plan-create` direct | oversized task accepted | `ValidationError` |
| `plan-approve` hand-written | approved into executable state | `StateError`, nothing modified |
| Dispatch eligibility | over-sized plan dispatched | `StateError: execution refused` |
| Valid small task (Hero/frontend/backend/CMS) | — | still allowed (backward compatible) |

## G. Environment and platforms

| Item | Value |
| --- | --- |
| OS | macOS 27.2 (Darwin 27.2.0, arm64) |
| Python | 3.14.7 (repo) / 3.11.16, 3.12.14, 3.13.15 (uv) |
| Git | 2.54.0 |
| uv | 0.12.21 |
| pipx | 1.18.16 (installed via uv for testing) |
| **Windows** | **NOT tested — UNVERIFIED** |
| **Linux** | **NOT tested — UNVERIFIED** |

## H. Installation comparison

| Method | Isolation | Verified | Notes |
| --- | --- | --- | --- |
| `uv tool install` (wheel) | yes | **yes** | upgrade/uninstall clean; bin on PATH |
| `uv tool install` (git @ immutable commit) | yes | **yes** | re-verified against the audited commit `3dc3207` (24-char SHA resolved, `aperio 1.2.0rc2`) |
| `pipx install` (wheel) | yes | **yes** | viable fallback |
| venv + wheel | yes | yes | more manual |
| PyPI | — | **N/A** | no package published |
| GitHub Release asset | — | **N/A** | no release published |
| custom `curl \| sh` installer | — | **not built** | deliberately avoided |

## I. Chosen installation method

**`uv tool install` from a pinned git ref**, with **`pipx`** as the documented
fallback. Reasons: true isolation, one predictable executable, works on
macOS/Linux/Windows, clean upgrade/uninstall, no global dependency change, and
it is reproducible when pinned to a commit or tag. No custom installer was
built. Project `.aperio/` data survives upgrade and uninstall (verified).

## J. README replacement

- 790 → 202 lines (150 non-blank), beginner-first, ordered per the directive.
- All 16 required questions answered; deep material moved to `docs/`.
- **Every** CLI command in the README was executed against a clean isolated
  install (wheel built from this branch, installed outside the repo).
- 16 relative links, all resolve. No badges, no screenshots, no placeholders.
- The documented install command is pinned to the audited code commit
  `3dc3207` and was executed successfully from a clean environment with both
  `uv tool install` and `pipx install`
  (`Installed 1 executable: aperio` → `aperio 1.2.0rc2`), after the second
  remediation and the Codex review round. `aperio next`, `aperio --help`, and
  `aperio start` (non-TTY refusal, directory untouched) were re-run against
  that install.
  **Owner action after merge:** re-pin the ref to the merge commit or a
  release tag so installs always fetch the audited code.

## K. Documentation examples tested

`aperio --version`, `--help`, `next .`, `start .` (real PTY, and non-TTY refusal),
`onboarding-confirm-init`,
`onboarding-answer`, `onboarding-readiness`, `host-doctor`, `status`,
`benchmark-run`, `uv tool upgrade`, `uv tool uninstall` — all exercised on the
installed artifact. `aperio next` confirmed to leave an empty directory empty.

R01 and R04 were additionally verified end to end in a **real PTY** against
the pinned install (`4575497`, `66ed17f`, `f9b9d3f`, `3dc3207`), on an existing
project with no `.aperio`:

- declining the init prompt printed by the wizard left the directory unchanged
  (no `.aperio`, no crash — previously it raised a lock-path `StateError`);
- the wizard asks, verbatim, "Create Aperio's .aperio/ scaffolding in this
  existing project? [y/N]" *before* any mutation;
- answering `y` created `.aperio/` and recorded `intent: Continue existing
  work` in the session;
- the same command without a TTY exits with a clear message and does nothing;
- declining the same consent prompt on the re-verified install printed
  "nothing was changed" and left the directory byte-identical.

## L. Compatibility matrix (passive vs live)

| Host | Level | Live authenticated call? |
| --- | --- | --- |
| Terminal CLI | cli_verified | Yes (this is Aperio's own runtime path) |
| Codex CLI | adapter tested vs **simulated** exec | **No** |
| Claude Code | adapter tested vs **simulated** exec | **No** |
| OpenCode/Cline/Cursor/Kilo/ZCode/Goose/Antigravity/Pi | cooperative fallback | **No** |

Each `host-doctor` row now carries `verification` with six booleans; only
`executable_present` / `supported_flags_detected` can be true from a passive
probe. `cli_launch_tested`, `authenticated_model_call_tested`,
`functional_handoff_tested`, `native_command_registered_and_tested` are `false`
until a real run is recorded. No mock executable is described as a live test.

### Third Codex review round (R10) — what changed and why

| ID | Defect (reproduced first) | Fix | Evidence |
| --- | --- | --- | --- |
| R10 | Criteria were folded into scope text, so a *verification* requirement ("Unit tests pass") counted as a second implementation area and a bounded CSS fix was refused | Implementation scope drives breadth; criteria are read only to catch hidden scope, and a slice defends a criterion only for the same feature | 3 tests (bounded + test criterion allowed, hidden project/section still blocked, slice with section criterion allowed) |

### Codex re-review round (R08–R09) — what changed and why

| ID | Defect (reproduced first) | Fix | Evidence |
| --- | --- | --- | --- |
| R08 | Scale inference required *no* large-scope term, so "Fix a typo in the website" was project-scale and the bridge refused it | A bounded operation wins over the product it mentions; breadth markers, multiple distinct large terms, or a long objective still mean large | 8 tests (4 allowed, 4 still blocked); CLI on the pinned install bridges the typo goal to a draft plan |
| R09 | `_task_text` read only `completion_criteria` (goal schema); plan tasks use `acceptance_criteria`, so scope hidden there passed create/approve/dispatch | Both criterion keys are read | 2 tests, including a hand-forced approval stamp proving dispatch refuses on its own |

### Codex review round (R06–R07) on PR #1 — what changed and why

| ID | Defect (reproduced first) | Fix | Evidence |
| --- | --- | --- | --- |
| R06 | `run_captured` hung for 9.0s against a 0.2s timeout when a grandchild inherited the pipes; readers stayed blocked and `stream.close()` then waited on the reader lock | Drain bounded by `drain_grace_seconds` (default 2s); on a stall the remaining process group is stopped; the stream is closed off-thread with a 1s deadline; `drain_stalled` reports honestly | reproduction now returns in ~1.1s with output intact and no leaked descendants |
| R07 | `/back` wrote `Not sure yet` over a genuine answer and advanced past the question | `withdraw_answer()` removes the answer (kept in `answer_history` + audit trail) and `withdraw()` clears derived profile/category; the wizard re-prompts | 3 tests: no overwrite, question re-offered, withdraw is scoped |

### Second remediation (R04–R05) — what changed and why

| ID | Defect (reproduced first) | Fix | Evidence |
| --- | --- | --- | --- |
| R04 | Both wizard mutation paths skipped consent when `request_approval` was `None`, so an unattended caller initialized a new project or created `.aperio/` in an existing one; the docstring claimed the opposite | `_consent_granted()` fails closed: a missing callback, a raising callback, or a falsy return is a refusal, and nothing is written | 4 tests (new project, existing project, no session, callback-approve still works); real PTY decline on the pinned install leaves the directory unchanged |
| R05 | Atomicity only recognised whole-project keywords; "Implement the Hero Section" was accepted as one task at goal, create, approve and dispatch | Task size now also flags a breadth marker plus a feature unit, a task spanning several implementation areas, and a bare `<verb> the <feature unit>` without a narrowing slice in the declared scope | 8 tests across the four paths; CLI proof: whole-section plan rejected at `plan-create`, bounded hero slice created → approved → dispatched |

Preserved on purpose (no unnecessary micro-decomposition): "Build the Hero
Section markup", "Style the Hero responsive layout", "Add the hero heading
copy", "Fix a typo in the README", "Update a docstring", "Build the frontend",
"Implement the pages", "Configure the CMS", "Add an export button to the
billing dashboard". Layers inside one area do not count as breadth, and slice
qualifiers are read from the declared scope (title/objective/action), so a
stale deliverable cannot launder a whole-section claim.

### Final remediation (R01–R03) — what changed and why

| ID | Defect (reproduced first) | Fix | Evidence |
| --- | --- | --- | --- |
| R01 | `aperio start` on an existing project with no `.aperio` session died with a lock-path `StateError`, and stored intent without asking | consent via `confirm_initialization()` (the sanctioned first mutation) before `record_intent`; decline leaves the workspace untouched | 3 tests; non-TTY run leaves the dir unchanged |
| R02 | `record_approval()` without a fingerprint authorized any install plan — a real bypass of A13 | `execute_install` requires a bound fingerprint and refuses a legacy unbound approval with a re-approve message; CLI records the reviewed fingerprint | 4 tests: legacy unbound, bound match, tampered, CLI binding |
| R03 | Task-size gate ran only when a goal had no tasks; one or many oversized tasks, `plan-create`, `plan-approve`, and dispatch were all unguarded | `assess_task_boundeness` + `assess_plan_boundeness` wired into goal assessment, `validate_plan_input`, `approve_plan`, dispatch eligibility | 10 tests over all five paths plus the backward-compatibility cases |

## M. Security limitations and remaining risks

- Redaction is pattern-based data-minimisation, not a formal secrecy proof.
- MCP "install" runs the documented `npx …` command; Aperio does **not** edit
  host MCP config, so no host is claimed wired to a server (`launch-only`).
- Provider execution is fail-closed but only tested against fakes.
- Context archives are unencrypted by design.
- Same-machine processes can read the loopback UI token (documented).

## N. External blockers

- **GitHub Actions:** owner reports an account billing lock; jobs do not start.
  Classified `BLOCKED_EXTERNAL`. Workflows were not modified or disabled.
- **No PyPI / GitHub Release** exists; installation is from git or a local wheel.
- **Windows/Linux** unavailable on the audit machine → `NOT_VERIFIED`.

## O. Outstanding internal defects

None known that block the CLI release candidate. Residual risks are the
documented limitations in M. The rc2 build is **not** claimed production-ready.

## P. Release gates

| Gate | Status | Evidence |
| --- | --- | --- |
| G01 Repo state preserved | PASS | §A |
| G02 High/critical defects fixed | PASS | §B, §D |
| G03 Unit tests pass | PASS | 1,683 (4 interpreters) |
| G04 Integration tests pass | PASS | e2e/onboarding suites green |
| G05 Real onboarding journey works | PASS | §K, PTY wizard |
| G06 Atomic execution policy enforceable | PASS | §C A05 |
| G07 Bounded recovery works | PASS | A13r test |
| G08 Tool-install safety gates work | PASS | A04/A13 tests |
| G09 Verified MCP integration | **FAIL (honest)** | launch-only by design; not claimed |
| G10 Secret handling passes adversarial tests | PASS | A08 tests |
| G11 Agent compatibility truthful | PASS | §L |
| G12 Portable state/handoff tests pass | PASS | exchange/portable suites |
| G13 Clean wheel install | PASS | §H |
| G14 Recommended tool install | PASS | uv git + wheel |
| G15 README commands reproducible | PASS | §J/§K |
| G16 OS testing honestly reported | PASS | Windows/Linux UNVERIFIED |
| G17 Schemas/APIs compatible | PASS | no breaking schema change; additive fields |
| G18 Docs match source | PASS | §D/docs sync |
| G19 Packaging contains resources | PASS | 14 benchmark JSON + 11 skill packs in wheel |
| G20 No known critical internal defects | PASS | §O |

G09 is deliberately **FAIL (honest)**: Aperio does not register MCP servers
with hosts, and the code and docs say so. It is not marketed as done.

## Q. Branch and commits

Branch `audit/1.2-final-hardening`: every commit is additive on `9773ba4`
(no reset, amend of shared history, or force-push). Highlights, newest first:

```
docs-only  README re-pin + audit/release-notes/handoff totals (no code change)
4575497 fix: fail closed on wizard consent; treat a whole section as non-atomic
f04232d fix: bind install approval to the plan, gate task size on every path
8279e1d fix(audit): bound subprocess capture, guard POSIX kill, require all prereqs, bind install approval to plan fingerprint
d69d24a fix(audit): scan directories incrementally and read documents with a byte-bounded handle
eebdc4a feat(audit): refuse undecomposed project-scale goals at the plan bridge
410b98e feat(audit): opt-in interactive onboarding wizard ('aperio start')
f4d178f fix(audit): add a task-level retry ceiling above per-signature budgets
ba52f68 fix(audit): record host verification as six separate facts
d935718 fix(test): make 'tests.' imports resolve under any pytest invocation; honest metadata
71c7bf3 fix(locks): classify the lock path with a single lstat to close a TOCTOU
dc99263 docs: rewrite README for beginners; modernize install and sync docs
```

## R. Reproduction commands for the owner

```sh
git clone https://github.com/SHAHID8142/aperio-next.git
cd aperio-next
git checkout audit/1.2-final-hardening

# Full suite (no local venv required):
uv run --python 3.14 --with pytest pytest

# Verify a clean isolated install + the README flow:
uv build --wheel
uv tool install ./dist/aperio_next-1.2.0rc2-py3-none-any.whl
aperio --version
mkdir /tmp/demo && cd /tmp/demo && aperio next . && aperio start .
```

## Final verdict

**RELEASE CANDIDATE — SPECIFIC VALIDATION PENDING.**

All reproduced internal high/critical defects are fixed and covered by
regression tests; the full suite passes on four Python versions. The release
is **not** called Release Ready because: Windows and Linux were not executed,
no live authenticated agent integration was run, hosted CI is blocked, and no
public PyPI/Release artifact exists. Those are stated as UNVERIFIED /
BLOCKED_EXTERNAL, not hidden.

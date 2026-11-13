# Aperio Next Release-Candidate Tasks

## Execution status

Approved release plan: revision 2, recorded through the engine after explicit
owner approval. Tasks 001–011 have recorded completion evidence; task 012
has final independent review approval and is performing the Git handoff.
The engine-managed plan and checkpoints are authoritative. Detailed checklists
below retain the approved acceptance contract; unavailable host/platform
checks remain explicit limitations in docs/release-candidate-evidence.md.

## TASK-001: Verify baseline and current implementation

**Description:** Establish the exact branch, diff, test baseline, package structure, and current behavior of the fixes already present after `061dbf9`.

**Acceptance criteria:**

- [ ] Local/remote branch state and pre-existing untracked content are recorded and preserved.
- [ ] Python and Node baseline suites finish with exact counts.
- [ ] Current implementations of every named P0 target are traced to code and tests.

**Verification:**

- [ ] `.venv/bin/python -m pytest -q`
- [ ] `node --test packaging/npm/test/launcher.test.js`
- [ ] `git diff --check main...HEAD`

**Dependencies:** None

**Estimated scope:** Small (evidence only)

## TASK-002: Reproduce P0 failure scenarios

**Description:** Run focused tests and adversarial fixtures for wrong-engine resolution, broken identity, paths with spaces, partial cleanup, project-pointer failure, installed resources, and interrupted lifecycle operations.

**Acceptance criteria:**

- [ ] Each listed P0 scenario has a deterministic result.
- [ ] Any failure is reproduced before source changes.
- [ ] Non-reproduced claims are tied to concrete passing tests, not code inspection alone.

**Verification:**

- [ ] Focused `tests/test_integrations.py`, `tests/test_packaging.py`, and Node launcher tests pass.
- [ ] Disposable fixture outputs are saved in the release evidence/checkpoint.

**Dependencies:** TASK-001

**Estimated scope:** Medium (tests and evidence)

## TASK-003: Correct engine-resolution defects

**Description:** If reproduction exposes a gap, add the failing test and minimally correct native skill/command rendering and identity validation.

**Acceptance criteria:**

- [ ] No generated native asset silently invokes an unverified PATH executable.
- [ ] Correct isolated, spaced, missing, corrupt, wrong-product, upgrade, fresh, and project-scoped paths behave safely.

**Verification:**

- [ ] Focused Python integration tests pass from source and installed wheel.

**Dependencies:** TASK-002

**Estimated scope:** Medium (up to 5 files)

## TASK-004: Correct lifecycle and error-propagation defects

**Description:** If reproduced, fix aggregate install/repair/update/uninstall reporting while preserving foreign content and project state.

**Acceptance criteria:**

- [ ] Required partial failures return nonzero and retain recoverable state.
- [ ] Uninstall never strands known registrations or removes an unverified/foreign engine.
- [ ] Repeated operations are safe and unrelated user configuration survives.
- [ ] Inject failed engine installation/upgrade, failed skill/command regeneration, partial host-registration failure, and interrupted update; each must preserve or restore the last known-good engine and compatible integration assets, verify recovered identity and asset compatibility, and preserve previous project data and unrelated user configuration. Claim rollback only from actual injected-failure evidence.

**Verification:**

- [ ] Focused Python and Node failure-injection tests pass.

**Dependencies:** TASK-002

**Estimated scope:** Medium (up to 5 files)

## TASK-005: Complete durable project activation

**Description:** If reproduced, connect approved onboarding to idempotent project instructions without mutating unrelated projects or overwriting host/user instructions.

**Acceptance criteria:**

- [ ] Authorized initialization creates the required pointer exactly once.
- [ ] Existing `AGENTS.md`, `CLAUDE.md`, host rules, and `.aperio/` state remain intact.
- [ ] Pointer write/removal failures propagate accurately.

**Verification:**

- [ ] Focused onboarding and integration tests pass.
- [ ] New/existing disposable project checks pass.

**Dependencies:** TASK-002

**Estimated scope:** Medium (up to 5 files)

## TASK-006: Harden packaging tests

**Description:** Verify clean-built wheel/tarball contents and make packaging/failure tests deterministic wherever the current suite has a gap.

**Acceptance criteria:**

- [ ] Required build failures fail tests rather than becoming skips.
- [ ] Installed resources include every required Markdown/TOML native asset.
- [ ] Node tests prove injected failure preconditions occurred.

**Verification:**

- [ ] `tests/test_packaging.py` passes against a clean build.
- [ ] npm pack contents and Node suite pass.

**Dependencies:** TASK-002, TASK-003, TASK-004, TASK-005

**Estimated scope:** Medium (up to 5 files)

## TASK-007: Verify clean-room installation lifecycle

**Description:** Build real artifacts and exercise install, doctor, activation readiness, repair/update, and uninstall in isolated roots.

**Acceptance criteria:**

- [ ] Engine identity and absolute paths are correct after install/update.
- [ ] Project state survives uninstall.
- [ ] Broken/partial lifecycle states produce honest, retryable failures.
- [ ] Inject failed engine installation/upgrade, failed skill/command regeneration, partial host-registration failure, and interrupted update; each must preserve or restore the last known-good engine and compatible integration assets, verify recovered identity and asset compatibility, and preserve previous project data and unrelated user configuration. Claim rollback only from actual injected-failure evidence.

**Verification:**

- [ ] Captured clean-room commands and outputs support every claim.

**Dependencies:** TASK-006

**Estimated scope:** Medium (evidence fixtures)

## TASK-008: Verify governed project workflow and recovery

**Description:** Use disposable new and existing projects for consented onboarding, a small real coding task, checkpoint, verification, independent review, handoff, and fresh-session recovery.

**Acceptance criteria:**

- [ ] Existing files/instructions are preserved and pointer blocks are idempotent.
- [ ] The coding task follows plan approval, execution authorization, evidence, and review gates.
- [ ] A genuinely new Codex CLI session recovers pending/completed state; unavailable access is recorded as an unmet acceptance requirement.
- [ ] A mandatory genuinely fresh Codex CLI session invokes native $aperio-next and proves discovery, activation, correct engine identity, actual aperio next execution, consented initialization, and a governed coding task in a disposable project. Another fresh session receives Continue the previous task and uses the engine to recover completed and pending work. Reading SKILL.md or launching a Python subprocess is insufficient.

**Verification:**

- [ ] Real fixture tests and engine state reports demonstrate the sequence.

**Dependencies:** TASK-007

**Estimated scope:** Medium (disposable fixtures)

## TASK-009: Verify native host compatibility

**Description:** Probe Codex, Claude Code, OpenCode, ZCode, Gemini CLI, and Cursor, then run bounded live activation only on available authenticated hosts.

**Acceptance criteria:**

- [ ] Registration/discovery/activation/engine/init/recovery statuses are separately recorded.
- [ ] Missing or inaccessible hosts remain unverified.
- [ ] Cross-agent continuation is tested only when two verified agents are safely available.
- [ ] Codex is a primary mandatory acceptance target. Report Codex CLI and VS Code Codex extension results separately with exact blockers for unavailable environments; missing Codex evidence remains unmet.

**Verification:**

- [ ] Host-doctor, integration status, and bounded fresh-session outputs support the matrix.

**Dependencies:** TASK-007

**Estimated scope:** Medium (host evidence)

## TASK-010: Security and independent diff review

**Description:** Review the entire feature diff for destructive filesystem behavior, identity spoofing, quoting, traversal/symlink issues, timeouts, prompt injection, gate bypasses, conflicts, and unsupported claims.

**Acceptance criteria:**

- [ ] Every reproducible critical/high finding is fixed with a regression test.
- [ ] No known critical/high issue remains in the declared verified scope.
- [ ] Final diff has no accidental or unrelated changes.
- [ ] A separate reviewer agent or independent fresh review session receives the complete final diff, acceptance criteria, and test evidence. The implementation author is not the sole reviewer. Require an explicit findings report with concrete security, reliability, and integration defects, file locations, reproduction steps, and assessment of governance/authorization boundaries, packaging, lifecycle, and persistent project instructions. Reproduce and fix all valid critical/high findings with regressions, repeat affected verification, and obtain independent follow-up review. If independence is unavailable, record the exact limitation and leave independent acceptance unmet; never self-certify independence.

**Verification:**

- [ ] Security-focused suites and full regression suite pass.
- [ ] `git diff --check` passes.

**Dependencies:** TASK-007, TASK-008, TASK-009

**Estimated scope:** Medium (review plus focused fixes)

## TASK-011: Update release evidence and documentation

**Description:** Reconcile README, integration guide, handoff, and evidence documents with newly executed results and precise limitations.

**Acceptance criteria:**

- [ ] Counts, versions, activation claims, and commands match current evidence.
- [ ] Implemented/unit-tested/packaged/registered/activated/E2E/recovered levels remain distinct.
- [ ] Public npm installation is not claimed before publication.

**Verification:**

- [ ] Documentation links/commands are checked against built artifacts and test results.

**Dependencies:** TASK-010

**Estimated scope:** Medium (up to 5 files)

## TASK-012: Finalize the release-candidate handoff

**Description:** Run final verification, checkpoint/handoff the durable workflow, commit coherent changes, and push the existing feature branch if access permits.

**Acceptance criteria:**

- [ ] Full suites and package lifecycle pass with exact counts.
- [ ] Commit and push state are accurately reported; no merge or publication occurs.
- [ ] Final verdict and quick-start are limited to the verified scope.
- [ ] Independent review covers the complete final diff including final documentation; recheck subsequent changes before handoff. Report injected-failure rollback evidence, separate Codex CLI/VS Code results, and independent findings and resolutions. Missing mandatory evidence prevents a READY FOR DAILY USE verdict.

**Verification:**

- [ ] `git status`, final SHA, remote branch, and Aperio work status/checkpoint are captured.

**Dependencies:** TASK-011

**Estimated scope:** Small (release operations and report)

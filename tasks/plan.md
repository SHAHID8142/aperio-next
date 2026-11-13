# Implementation Plan: Aperio Next Release-Candidate Completion

## Overview

Independently validate the `feat/universal-agent-integration` branch as a daily-use release candidate, reproduce and fix any remaining release-blocking defects with regression tests, exercise the real installer and native-agent experience in isolated fixtures, and publish an evidence-backed compatibility verdict without merging or publishing packages.

## Architecture Decisions

Revision 2 retains all 12 tasks and the existing architecture. The engine preserves revision 1 in draft history. Implementation requires human approval of the amended draft.

- Preserve the Python orchestration engine as the single source of truth; the npm package remains a thin installer and lifecycle wrapper.
- Treat native host files as cooperative adapters that must embed a validated absolute engine path; deterministic governance remains inside the engine.
- Use the existing ownership markers and atomic-write rules for every integration mutation; never adopt, overwrite, or delete foreign content.
- Test in disposable roots and projects. The repository's existing `.aperio/` and `audit-inputs/` content is preserved.
- Scope every release claim to newly executed evidence. Missing hosts, operating systems, credentials, CI access, and registry publication remain explicitly unverified.

## Dependency Graph

```text
Baseline and source audit
    -> P0 regression reproduction
        -> focused defect fixes (only when reproduced)
            -> package and lifecycle clean-room tests
                -> native activation and persistent-workflow tests
                    -> security/final-diff review
                        -> evidence, commit, push, final verdict
```

## Task List

### Phase 1: Establish Evidence

- [x] TASK-001: Verify repository, branch, baseline tests, package contents, and existing P0 fixes.
- [x] TASK-002: Reproduce the P0 engine-resolution, project-persistence, aggregate-error, uninstall/repair, and packaging failure cases.

### Checkpoint: Baseline and Reproduction

- [x] Focused Python and Node suites pass.
- [x] Every alleged P0 is classified as reproduced, fixed already, or blocked by unavailable infrastructure, with evidence.
- [x] A durable Aperio checkpoint records the classification.

### Phase 2: Close Confirmed Release Blockers

- [x] TASK-003: Fix confirmed engine-resolution or native-template defects test-first.
- [x] TASK-004: Fix confirmed install/repair/update/uninstall safety or error-propagation defects test-first.
- [x] TASK-005: Fix confirmed onboarding-to-project-persistence defects test-first.
- [x] TASK-006: Harden installed-artifact packaging and deterministic JavaScript/Python tests where gaps remain.

### Checkpoint: Implementation

- [x] Each code change has a failing regression test captured before the fix.
- [x] Focused suites and package builds pass.
- [x] No ownership, consent, approval, review, or evidence gate is weakened.

### Phase 3: Real-User Acceptance

- [x] TASK-007: Build and inspect the wheel and npm tarball, then run the isolated install -> doctor -> activate -> repair/update -> uninstall lifecycle.
- [x] TASK-008: Exercise safe new/existing-project onboarding, durable instructions, a governed coding task, verification, review, checkpoint, and fresh-session recovery.
- [x] TASK-009: Probe installed coding agents and run bounded native activation/fresh-session/cross-agent tests where credentials and host interfaces permit.
- [x] TASK-010: Perform the security audit and independent final-diff review, addressing any reproducible critical/high finding.

### Checkpoint: Acceptance

- [x] Full Python and Node suites pass.
- [x] Clean-room package lifecycle succeeds without touching real project state.
- [x] At least one real supported agent is natively activated and a genuine fresh session recovers the fixture, or the verdict remains below READY FOR DAILY USE.
- [x] Governance and independent-review gates are demonstrated with durable evidence.

### Phase 4: Release-Candidate Handoff

- [x] TASK-011: Update release documentation and evidence with only newly observed results and exact limitations.
- [x] TASK-012: Review the final diff, commit coherent verified changes, push the existing feature branch, and report GitHub/publication state without merging or publishing.

### Checkpoint: Complete

- [x] Required final report contains verdict, behavior, resolved issues, exact test results, six-host matrix, limitations, Git state, and a genuinely working quick-start.
- [x] Working tree contains no accidental generated artifacts or changes to pre-existing unrelated files.
- [x] Aperio task states, checkpoint, decisions, and handoff match the evidence.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Tests pass only from source | High | Build wheel/tarball and test installed resources in isolated roots. |
| Legacy `aperio` wins PATH lookup | Critical | Assert absolute validated engine paths in skills and commands, including paths with spaces. |
| Partial cleanup is called success | High | Inject deterministic failures and assert aggregate nonzero results and recoverable state. |
| Native file tests are mistaken for activation | High | Separate registered, discovered, activated, engine-invoked, initialized, and fresh-session evidence. |
| Provider access consumes quota or is unavailable | Medium | Use bounded probes/sessions; do not install paid tools or claim unavailable hosts. |
| Local host configuration is damaged | High | Use disposable HOME/install roots wherever possible and preserve foreign files. |
| Approval or review is fabricated | Critical | Require genuine operator plan approval and engine-recorded independent review outcomes. |
| CI or public registry is unavailable | Medium | Record exact blocker and provide equivalent local evidence without claiming CI/public installation. |

## Mandatory Revision 2 Acceptance

- **TASK-004 and TASK-007:** Inject failed engine installation/upgrade, failed skill/command regeneration, partial host-registration failure, and interrupted update; each must preserve or restore the last known-good engine and compatible integration assets, verify recovered identity and asset compatibility, and preserve previous project data and unrelated user configuration. Claim rollback only from actual injected-failure evidence.
- **TASK-008:** A mandatory genuinely fresh Codex CLI session invokes native $aperio-next and proves discovery, activation, correct engine identity, actual aperio next execution, consented initialization, and a governed coding task in a disposable project. Another fresh session receives Continue the previous task and uses the engine to recover completed and pending work. Reading SKILL.md or launching a Python subprocess is insufficient.
- **TASK-009:** Codex is a primary mandatory acceptance target. Report Codex CLI and VS Code Codex extension results separately with exact blockers for unavailable environments; missing Codex evidence remains unmet.
- **TASK-010:** A separate reviewer agent or independent fresh review session receives the complete final diff, acceptance criteria, and test evidence. The implementation author is not the sole reviewer. Require an explicit findings report with concrete security, reliability, and integration defects, file locations, reproduction steps, and assessment of governance/authorization boundaries, packaging, lifecycle, and persistent project instructions. Reproduce and fix all valid critical/high findings with regressions, repeat affected verification, and obtain independent follow-up review. If independence is unavailable, record the exact limitation and leave independent acceptance unmet; never self-certify independence.
- **TASK-012:** Independent review covers the complete final diff including final documentation; recheck subsequent changes before handoff. Report injected-failure rollback evidence, separate Codex CLI/VS Code results, and independent findings and resolutions. Missing mandatory evidence prevents a READY FOR DAILY USE verdict.

Unavailable infrastructure or reviewer independence is a limitation, not a passing acceptance result.

## Open Questions

- None for planning. Live-host and publication claims are determined by observed access and evidence, not assumptions.

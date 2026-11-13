# Revision-2 release-candidate evidence

Recorded 2026-11-12 on macOS arm64. Implementation started from feature
branch `feat/universal-agent-integration` at `2a3a73ff5a838a18caac47417cc55278524b0596`.
Release plan revision 2 and the separate disposable greeting plan revision 1
were explicitly approved by the owner and recorded through `plan-approve`
with exact revision arguments. No owner approval was fabricated.
Final handoff continued on 2026-11-13 (Asia/Dhaka).

## Verdict and verified scope

**USABLE WITH DOCUMENTED LIMITATIONS.** The local artifact supports the
complete native Codex CLI journey on this macOS machine: install, activate,
consented initialization, automatic persistent instructions, approved coding,
verification, separate-agent review, and recovery in a fresh agent session.
OpenCode additionally demonstrated native activation and read-only recovery
of the Codex fixture. This is not a universal-host or public-registry claim.

## Newly executed verification

| Check | Command / observation | Result |
| --- | --- | --- |
| Full Python | `.venv/bin/python -m pytest -o addopts='' -q` | **1769 passed in 112.98s**, no skips |
| Full Node | `node --test packaging/npm/test/*.test.js` | **34 passed in 151.66s**, no failures/skips |
| Wheel resources | `tests/test_packaging.py`, also included in full Python run | 5 tests; real built wheel installed outside checkout; Gemini TOML rendered from installed resources |
| Build | `./scripts/build-npm-package.sh` | Python wheel and npm tarball produced; npm has 4 expected files: launcher, wheel, package metadata, README |
| Packed lifecycle | `node scripts/verify-release.cjs` | Explicit npx local-package install, doctor, identity, consented fixture init, Codex project registration, update, uninstall; state bytes preserved |
| Native Codex | `node scripts/verify-codex.cjs` | Fresh CLI native `$aperio-next`, real isolated identity and `next`, `empty_new`, stopped before consent |
| Native initialization | `node scripts/codex-fixture-session.cjs initialization <fixture-prompt>` | Fresh session initialized with labelled fixture consent, wrote AGENTS pointer, completed intake, left draft plan unapproved |
| Native coding | `node scripts/codex-fixture-session.cjs implementation <fixture-prompt>` | Following owner's exact fixture-plan approval: manual engine RUN-0001, failing unittest, implementation, passing unittest/direct assertion, review submission |
| Independent fixture review | Separate reviewer agent, actual engine `review-decide` | Reviewer inspected both files, independently ran unittest/direct assertion, approved REV-0001; did not complete implementer's task |
| Fresh recovery | Another CLI invocation, prompt only `Continue the previous task.` | New thread recovered context, review and active run; completed task/run with exact approved evidence, wrote handoff/checkpoint 5, integrity OK |
| Final artifact recovery recheck | Refreshed fixture engine; another fresh read-only CLI process with the same ordinary prompt | Identity, next, work-status, memory and readiness exited 0, correctly showing completed work and no pending review; CLI then exited 1 on provider usage limit before final reply. This attempt is partial, not a second completed agent turn. |
| Cross-agent read-only | `node scripts/verify-other-hosts.cjs` | OpenCode session activated project skill and executed identity, next, memory, work-status, recover against Codex fixture |
| Registry | `npm view aperio-next version --registry=https://registry.npmjs.org` | E404; no public install claim |

Raw machine-local transcripts and failure outputs are under ignored `.qa/`.
They are not committed because they contain local paths and provider context.
The commands, results and separate reviewer observations are summarized here
and in [independent-release-review.md](independent-release-review.md).
The `.qa/` fixtures are disposable; no production project was modified.

## Root causes and regression proof

| Issue | Root cause and correction | Regression evidence |
| --- | --- | --- |
| Destructive failed update | pip reinstalled into the working venv before registration succeeded. Snapshot owned engine/bin/manifest and managed or absent asset paths before mutation; restore on failure. | `rollback.test.js`: engine, regeneration, partial-host, interrupted cases; four RED failures before fix, GREEN afterward |
| Interrupted manifest | A torn live manifest prevented the available rollback from being recognized. Use atomic JSON publication and recognize owned backup state. | `manifest-interrupted`: actual SIGKILL and next-doctor recovery |
| Interrupted cleanup | An interrupted recursive backup deletion was mistaken for an uncommitted update. Durable completion marker outside backup deletion covers both successful update and completed rollback. | `cleanup-interrupted`, `rollback-cleanup-interrupted`: actual SIGKILL, engine remains callable |
| Foreign/concurrent content | Missing-slot rollback unconditionally deleted a concurrently created foreign file; full instruction snapshots erased unrelated edits. Require current managed markers before deleting newly created assets and restore only owned instruction sections. | Foreign-content and concurrent AGENTS edit tests preserve content |
| New hosts after failed update | Snapshot used nonexistent `missing` state instead of engine's `absent`. Snapshot actual absent slots. | Newly created managed Cursor slot removed after failed update |
| Fresh-install retry | Partial engine installation had no ownership record, so retry treated it as foreign. Record owned `installing` state and permit bounded retry before any known-good registration exists. | Failed fresh installation repaired by retry |
| Broken entry-point repair | Snapshot required the broken executable, contradicting repair instructions. Permit an isolated `-I -m aperio` fallback only after module product and owned interpreter identity validation. | Missing and corrupt executable tests; foreign roots and symlinks still refused |
| Aggregate uninstall status | Valid JSON arrays hid nonzero process status/`ok:false`. Honor aggregate and project errors, retain engine on failure, validate identity before cleanup. | Host/project valid-JSON nonzero tests; existing missing/corrupt engine preservation tests |
| Ancestor symlinks | Direct Python integration operations checked only leaf/immediate parent. Check all ancestors before creation/read/removal, allowing only known macOS system aliases. | `test_symlinked_host_ancestor_never_writes_or_removes_outside_home` |
| Incomplete consent disclosure | Prompt promised only `.aperio/` while also adding AGENTS. Disclose managed instructions before consent in protocol and wizard. | `test_initialization_consent_discloses_persistent_instructions` |
| Initialization error exit | Pointer errors appeared in JSON but exit status was zero. Emit partial-state JSON then return failure. | `test_initialization_pointer_failure_is_nonzero_with_partial_state` |
| Codex project registration | Catalog omitted documented repository `.agents/skills`. Register that supported location. | `test_codex_project_registration_uses_documented_repo_skill_path` |
| Incorrect review status | `work-status` read obsolete decision/task/submitter keys. Use outcome, nested target and submitted_by; exclude decided reviews. | `test_work_status_tracks_actual_review_target_submitter_and_outcome`; focused 22 tests and separate reviewer 15 tests passed |
| Local npx quick-start | Bare tarball path was treated as a command and exited 126. Specify the package and executable explicitly. | Literal old command failed; `npx --yes --package <tarball> aperio-next <command>` lifecycle passed |

Changes are concentrated in the npm launcher/tests; Python integrations,
onboarding/CLI/wizard, work-status and their focused tests; packaging tests;
native skill guidance; verification scripts and documentation. The Python
engine was preserved. Existing engine identity/PATH-collision, quoting,
ownership, governance and evidence-binding regressions remain passing.

## Host acceptance matrix

“Registered” means observed managed assets, not successful model execution.
Personal host registrations were inspected read-only. New assets used in
live acceptance were installed only inside disposable fixtures.

| Host | Detected / registered | Discovered / activated | Engine invoked | Consented init | Fresh recovery | Daily-use status |
| --- | --- | --- | --- | --- | --- | --- |
| Codex CLI 0.167.0-alpha.4.1 | yes / yes | yes / yes | yes, correct isolated product/path | yes, automatic AGENTS pointer | yes, separate CLI process/thread and ordinary continuation prompt | full tested fixture journey on macOS |
| Codex VS Code extension | installed provider CLI is bundled with extension | fresh extension session not independently tested | not separately claimed | unverified | unverified | unverified UI scope |
| Claude Code 2.1.322 | yes / yes | current attempt unverified | current attempt unverified | unverified | unverified | blocked by `unrecognized_model` diagnostic for configured `nvidia/nemotron-3-ultra-550b-a55b:free`, then 120s timeout |
| OpenCode 2.0.32 | yes / yes | yes / yes, project skill | yes | not separately executed | yes, read-only recovery of Codex work | activation/recovery verified; full write workflow not separately tested |
| ZCode | configuration/managed assets present | unverified | unverified | unverified | unverified | no launchable zcode CLI on PATH; GUI not exercised |
| Gemini CLI | configuration/managed assets present | unverified | unverified | unverified | unverified | no launchable gemini binary on PATH |
| Cursor | configuration/managed assets present | unverified | unverified | unverified | unverified | neither cursor nor cursor-agent on PATH; GUI not exercised |

## Governance and independence

The native implementation session stopped at a genuine human fixture-plan
approval gate; approval was requested and received separately. It submitted
review without deciding it. A separate reviewer agent ran tests and made
its own review decision. Fresh continuation encountered enforced actor-role
and exact-evidence checks, then used implementer authorization and the
reviewed evidence. No fake owner or independent-review approval was inserted.
Manual handoff preparation did not use provider `--authorize-exec` or
`--authorize-write` flags; no such authorizations were silently supplied.

Actor IDs are declarations, not authenticated identity. Skills and project
pointers guide cooperative agents; they do not intercept arbitrary direct
filesystem changes. Deterministic enforcement applies to engine commands.
The separate-agent review uses the same available model/tools and is not
independent human certification. See the explicit reviewer findings report.

## Limitations

- macOS arm64 only; Windows/Linux runtime execution remains unverified.
- Codex CLI is verified separately from the VS Code UI. Other host results
  are scoped exactly as in the matrix; historical successful Claude runs
  are not substitutes for the current blocked attempt.
- The last optional final-artifact Codex recheck hit the provider usage
  limit after successful engine recovery reads. Earlier native coding and
  fresh continuation completed with exit 0; no additional model request,
  credit purchase or quota bypass was attempted after the limit.
- `next` remains onboarding-oriented and can return `evaluate_readiness`
  while work-status/recover describe an active task. Managed-project skill
  instructions explicitly load work status and recovery; the engine's task,
  review and authorization gates remain authoritative.
- Missing/corrupt executable repair requires a verified installed module.
  Complete module corruption without a usable backup is refused. An
  incomplete pre-mutation backup may require inspection. SIGKILL recovery
  is verified; sudden power loss is not.
- A local tarball installs the bundled version. Updating to a newer version
  requires building/selecting its new artifact; `update` is not a registry
  version-discovery service. npx does not leave a permanent launcher command.
- Public npm/PyPI publication and public releases were not performed.
  Registry lookup returned E404. CI claims require a newly observed hosted
  run; local test results do not establish CI success.
- Hosted CI remains externally blocked: `gh run view 43079219325` reports
  “The job was not started because your account is locked due to a billing
  issue” for each Python matrix job. No feature-branch run or PR was returned
  by the pre-push query. See [the observed run](https://github.com/SHAHID8142/aperio-next/actions/runs/43079219325).

## Verified quick-start

From this feature checkout, build and install the local release candidate:

```sh
./scripts/build-npm-package.sh
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next install
```

Open a project in Codex CLI, type `$aperio-next`, and expect a real engine
identity check plus project classification. Approve initialization when
prompted; it creates `.aperio/` and the managed AGENTS section while
preserving existing instructions. Approve the proposed work plan separately.
In a fresh chat in that project, say `Continue the previous task.`

```sh
"$HOME/.aperio-next/bin/aperio" core-identity --json
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next doctor
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next update
npx --yes --package ./dist/aperio-next-1.2.0-rc.2.tgz aperio-next uninstall
```

Require `product: aperio-next`. Update failure restores the previous owned
engine/assets; invoke doctor after interruption to recover. Uninstall removes
managed integrations and engine data while retaining project `.aperio/`.
The legacy Aperio product and unrelated configuration are not removal targets.

## Git and release boundary

Work remains on `feat/universal-agent-integration`. Commit/push results and
the final SHA/PR/CI state are reported after final review. Only ordinary
feature commits are authorized. No merge, force-push, package publication
or public release is authorized or performed. Pre-existing `.aperio/` and
`audit-inputs/` remain outside these commits.

Official mechanism references checked in this run:
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[Claude skills](https://code.claude.com/docs/en/skills),
[OpenCode skills](https://opencode.ai/docs/skills/),
[OpenCode commands](https://opencode.ai/docs/commands/).

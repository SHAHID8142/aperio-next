# Independent release review

## Review identity and scope

Reviewer: `/root/independent_review`, a separate agent from the implementation author, using the same available model/tool environment. This provides agent-level review independence; it is not independent human certification.

Initial review on 2026-11-12 covers the feature branch diff against `main`, current uncommitted lifecycle/onboarding changes, revision 2 acceptance criteria in `tasks/plan.md` and `tasks/todo.md`, and supplied baseline evidence. The author reported 1763 baseline Python tests, 17 baseline Node tests, 96 focused Python tests, and 21 Node tests including four rollback injections. These author results are distinguished from reviewer executions below. Final diff review remains pending while implementation and documentation change.

## Findings

### IR-01 — HIGH: direct integration commands follow symlinked ancestors

Location: `src/aperio/integrations.py`, `_prepare_managed_file`, `_atomic_write`, `_delete_managed_file` (initially around lines 736–807).

The original guards checked only the destination and immediate parent. A symlink at the host root redirected both creation and removal outside the intended directory.

Reproduction executed by reviewer: create disposable `home/.claude -> outside`, then call `install_for_host(host_by_id('claude-code'), home=home, engine_path='/fixture/aperio', version='fixture', scope='user')`. It returned `errors=[]` and created `outside/skills/aperio-next/SKILL.md`. `remove_for_host` returned `errors=[]` and deleted the redirected file.

Status: author added ancestor validation before mkdir/read/delete. Reviewer reran `.venv/bin/python -m pytest tests/test_integrations.py -k symlinked_host_ancestor -o addopts='' -q`: **1 passed, 62 deselected**. Resolved in the tested case; full final regression still pending.

### IR-02 — HIGH: uninstall discards aggregate failure status

Location: `packaging/npm/bin/aperio-next.js`, `cmdUninstall`, user and project cleanup report handling (initially around lines 569–625).

A parseable JSON `results` array causes removal processing to ignore a nonzero subprocess exit and `ok:false` unless individual result rows contain errors. This can delete the engine and report successful uninstall after failed cleanup.

Reviewer reproduction: install in a disposable root, then use a child preload at the `spawnSync` boundary returning `{status:1, stdout:JSON.stringify({results:[],ok:false}), stderr:'injected failure'}` for `integrations remove`. Observed **uninstall exit 0**, **engine root removed**, and no stderr. Project cleanup has the same unchecked-status pattern.

Required correction: aggregate status and malformed reports must retain the engine and return nonzero. Add deterministic user/project report tests.

### IR-03 — HIGH: rollback overwrites unrelated concurrent instruction changes

Location: `packaging/npm/bin/aperio-next.js`, `beginUpdate` project snapshots and `restoreUpdate` file replacement (initially around lines 192–219).

Rollback snapshots and replaces entire `AGENTS.md`/`CLAUDE.md` files, rather than preserving changes outside the owned section. Concurrent user work can be lost even when failure occurs before integration regeneration.

Reviewer reproduction: install, initialize/register a disposable project, append `user-before` to its `AGENTS.md`; inject a pip failure after appending `user-concurrent-edit` to that file. Observed update exit 1 and rollback restoration; `user-before` survived but **`user-concurrent-edit` was erased**.

Required correction: preserve non-owned instruction content or detect an intervening modification, retain backup, and report a conflict instead of overwriting it. Add regression coverage.

### IR-04 — HIGH: onboarding reports success after required integration failure

Location: `src/aperio/cli.py`, `_cmd_onboarding_confirm_init` (initially around lines 5855–5879).

The command emits the protocol but does not propagate `project_integration.errors` to its exit status. This violates mandatory automatic durable integration acceptance.

Reviewer reproduction: create a disposable project with `AGENTS.md` as a directory; call `main(['onboarding-confirm-init', project, '--approve', '--json'])`. Observed **exit 0**, `.aperio/state.json` created, and `project_integration.errors` containing `Is a directory`.

Required correction: preserve the valid partial initialization and JSON diagnostics, but return nonzero when required integration failed. Add a regression test and verify safe retry.

### IR-05 — HIGH: documented repair path refuses a missing engine

Location: `packaging/npm/bin/aperio-next.js`, `beginUpdate` identity precondition and `cmdUninstall` repair guidance (initially around lines 174–176 and 645–654).

The new transaction precondition refuses update when the current engine is broken or missing. Uninstall still directs users to `aperio-next update` to restore that engine. Neither reinstall nor update can pass the same `beginUpdate` precondition.

Reviewer reproduction: install in a disposable root; remove `venv/bin/aperio`; run `update`. Observed **exit 1**: `cannot snapshot an unverified engine; restore a known-good installation before updating`.

Required correction: provide a safe bounded repair path from the trusted bundled wheel while retaining existing registrations/configuration, or leave this mandatory lifecycle requirement explicitly unmet. Add repair and failure-recovery tests.

## Governance and acceptance observations

- Reviewer did not approve plans, write `.aperio/` files, or fabricate `review-decide` outcomes.
- Engine-side plan approval, task lifecycle, and independent evidence binding remain distinct from native cooperative skill instructions. A skill cannot deterministically enforce arbitrary direct agent filesystem edits.
- Native Codex activation, governed coding, and genuinely fresh-session recovery were not executed by this reviewer during initial source review and are not certified here.
- Packaging and wheel/native TOML resource handling are covered by existing tests and author baseline evidence; final installed-artifact results are still required.
- Reviewer executed all destructive reproduction operations only in newly created disposable fixtures and removed those fixtures afterward.

## Initial verdict

**Changes required.** IR-02 through IR-05 remain release-blocking findings at this review checkpoint. IR-01's focused correction was independently verified. A final review of fixes, final documentation, package/lifecycle evidence, and the complete final diff is required before independent acceptance can be reported complete.

## Follow-up review: additional crash windows

The author supplied corrections and regression tests for IR-01 through IR-05. Reviewer independently reran `.venv/bin/python -m pytest tests/test_integrations.py tests/test_onboarding_cli.py tests/test_packaging.py -o addopts='' -q`: **101 passed in 41.43s**. This includes the ancestor-symlink guard, onboarding error propagation, Codex project-scope registration, and installed-wheel resource verification. Independent Node rollback revalidation is in progress. These focused results do not certify unfinished native activation or final release acceptance.

### IR-06 — HIGH: interrupted manifest write blocks available rollback

Location: `packaging/npm/bin/aperio-next.js`, `writeManifest`, `validateInstallRoot`, and main recovery ordering.

Reviewer injected a real SIGKILL during an update's manifest write: child preload intercepts `fs.writeFileSync(install-root.json)`, writes `{`, then kills the launcher. The complete rollback journal remains present. The next `doctor` exits **1**, reporting `foreign installation root`, because manifest-based ownership validation runs before recovery. Thus a valid backup cannot recover a realistic interrupted-write state.

Required correction: publish manifests atomically, and make backup-based recovery safe when the live manifest is torn; journal validation must precede any destructive restoration.

### IR-07 — HIGH: interrupted successful commit cleanup destroys the working engine

Location: `packaging/npm/bin/aperio-next.js`, `commitUpdate` and `recoverUpdate`.

There is no durable committed phase before recursive backup removal. Reviewer intercepted `fs.rmSync(BACKUP)` after a successful update, removed `BACKUP/bin`, then sent SIGKILL, simulating interruption during recursive cleanup. On the next `doctor`, recovery interpreted the surviving journal as uncommitted, deleted the working engine `bin`, and failed copying the already deleted backup `bin` with `ENOENT`. Observed **doctor exit 1** and **engine shim missing**.

Required correction: atomically record the committed state before deleting backup data. Recovery of a committed transaction must finish cleanup only, never restore an incomplete backup. Add a real interruption regression.

### IR-08 — HIGH: rollback deletes concurrent foreign files in formerly absent slots

Location: `packaging/npm/bin/aperio-next.js`, `restoreUpdate`, prior-absent asset removal.

Reviewer installed into an isolated no-host HOME, then injected a pip failure after creating `~/.cursor/skills/aperio-next/SKILL.md` with plain `concurrent foreign user file` content. That slot was absent at snapshot time. Rollback unconditionally unlinked it: **update exit 1, foreign file did not survive**.

Required correction: verify current ownership before deleting newly appeared files. Preserve/report foreign conflicts rather than erasing user configuration. Add a concurrent-file regression.

### Independent revalidation of IR-01 through IR-05

Reviewer command `node --test packaging/npm/test/rollback.test.js` completed with **13 passed, 0 failed, 0 skipped in 132.46s**. This run independently verifies user/project aggregate-removal failure propagation (IR-02), concurrent instruction preservation (IR-03), and missing/corrupt executable repair through the owned isolated module (IR-05), as well as the original four rollback injections, fresh-install retry, foreign/symlinked root refusal, new-registration rollback, and project-pointer aggregate failure.

Combined with the **101 passing focused Python tests**, IR-01 through IR-05 are resolved in the exercised cases. The repaired module fallback uses isolated Python `-I` and checks its reported interpreter location against the owned venv. This is recovery for a missing/corrupt entry-point executable when the owned module/runtime remains available, not certification of arbitrary destroyed-runtime recovery.

The author has applied corrections for IR-06 through IR-08 (atomic manifest publication, separate committed marker, backup-based ownership recovery, and foreign-file preservation). Independent regression execution of those latest corrections remains pending.

### Independent revalidation of IR-06 through IR-08

Reviewer ran `node --test --test-name-pattern='manifest-interrupted|cleanup-interrupted' packaging/npm/test/rollback.test.js`: **2 passed, 0 failed in 17.91s**. Reviewer also ran `node --test --test-name-pattern='corrupt-manifest|committed-backup|foreign content' packaging/npm/test/rollback.test.js`: **1 passed, 0 failed in 8.85s**, the matched test being concurrent foreign-file preservation. IR-06 and IR-08 are resolved in these exercised cases; the successful-update cleanup path of IR-07 is corrected.

IR-07 follow-up found the identical crash window in **rollback completion cleanup**: `restoreUpdate` still deleted the backup without first writing a durable completion marker. Reviewer injected pip failure, then interrupted rollback's recursive backup cleanup after deleting `BACKUP/bin`. Next `doctor` again deleted the working shim and failed copying the missing backup bin with `ENOENT`. This is the remaining path of IR-07; rollback completion also needs a durable completed/restored phase before backup deletion and its own interruption regression.

Reviewer inspected the packed-artifact lifecycle log `.qa/lifecycle.jsonl`: install, doctor, identity, approved disposable onboarding, explicit Codex project registration, update, and uninstall all exited zero; project-state bytes survived. Reviewer inspected `.qa/codex-attempt.json`: a fresh Codex CLI process invoked the local skill, the correct isolated identity, and real `aperio next`, observed `empty_new` with `confirm_initialization`, and stopped without mutation. This proves live CLI activation/engine invocation and the consent boundary. It does not yet demonstrate consented initialization, governed coding, or fresh-session recovery, and provides no fresh VS Code extension evidence.

The project-update implementation now refreshes only hosts whose project files are already managed and passes explicit host selectors, avoiding unrelated project host directories.

### Final source-fix revalidation

The author applied the durable completion marker to rollback completion as well as successful update commit. Reviewer independently ran `node --test --test-name-pattern='rollback-cleanup-interrupted' packaging/npm/test/rollback.test.js`: **1 passed, 0 failed, 0 skipped in 8.45s**. The real SIGKILL rollback-cleanup scenario now recovers on the next invocation with the working engine and original integration bytes intact. IR-07 is resolved across both reproduced cleanup paths.

All eight HIGH findings identified by this reviewer are resolved in the independently exercised regression cases. No additional confirmed HIGH defect was found in the reviewed module fallback, lifecycle scripts, or restriction of project refresh to already managed host registrations. This does not imply exhaustive proof of absence of defects.

### Independent disposable coding review

Reviewer independently inspected `.qa/native-v3ZCOg/home/project/greeting.py` and `test_greeting.py` against the actual approved fixture plan: a dependency-free `greet(name)` helper, exact `greet('Aperio') == 'Hello, Aperio!'`, and a focused unittest within the two-file code scope. Reviewer ran `python3 -m unittest`: **1 test, OK**, and the exact greeting assertion: **exit 0**. No code defect was found in this bounded fixture.

The engine showed pending `REV-0001`, submitted and implemented by `native-implementer`, bound to task `TASK-001`, content plan revision 1, run `RUN-0001`, attempt 1. The reviewer recorded their actual independent decision through `review-decide ... --actor independent-fixture-reviewer --outcome approved`, retaining submitted evidence and the disclosed limitation that `python3` was used because `python` was unavailable. The engine accepted this decision. Reviewer did not approve the human plan or mark the task complete. A first overly long findings string was refused by the engine's 300-character input bound and was replaced with a concise truthful finding.

Source-fix verdict: **no unresolved confirmed HIGH findings from this review**. Final release acceptance remains pending genuine fresh-session recovery and final documentation/evidence review.

### Work-status integration follow-up

The reviewer also observed a nonblocking schema mismatch in pending-review rendering: `work-status` showed `REV-0001 on None (submitted by None)` and continued showing already decided reviews as pending. The source expected nonexistent `decision`, `task_id`, and `submitter` fields instead of actual `outcome`, `target.task_id`, and `submitted_by` fields. The author corrected this and added `test_work_status_tracks_actual_review_target_submitter_and_outcome`.

Reviewer independently ran that regression plus `tests/test_checkpoint.py`: **15 passed in 1.60s**. Pending review links now use the actual schema and approved reviews leave the pending list. The original live fixture package predates this small source fix; live OpenCode output from that fixture therefore still exhibits the old rendering, which must not be described as a new-source failure or silently rewritten evidence.

Reviewer inspected `scripts/verify-release.cjs`, `verify-codex.cjs`, `codex-fixture-session.cjs`, and `verify-other-hosts.cjs`: subprocess requests are bounded, use disposable project/engine roots, and do not invoke permission-bypass switches. The OpenCode log shows actual isolated-engine identity, `next`, `memory`, `work-status`, and `recover` execution against the Codex fixture, with mutation prohibited. This supports read-only cross-agent context recovery, not cross-agent coding completion. Claude's attempt did not establish activation success.

## Final disposition — 2026-11-13, Asia/Dhaka

The separate reviewer completed follow-up review of the full feature diff against `main`, current implementation changes, all acceptance additions, tests, verification scripts, task documents, README/install/package README, historical-evidence banners, and `release-candidate-evidence.md`. All eight reproduced HIGH findings above have corrections and independently exercised regression evidence. The additional review-schema defect was also corrected and independently tested. No unresolved confirmed critical or HIGH defect remains from this review in the tested macOS/isolated-artifact/Codex CLI scope.

Reviewer directly inspected the final machine-local result files: `.qa/python-final.log` records **1769 passed in 112.98s**; `.qa/node-final.log` records **34 passed, 0 failed, 0 skipped in 151.66s**; `.qa/lifecycle-final.jsonl` ends with successful packed installation, identity, doctor, project registration, update, uninstall, and preserved project-state bytes. These full-suite runs were executed by the author; the separate focused regression executions reported above were executed by this reviewer.

Reviewer directly inspected the successful plain-continuation transcript `.qa/codex-recovery.json`: a fresh Codex thread recovered the existing plan/run and this reviewer's actual `REV-0001` decision, independently reran the fixture test, encountered and respected role/input/evidence gates, completed the task using the exact approved evidence, completed its run, and wrote handoff/checkpoint revision 5. Integrity returned OK and recovery became eligible with one verified completed task. The process completed successfully with exit 0 and a final response. This supplies the previously missing genuine fresh-agent continuation evidence; it is not a Python-subprocess simulation.

Reviewer separately inspected `.qa/codex-final-recovery.json` after the fixture engine was refreshed to the final artifact. A different fresh thread successfully executed identity, `next`, `work-status`, memory, and readiness; work status showed completed work and no false pending review. The provider then returned a usage-limit failure before the final response, and the process exited 1. This is partial final-artifact recheck evidence, not a second successful completed agent turn. The release evidence correctly preserves that distinction.

Documentation follow-up corrected the local npx command, absence of a persistent launcher command after npx, registry access and user-configuration effects, current Python-version scope, and automatic AGENTS integration disclosure. Historical test counts/host-access statements are explicitly marked historical. Public npm installation, untested hosts, VS Code extension sessions, Windows/Linux runtime behavior, power-loss durability, destroyed-module repair, and hosted CI are not asserted as newly verified.

The author subsequently confirmed the packaged README correction, final tarball rebuild and four expected package files, plus **69 focused integration/packaging tests passed in 30.92s**. These are author-executed final artifact checks; the reviewer has inspected the corrected packaged README and clean whitespace diff.

Final review verdict: **USABLE WITH DOCUMENTED LIMITATIONS**, consistent with the release evidence. The code and reviewed documentation are acceptable for ordinary feature-branch commit/push under the existing authorization. The author remains responsible for reporting the actual final Git state. This review does not authorize merge, publication, public release, or additional provider quota use. Agent-level independence is established; independent human certification is not claimed.

# Troubleshooting

Every Aperio failure is one actionable `aperio: error: …` line (exit 1).
This page maps the common ones to their causes and fixes.

## "no memory store at <path>; create one with 'aperio mem-propose --store …'"

Aperio never creates, discovers, or guesses a memory store: pass the same
explicit `--store`/`--memory-store` path you created it with.

## "refusing to follow symlink at …"

A symlink sits where Aperio would write or read an input. Replace the
link with the real file (or point the command at the real path) so the
bytes match what you inspected. Dangling links are refused the same way
for write targets.

## "the destination already exists; pass --force to overwrite it"

A report/brief/exchange output file already exists. Inspect it, then add
`--force` to replace it atomically, or choose another `--output`. Without
`--force` nothing is ever overwritten.

## "an identical file already exists" / "exists with different content" (exchange-import)

`imported-*` names are content-derived. Identical content → already
present, nothing to do. Different content occupying the name → resolve
the collision explicitly (the importer refuses rather than overwrite).

## "someone reviewed it in parallel — re-read and retry" (feedback-review --replace)

The file's revision moved since you read it. Re-read the file and retry
with the current `--expected-revision`; nothing was modified.

## "goal: field 'intended_outcome' is required" / unknown fields

Goal documents are strictly validated: unknown fields are rejected (a
typo must not silently drop a requirement). See the field table in
[goal-contract.md](goal-contract.md).

## "memory 'MEM-…' trust label '…' disagrees with its recorded history"

A record claims more trust (or a `verification.basis`) than its recorded
review events support. Check the history with `aperio mem-show`, fix the
document, or record the real review with `aperio mem-review`. A record
whose history genuinely lacks the review reads as `unrecorded` and is
shown at the level the history proves.

## "store … refuses further appends after damage"

The store ends mid-line or a line breaks the chain — usually an
interrupted write or a hand edit. The complete records stay readable;
inspect the file, repair the tail by hand, and never append past a
broken chain.

## "executions.json is corrupt" / "unsupported exec-schema version"

Too-new schemas are refused rather than partially interpreted. Upgrade
Aperia, or inspect with `aperio integrity` (read-only) — the file is
never rewritten automatically.

## "your account is locked due to a billing issue" (GitHub Actions)

Repository owner action: github.com/settings/billing, then re-run the
workflow. This is an external infrastructure blocker; all local
verification stands on its own.

## "adapter 'codex-cli' cannot be launched safely" / capability probe failed

The installed provider CLI does not advertise the flags Aperia relies
on, or is missing. Run `aperio agent-doctor --agent codex-cli` for the
passive diagnosis; install or update the provider CLI; or use the
`manual` adapter / copy-paste workflows — no provider is ever required.

## A quality finding you disagree with

Every finding names its rule, the requirement it came from, and its
evidence. `confirmed` means a deterministic rule matched (check the
quoted evidence); `warning`/`recommendation` mean judgment is yours;
`unknown` means the tool says it cannot decide. Correct the goal, the
request, or the deliverable — or accept and move on; findings gate
nothing.

## A task keeps failing and the agent keeps retrying it

Aperio enforces bounded retries in code, not by convention: a failed
run is keyed by a **problem signature** (task + adapter + exit code +
recorded outcome), and at most **3 attempts** are allowed per signature.
A retry must declare what changed (`aperio run-start ... --remediation
"new evidence or changed hypothesis"`); repeating the same remediation
text is refused, and the 4th attempt on the same signature is refused
with escalation guidance.

When the budget is exhausted, stop retrying:

1. `aperio recovery-report DIR TASK` — read-only debugging history
   (every attempt, its outcome, and the declared remediation).
2. `aperio task-status DIR TASK --status blocked --reason "exhausted
   retry budget for signature <sig>"` — blocked tasks are never
   dispatched.
3. Record the findings (`aperio decision-add`) and change the approach
   materially (a new plan revision), switch adapter after
   `aperio agent-doctor`, or decide yourself as the operator.

The signature is a coordination boundary, not a root-cause analyzer:
two different root causes that produce the same recorded outcome share
a budget, and the same cause presenting differently does not. The
report history is the evidence a human reads.

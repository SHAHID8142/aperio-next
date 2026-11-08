# Universal goal contract (M2)

`goal-schema v1` describes *any* knowledge-work assignment in a
provider-neutral way. It is input to Aperio, not execution: building,
validating, or converting a goal never launches an agent, approves work,
verifies a claim, or writes into a project.

```
aperio goal-check goal.json          # validate + report gaps
aperio goal-pack  goal.json          # goal → portable context pack
aperio profiles                      # discover the built-in task profiles
```

Everything on this page works without a project, a plan, a provider CLI, a
model API, or any code. A project is optional everywhere.

## Why a separate contract

M1 already assembled context from an objective and operator fields. M2 adds
the missing piece: a *validated, versioned description of the assignment*
that can be stored, diffed, reviewed, and handed to any agent — a graphic
design brief, a research question, an article assignment, a business
decision, or a one-off general question, with the software workflow as one
optional domain.

The goal contract is deliberately small. Required fields are `objective`
and `intended_outcome`; everything else may be absent, and absent
information is reported as absent. Aperio never invents a constraint, a
deliverable, or an acceptance criterion to make a goal look complete.

## Goal document (goal-schema v1)

A goal is a JSON object. Unknown fields are rejected, so a typo cannot
silently drop a requirement.

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `goal_schema_version` | no (default `1`) | integer | Contract version; any other value is refused. |
| `goal_id` | no | string | Optional declared identity (`[A-Za-z][A-Za-z0-9_-]{0,63}`). |
| `objective` | **yes** | string | What is wanted, in the operator's words. |
| `intended_outcome` | **yes** | string | What a successful result looks like. |
| `profile` | no (default `general`) | string | One of the built-in profiles. |
| `constraints` | no | list of strings | Limits the result must respect. |
| `requirements` | no | list of strings | Explicit things the result must do. |
| `assumptions` | no | list of strings | Things taken as given, stated by the operator. |
| `uncertainties` | no | list of strings | Known unknowns. Rendered as missing information. |
| `risks` | no | list of strings | What could go wrong. |
| `audience` | no | string | Who will read the result. |
| `deliverables` | no | list of strings | Concrete things to produce. |
| `quality_criteria` | no | list of strings | How quality is judged. |
| `acceptance_criteria` | no | list of strings | What must be true to call it done. |
| `output_contract` | no | object | `format`, `structure`, `length`, `tone`, `language`. |
| `evidence_requirements` | no | list of strings | What evidence must accompany the result. |
| `work` | no | object | Optional work decomposition (`tasks`). |
| `provenance` | no | object | `source`: `operator`, `agent`, or `imported`; optional `note`. |

`requirements` and `constraints` are kept distinct in the goal and stay
distinct in the context pack (rendered as two subsections of "Requirements
and constraints"). They are never merged into one list, so neither cap can
silently swallow the other's entries, and no requirement is represented
twice.

Bounds (all enforced before anything is written): goal file ≤ 64 KiB,
objective ≤ 16 KiB, every other text field ≤ 8 KiB UTF-8, **≤ 50 entries per
list** (so up to 50 requirements *and* up to 50 constraints — 100 items
combined — are valid), ≤ 200 tasks. Every goal that passes `goal-check` can
be converted by `goal-pack`; the two commands share the same bounds. Text must be valid UTF-8, must not contain NUL, and must
use only `\n` line breaks (CR, NEL, U+2028/U+2029, and the ASCII separators
are rejected by the same rule that guards decisions and handoffs).

Example — a design goal:

```json
{
  "goal_id": "GOAL-GARDEN-ID",
  "objective": "Design a visual identity for a community garden",
  "intended_outcome": "A reusable identity direction and an A3 poster layout",
  "profile": "design",
  "constraints": ["Accessible contrast at A3 and on mobile"],
  "audience": "Neighbourhood residents and a volunteer committee",
  "deliverables": ["Identity direction (2 options)", "A3 poster layout"],
  "quality_criteria": ["Legible from 2 m", "Works in one colour"],
  "acceptance_criteria": ["A printer can quote from the spec without questions"],
  "evidence_requirements": ["Name the brand rules and assets consulted"],
  "output_contract": {"format": "direction summary plus specs", "length": "2 pages"},
  "uncertainties": ["Final event date is not fixed"]
}
```

The same shape works for research (`evidence_requirements`,
`uncertainties`), writing (`audience`, `output_contract`), business
(`assumptions`, `risks`, `acceptance_criteria`), or a plain question.

## Identity

- `goal_content_sha256` is a SHA-256 over the canonical, normalized goal
  (sorted keys, compact separators, UTF-8). No timestamp, path, host, or
  package version enters it: the same goal has the same identity on any
  machine, in any order of keys.
- `goal_identity` is the declared `goal_id` when present, otherwise
  `GOAL-<first 12 hex of the content hash>`.
- There is no authentication here. A hash detects accidental changes and
  supports comparison; it does not prove authorship or approval.
- Goal identity is *not* a plan revision or an approval. It never appears in
  a completion gate.

## Task profiles

A profile says how a domain *usually* expresses work. It is plain data in
`aperio/profiles.py` — no plugin loader, no dynamic import, no executable
rules engine, no third-party dependency.

| Profile | Typical focus |
| --- | --- |
| `general` (default) | objective, outcome, audience — domain-neutral |
| `design` | audience, deliverables, constraints, quality criteria |
| `research` | objective, evidence requirements, uncertainty, audience |
| `writing` | audience, outcome, output contract, requirements |
| `business` | objective, assumptions, risks, acceptance criteria |
| `software` | objective, requirements, constraints, acceptance criteria, evidence |

A profile contributes **suggestions only**: suggested quality criteria,
suggested evidence, and expected output shape. They are rendered under a
heading that says so, they are stored separately from the operator's
explicit fields, and they never become an approval or completion
requirement. `aperio profiles --json` shows the registry verbatim so a
reviewer can check that no rule is hidden in code.

The `software` profile does not change any Feature 01–07 behavior. It only
adds software-oriented guidance to a pack when the operator selects it.

## Work decomposition (optional)

```
"work": {
  "tasks": [
    {"id": "collect", "title": "Collect sources", "action": "Gather 5 sources"},
    {"id": "draft", "title": "Draft", "action": "Write 800 words",
     "depends_on": ["collect"], "completion_criteria": ["Draft is readable"],
     "verification": ["Operator reads it"], "owner": "researcher-1"}
  ]
}
```

Per task: `id`, `title`, `action` are required; `result`, `deliverables`,
`preconditions`, `depends_on`, `completion_criteria`, `verification`,
`risks`, and `owner` are optional and default to empty.

Validation reuses the plan-schema v1 graph rules (`planmodel.
validate_dependency_graph`): unique ids, no self-dependency, no dependency
on an unknown task, and no cycles. The goal layer therefore inherits a
already-reviewed validator instead of growing a second implementation.

**A one-off question needs no graph.** `work` is absent by default, and
`work_graph()` reports `task_count: 0` rather than inventing tasks. The task
graph in a goal is a *plan for a reader*, not the execution lifecycle:
nothing here becomes `plan.json`, no task status is tracked, and no
approval is implied.

## Standalone mode (Mode A)

```
aperio goal-check goal.json
aperio goal-pack  goal.json > pack.md
```

- No `.aperio/` directory is read or created.
- No plan, handoff, provider, model API, or code is required.
- The default profile is `general`.
- Output goes to stdout unless `--output FILE` is passed; writing a file is
  the only filesystem effect, and only when explicitly requested.

### Output file safety (`--output`)

`goal-pack --output` uses `artifacts.publish_text_atomic`, the same
exclusive-creation policy as Aperio's run artifacts:

- The content is written to a uniquely named temporary file created with
  `tempfile.mkstemp` (`O_CREAT|O_EXCL`) in the destination directory. A
  planted symlink or leftover file at any guessed temporary name is never
  opened, followed, or truncated, and concurrent commands cannot collide on
  a shared temporary path.
- Without `--force` the file is published with `os.link`, which fails
  atomically when the destination exists. A destination created *after* the
  earlier existence check is therefore still never overwritten, including
  under concurrent writers: exactly one of N racing commands wins.
- With `--force` the file is published with `os.replace`, after the leaf is
  proven not to be a symlink or non-regular file; a previous version is
  replaced atomically.
- Cleanup removes only the temporary file the current call created (inode
  re-check), and a failed write leaves the previous destination untouched.
- The destination is refused when its resolved form contains a `.aperio`
  path component (Aperio-managed project state, including through a
  symlinked directory) or when it is the goal document itself. A missing or
  non-directory parent is refused.
- Published files are created owner-readable/writable only (`0600`); a pack
  may contain operator material, so it is not world-readable by default.

Platform support and limits:

- Without `--force`, publication uses `os.link` only. Hard-link creation is
  atomic on POSIX filesystems that support links, so a concurrent creator can
  never lose its file to Aperio.
- If hard links are unavailable or refused — `ENOTSUP`, `EOPNOTSUPP`,
  `ENOSYS`, `EXDEV`, `EMLINK`, `NotImplementedError` — publication **fails
  closed** with an actionable error that names `--force`. There is
  deliberately **no** check-then-replace fallback: a non-atomic fallback
  could silently overwrite a concurrently created file.
- `EPERM`, `EACCES`, and `EROFS` are reported as a permission failure;
  other I/O errors are reported as write failures. Neither path overwrites
  anything, and neither is retried with a weaker mechanism.
- `--force` uses `os.replace`, which is an atomic replacement on POSIX and
  fails cleanly on Windows when the destination is open in another process.
- Local test evidence is macOS and Linux only. Windows is supported through
  the same code paths, but no Windows-specific behaviour (permissions,
  replacement semantics, antivirus interference) is claimed as tested.

## Project-backed mode (Mode B)

```
aperio goal-pack goal.json ./my-project --json
```

The workspace contributes exactly the M1 shareable context (brief,
decisions, structured handoff, plan/execution summaries, currently
applicable reviews) under the same trust rules. It is read-only: no
`.aperio/` file changes, no migration, and `.aperio/local/` is never read.
If the workspace is missing or uninitialized, the command fails closed with
an `aperio: error:` line rather than degrading silently.

Legacy plans are never converted. A goal is a separate document with its own
schema; it does not rewrite `plan.json`, `decisions.md`, `handoff.md`,
reviews, or the portable archive.

## Goal → context pack

`build_goal_context_pack()` produces an ordinary M1 context pack:

| Pack field | Source |
| --- | --- |
| `objective` | goal `objective` |
| `profile`, `profile_version` | goal `profile` |
| `requirements` | goal `requirements` (separate list) |
| `constraints` | goal `constraints` (separate list) |
| `assumptions`, `risks`, `quality_criteria` | same-named goal fields |
| `output_contract.expected_output` | goal `intended_outcome` |
| `output_contract.format` | goal `output_contract.format` |
| `output_contract.audience` | goal `audience` |
| `missing_information` | goal `uncertainties` |
| `content.goal` | identity, hash, trust, authority, declared source, completeness |
| `content.goal.deliverables`, `.acceptance_criteria`, `.evidence_requirements`, `.output_contract`, `.work` | only for operator-declared goals |
| `content.goal.untrusted_instructions` | agent/import-declared goals: every instruction-bearing field, verbatim, with attribution |
| `content.profile_suggestions` | advisory profile suggestions |

Consequences of this mapping:

- Nothing is counted twice: deliverables, acceptance criteria, and evidence
  requirements live in the goal block only; uncertainties appear only as
  missing information.
- Requirements and constraints keep their own caps and their own rendering.
  A pack produced without `requirements` (every M1 caller) is byte-identical
  to before.
- **Instruction authority follows the declared source.** Only an
  operator-declared goal may populate the pack's authoritative instruction
  sections (requirements, constraints, assumptions, risks, quality criteria,
  expected output/format/audience, missing information). An `agent` or
  `imported` goal contributes nothing to those sections; its instructions are
  preserved verbatim under `content.goal.untrusted_instructions` and rendered
  under "Proposed goal instructions (not operator-approved)", after the
  authoritative sections. The objective stays visible but its heading and
  body are labelled with the declared source. `content.goal.authority` is
  `operator` or `untrusted`, so JSON and Markdown agree without prose
  parsing.
- The pack keeps M1's reproducible content hash, whole-item byte budget,
  trust labels, and Markdown/JSON agreement. `goal-pack` output verifies
  against its own hash.
- Agent-declared provenance (`provenance.source: "agent"`) makes the goal
  `proposed`, adds a `goal_trust_not_operator` warning, and prints a notice
  that the text is data to assess, not instruction.
- The goal's provenance is also recorded as the pack's `objective_source`
  (`operator`, `agent`, or `imported`), so an agent-written objective is
  never presented as if the operator wrote it.
- No provider adapter, runtime, or model-specific field is imported by the
  goal layer (asserted by a test).

## Trust, safety, privacy

- Trust labels are the M1 set: `authoritative`, `verified`, `attested`,
  `proposed`, `imported`. A goal's provenance only selects the tier; it never
  upgrades project evidence.
- **Instruction authority is separate from mere visibility.** An
  agent-origin (`proposed`) or imported goal is still transferred and
  inspectable, but its requirements, constraints, intended outcome,
  assumptions, uncertainties, risks, audience, deliverables, quality and
  acceptance criteria, evidence requirements, output contract, and work
  decomposition never appear as operator instructions. They render as
  attributed proposals, and no field is labelled an "explicit operator ask".
- Untrusted goal text cannot override operator instructions: the pack states
  that non-authoritative goal text is data.
- There is no promotion mechanism: converting an agent/imported goal more
  often never raises its authority, and no command turns a proposal into
  operator instruction. Promotion is future work (M3+), and the declared
  source remains attribution, not authenticated identity.
- Sensitive material stays out: no environment variables, credentials, raw
  provider output, machine-local logs, absolute paths, or `.aperio/local/`
  content enters a goal-derived pack. Filenames of imported/proposed context
  are not copied in.
- Preparation, execution, verification, and completion stay separate. A goal
  is preparation. `goal-check`/`goal-pack` cannot complete a task, approve a
  plan, or produce verification evidence.

## Compatibility

- No change to project state schema v1, plan-schema v1, execution/review
  records, or the Feature 05 portable archive v1. No new mandatory project
  file, no auto-migration, no archive v2.
- New module `aperio/goals.py` (goal-schema v1) and `aperio/profiles.py`
  (profile registry v1), both owned by the universal layer and stdlib-only.
- The M2 goal block gained two additive informational keys (`authority`,
  `declared_source`) and, for non-operator goals, `untrusted_instructions`.
  Operator-origin pack behavior is otherwise unchanged, and M1 packs without
  a goal are byte-identical to earlier releases.
- `context_pack` gains one optional keyword (`goal_context`) and derives its
  profile ID/version/guidance from the registry; existing M1 output is
  unchanged when no goal is attached.
- `planmodel._validate_task_graph` is now the public
  `validate_dependency_graph(tasks, *, artifact="plan")`; plan-schema v1
  callers and messages are unchanged.
- Existing commands, exit codes, and error format are unchanged: one
  `aperio: error: ...` line, never a traceback, 130 on interruption.

## Python API

```python
from aperio.goals import (
    build_goal_context_pack, describe_goal, goal_content_sha256,
    goal_identity, load_goal_file, validate_goal, work_graph,
)
from aperio.context_pack import render_context_pack_markdown
from aperio.profiles import get_profile, list_profiles, profile_suggestions

goal = load_goal_file("goal.json")          # validated + normalized
report = describe_goal(goal)                # identity, trust, gaps, graph
pack = build_goal_context_pack(goal)        # provider-neutral context pack
print(render_context_pack_markdown(pack))
```

`validate_goal()` is idempotent, returns every optional field explicitly
populated, and raises `ValidationError` naming the offending field.

## Known limits

- No formal JSON Schema is shipped; validation is code, and this page is
  its human-readable contract.
- Profiles are suggestions, not domain ontologies. They do not prove a goal
  is well-formed for its domain.
- A goal pack describes intent. It is not a measurement that any agent
  performed better, and it does not sandbox or constrain the receiving
  agent.
- Provider execution remains optional and carries the documented Feature 07
  P1 metadata-bound defect; M2 does not change or resolve it.

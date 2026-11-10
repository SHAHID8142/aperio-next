# Two-level planning: master roadmap + detailed plan

Aperio plans projects at two levels with **one execution engine**:

- **LEVEL 1 — Master roadmap** (`.aperio/roadmap.json`,
  `roadmap-schema v1`): the whole project lifecycle — objective,
  lifecycle description, milestones with the major capabilities each
  delivers, milestone dependencies, risks, and acceptance conditions.
  Created with `aperio roadmap-create`, elaborated progressively with
  `aperio roadmap-revise`, approved with `aperio roadmap-approve` after
  genuine review.
- **LEVEL 2 — Detailed plan** (`.aperio/plan.json`, `plan-schema v3`):
  the executable level for the current milestone — objective, scope,
  acceptance criteria, and `TASK-…` units with evidence-required
  completion, independent review, and governance. This remains the
  **only** source of execution truth.

The linkage is one-directional and optional: a plan may declare the
milestone it implements (`plan-create --milestone MILESTONE-1` or a
`milestone` field in the plan input). The roadmap never points back and
stores **no execution state** — milestone progress is derived from which
milestone the current plan implements, never stored or guessed, so the
two documents cannot contradict each other.

## Rules

- A roadmap is created **draft** (revision 1). Every revision resets
  approval to draft — material planning changes require renewed human
  approval, exactly like plans.
- Governance mirrors plans: `roadmap.create`/`roadmap.revise` are
  planner actions, `roadmap.approve` is a planner/reviewer action, and a
  governed roadmap approval refuses its own author (separation of
  duties) with fail-closed authorship attribution.
- `roadmap-revise --revises N` must match the current `roadmap_version`;
  stale revisions are refused. Later milestones are elaborated when
  information arrives — the roadmap does not require upfront detail.
- The roadmap is optional: projects may use plans alone. `status` does
  not report it; use `roadmap-show`.

## Example

```bash
aperio roadmap-create . --file roadmap.json
aperio roadmap-approve . --by "owner"      # after genuine review
aperio plan-create . --file plan-m1.json --milestone MILESTONE-1
aperio plan-approve .
```

`roadmap.json` input (strict content keys: `objective`, `lifecycle`,
`milestones`; each milestone: `id` (`MILESTONE-<digits>`), `title`,
`capabilities` (≥1), `depends_on`, `risks`, `acceptance_conditions` (≥1)):

```json
{
  "objective": "Deliver a trustworthy local-first site builder",
  "lifecycle": "discovery -> planning -> build -> verify -> release",
  "milestones": [
    {
      "id": "MILESTONE-1",
      "title": "Project skeleton",
      "capabilities": ["working scaffold"],
      "depends_on": [],
      "risks": ["scope creep"],
      "acceptance_conditions": ["scaffold boots", "tests green"]
    }
  ]
}
```

## Honest limitations

- The roadmap is a planning document, not a scheduler: milestone
  ordering is declared through `depends_on`, and what is *executable
  right now* is decided only by the approved plan's derived readiness
  (`aperio task-list`).
- No `roadmap-adopt` recovery path exists: an attribution-less roadmap
  revision cannot be approved (the roadmap is optional — recreate it
  under governance instead).
- The roadmap travels in portable exports (`exchange`/`export`) since
  aperio 1.2; archives from 1.1.0rc1 that contain a plan but no roadmap
  import unchanged.
- Live cross-host workflows are unaffected: the roadmap is read by
  `roadmap-show` like any other durable artifact.

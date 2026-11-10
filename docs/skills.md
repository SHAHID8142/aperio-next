# Trust-aware portable skill packs (skill-pack v1)

A skill is reusable **guidance** — checklists, conventions, review
questions — composed into agent briefs as quoted context. Skills are
plain bounded Markdown (`SKILL.md`-compatible), never executable
content, never plugins: this solves a prompt-composition problem, and
Aperio deliberately does not grow an executable plugin system for it.

## Trust model

| State | Meaning |
| --- | --- |
| `untrusted` | every import starts here; never composed into briefs |
| `approved` | an operator explicitly reviewed it (`skill-review --approve`) |
| `revoked` | an operator explicitly withdrew it |

Hard rules, enforced by the module and tests:

- **Imported ≠ approved.** Builtin packs ship inside the package but
  enter a store as `untrusted` like anything else — installing the
  package is not operator approval.
- **Operator requirements outrank skills.** Composed content renders
  under the brief's trust boundary (quoted guidance) and never gates
  approval or completion.
- **Content changes reset trust.** Importing a different version
  replaces the pack and resets the state to `untrusted`: new content is
  a new judgment, and a previous approval never carries over. The same
  version with different content is refused (bump the version or use a
  new name; nothing is silently overwritten).
- **Conflicts are reported, never auto-resolved.** Skills may declare
  `conflicts:`; selecting two mutually-conflicting skills blocks
  composition until the operator deselects one.
- **Private material never travels.** Exports carry only the skills'
  own declared content — no memory, no project state, no machine paths
  — and only `approved` skills; untrusted and revoked are excluded.

## The SKILL.md format

```markdown
<!-- aperio:skill
name: frontend-engineering
version: 1.0.0
description: Frontend implementation guidance for components and responsive UI.
scope: frontend
depends_on: design-systems, accessibility
conflicts: 
provenance: aperio-builtin-v1
-->
Verify each acceptance criterion with a recorded check before marking
work complete. Prefer small components with explicit props over large
coupled templates. ...
```

The metadata header is an HTML comment, so the file renders as ordinary
Markdown in any SKILL.md-compatible reader. Bounds: file ≤ 64 KiB,
name/version/scope validated against fixed vocabularies, ≤ 12
dependencies and conflicts, description ≤ 300 bytes. The body is
imperative guidance to an agent — checklists and conventions, not
essays.

## Commands

```sh
aperio skill-import ~/skills/frontend-engineering --store ~/skills
#   (the store is an operator-named directory, like a memory store)
aperio skill-list ~/skills
aperio skill-show ~/skills frontend-engineering
aperio skill-review ~/skills frontend-engineering --action approve \
  --actor you --note "reviewed against the house checklist"
aperio skill-select ~/skills goal.json        # what composes for this goal
aperio agent-brief goal.json --skills-store ~/skills \
  --skill frontend-engineering                # compose into a brief
aperio skill-export ~/skills --output skills.json
aperio skill-import-export ~/skills2 skills.json   # arrives untrusted
```

## Selection rules

`skill-select` matches a skill's `scope` against the goal's profile
(scope `general` matches everywhere; `--require NAME` forces a skill
into the composition). The report lists, explicitly:

- **selected** — approved, scope-matching skills;
- **rejected** — untrusted/revoked skills and scope mismatches, each
  with its reason (an untrusted skill is never silently skipped);
- **conflicts** — pairs that declared conflicts with each other;
- **superseded versions** — when a version change replaced a pack
  (which also reset its trust state).

## Builtin packs

Eleven foundational packs ship with the package (`aperio-builtin-v1`
provenance), covering: client requirements review, UX/IA, design
systems, accessibility, frontend engineering, backend/CMS, security
review, testing and integration, release and rollback, technical SEO,
and SEO content reporting. They are starting points — review them,
adjust them, and replace them with your own conventions; their
dependencies form an acyclic graph (e.g. frontend-engineering depends
on design-systems and accessibility).

## Honest limits

- Skills are guidance with no verified performance claims: nothing
  here says a skill improves outcomes — measure that yourself with the
  benchmark/feedback machinery.
- Approval records a declared identity, not authentication.
- Skill bodies are quoted text; a malicious skill is still text an
  agent might follow — which is exactly why imports start untrusted
  and composition labels provenance.

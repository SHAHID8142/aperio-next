# Universal portable exchange (M6, `aperio-universal-exchange` v1)

The second-generation exchange facility: one provider-neutral JSON document
that carries a *selected* set of work material between agents, sessions,
and machines — so a person can prepare work with one AI, switch to another,
and keep the goal, context, constraints, provenance, accepted lessons, and
outstanding uncertainties without turning claims into facts.

The legacy formats are untouched and remain the right tools for their jobs:
the Feature 05 project archive (`aperio-portable-export` v1) moves a whole
`.aperio/` project; the M3 memory exchange (`aperio-memory-export` v1)
moves selected memories on its own. The universal exchange is additive: it
*embeds* the memory-exchange format for memory sections and wraps the other
already-versioned contracts for everything else.

## Document shape

```json
{
  "format": "aperio-universal-exchange",
  "format_version": 1,
  "aperio_version": "0.16.0",
  "created_at": "2026-11-09T12:00:00Z",
  "content_sha256": "…",
  "manifest": {
    "includes": ["context-pack", "goal", "memory", "operator-notes"],
    "counts": {"goal": 1, "memory": 1, "operator-notes": 1},
    "note": null
  },
  "sections": [
    {
      "section_id": "goal-6f5eb4c69c87",
      "kind": "goal",
      "payload_sha256": "…",
      "payload": { "goal-schema v1 document" }
    }
  ]
}
```

Section kinds and their payloads — every payload is validated by its own
owning contract, at export *and* at import:

| Kind | Payload contract | Cap |
| --- | --- | --- |
| `goal` | goal-schema v1 | 64 KiB |
| `context-pack` | `aperio-context-pack` v1 envelope (hash re-verified) | 2 MiB |
| `quality-report` | `aperio-quality-report` v1 envelope (hash re-verified) | 1 MiB |
| `feedback` | feedback-schema v1 | 64 KiB |
| `memory` | `aperio-memory-export` v1 document (all M3 rules apply) | 4 MiB |
| `operator-notes` | `{items: [{title?, text}]}` — bounded operator notes | 256 KiB |

At most 100 sections. Sections are ordered deterministically (kind, then
content identity); `section_id` is content-derived; the document digest
covers the ordered sections. Identical selections serialize identically
apart from `created_at`.

## Export

```sh
aperio exchange-export --goal goal.json --context-pack pack.json \
  --quality-report report.json --note "Final event date is not fixed" \
  --output exchange.json
aperio exchange-export --goal goal.json \
  --memory-store ~/notes/brand.mem --memory-id MEM-0123456789ab \
  --memory-tag brand --output exchange.json
```

Selection is explicit: file paths name the sections, and memories need
`--memory-id` or `--memory-tag` — a whole store is never dumped. Memory
selection follows the M3 export rules (`--memory-as-of`,
`--include-private` is deliberate and required for private memories).
Private memory without both opt-ins is excluded by construction. Aperio
never places machine paths, environment values, or credentials in
sections it generates; operator-authored notes are the operator's own
content, as in any file they write.

## Import

```sh
aperio exchange-import exchange.json --dest-dir ./work        # preview
aperio exchange-import exchange.json --dest-dir ./work --apply
aperio exchange-import exchange.json --dest-dir ./work --apply \
  --memory-store ~/notes/brand.mem --max-sensitivity internal
```

- **Preview is the default.** `--apply` writes.
- Imported files are named `imported-<kind>-<digest>.json` — visibly
  separate from operator-authored artifacts. Existing files are never
  overwritten: an identical file (same content identity) is reported as
  `already-present`, a different-content occupant of the digest-based name
  is a refused conflict the operator resolves.
- **Memory sections enter the destination store as `proposed`.** The
  embedded `aperio-memory-export` payload goes through the ordinary M3
  import: duplicates skipped and reported, subject conflicts reported,
  claimed states preserved only as `declared_state`, unsupported trust
  claims listed under `unsupported_trust_claims`, review evidence carried
  under `transfer_evidence`. Without `--memory-store`, memory sections are
  skipped with an explicit reason.
- **Imported goals are untrusted input.** The report warns on every goal
  section: review it and re-declare provenance before treating it as
  operator instructions. (The goal document's own authority model still
  applies when it is later packed or checked.)
- Nothing executes, nothing is evaluated, nothing is promoted, and no
  operator file is modified: the exchange's own artifacts are the only
  writes.

## What the receiving agent sees

An exchange gives the next agent, in one readable document: the validated
goal (objective, constraints, requirements, uncertainties), the assembled
context pack (provenance/trust labels included), the latest quality report
(outstanding issues with evidence), feedback and accepted lessons (with
their review records), relevant reviewed memories (with their actual trust
and history), and the operator's notes about what is still uncertain.

## Honesty limits

- A hash proves the bytes were not altered; it does not authenticate the
  sender.
- `imported-*` filenames and warnings do not sanitize content — a receiving
  agent must still treat exchanged text as data, not instructions.
- Memories keep the M3 limits: chain hashes detect corruption, not
  authorship; an imported claim is proposed, and only a recorded review on
  the receiving side raises it.
- A newer `format_version` or an unknown section kind is refused with an
  actionable message, never partially interpreted.

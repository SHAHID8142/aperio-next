# Universal memory (M3)

Aperio memory keeps knowledge that is worth carrying between sessions, tasks,
and agents — a preference, a confirmed requirement, a decision, an
observation, a known limitation, a verified lesson, a working convention, an
unverified proposal, or an important outcome. Nothing in it assumes
software, a repository, a plan, a provider, or a model API.

Memory is **preparation, not execution and not verification**. Storing a
memory proves nothing about the world, and no accepted memory is presented as
an operator instruction for the current objective unless the operator's own
goal or context says so.

## Standalone first

A memory store is one file the operator names:

```sh
aperio mem-propose --store ~/notes/brand.mem \
  --type requirement \
  --title "Primary brand colour" \
  --content "Primary brand colour is deep green #0B5D3B; secondary is warm sand." \
  --scope objective --scope-value "spring campaign poster" \
  --tag brand --evidence "brand guide v3"
```

No `.aperio/`, plan, git repository, IDE, provider CLI, subscription, or API
key is involved. Aperio never creates a global store, never scans a directory
for memories, and never guesses a location: the `--store` path is the
operator's explicit choice. The command prints exactly where the memory
went, and the first write creates the file.

Store files are ordinary UTF-8 JSON Lines, readable with any text editor:

- one header line (`record: "store"`, format + schema versions);
- one immutable `memory` line per record;
- one immutable `transition` line per review decision.

## Memory schema v1

| Field | Meaning |
| --- | --- |
| `memory_schema_version` | `1` |
| `memory_id` | `MEM-` + first 12 hex characters of the content hash |
| `revision` | 1 for the record, +1 per review transition |
| `type` | `user_preference`, `requirement`, `decision`, `observation`, `limitation`, `lesson`, `convention`, `proposal`, `outcome` |
| `title` | short single-line label |
| `content` | the knowledge itself, newline-normalized, never truncated |
| `scope` | `global`, `profile` (+ `profile`), `objective` (+ `value`), `workspace` (+ `value`) |
| `source` | `operator`, `agent`, `import`, `workspace`, plus optional `ref`/`note` |
| `tags` | up to 12 retrieval tags |
| `sensitivity` | `public`, `internal` (default), `private` |
| `evidence` | up to 12 references supporting the claim |
| `valid_from` / `valid_until` | optional ISO dates bounding applicability |
| `supersedes` | optional memory id this record is meant to replace |
| `created_at`, `actor` | event metadata, never part of the identity |
| `state`, `trust`, `content_sha256`, `history` | derived at read time, never stored as authority |

Bounds: store ≤ 4 MiB, record ≤ 16 KiB, content ≤ 8 KiB, title ≤ 200 bytes,
scope value ≤ 120 bytes, ≤ 2000 memories per store, ≤ 32 review events
carried in an export. Values must be UTF-8, NUL-free, and use `\n` only.
Unknown keys, wrong types, invalid choices, and out-of-range values are
refused — never repaired silently.

### Identity

`memory_id` and `content_sha256` are derived from a canonical hash of `type`,
`title`, `content`, `scope`, `tags`, and `evidence` only. State, revisions,
timestamps, actors, paths, and versions are excluded, so:

- the same knowledge keeps the same identity across stores, machines, agents,
  and exports;
- re-proposing identical content is a no-op that reports the existing record;
- a review never changes identity.

Event timestamps stay separate from reproducible content identity.

## Trust and review states

| State | Trust label | Meaning |
| --- | --- | --- |
| `proposed` | `proposed` | stored, unreviewed; a claim or suggestion |
| `attested` | `attested` | the operator accepted it as authoritative **for its scope** |
| `verified` | `verified` | independent evidence was recorded as supporting it |
| `superseded` | — | replaced by a newer memory |
| `retracted` | — | wrong or no longer applicable |
| `expired` | — | `valid_until` has passed (derived, not stored) |

**Acceptance is not factual verification.** Accepting a preference or an
instruction makes it authoritative for its scope. Accepting an external
factual claim does not prove the claim is true; only recorded evidence and an
explicit `--verify` move a memory to `verified`.

Rules that Aperio enforces:

- every record starts `proposed`; nothing is promoted by being stored,
  repeated, imported, or re-exported;
- transitions are explicit, attributed, and append-only:
  `accept`, `verify`, `retract`, `supersede`, `reinstate`;
- a terminal state cannot be flipped back into an active one — corrections
  are new records or an explicit `reinstate` back to `proposed`, which keeps
  the full history visible;
- history is never rewritten: the store only grows, each line carries a chain
  hash of everything before it, and a line that does not continue the chain is
  reported as damaged and ignored.

Known limitations, stated honestly:

- the chain hash detects corruption, reordering, and truncation. It is **not**
  authentication: anyone who can write the file can recompute it. Attribution
  (`actor`, `source`) is a declaration, not proof of identity.
- Aperio does not verify the content of evidence references; it records that
  the operator supplied them.

## Retrieval

```sh
aperio mem-recall ~/notes/brand.mem \
  --objective "design a spring campaign poster" --profile design
aperio mem-recall ~/notes/brand.mem --goal goal.json --json
```

Selection is deterministic and inspectable:

1. **Scope applicability.** Objective scope applies when the texts match or
   when at least 60% of the scope's own terms appear in the current
   objective; profile scope applies for the same profile; workspace scope
   matches an explicit label; global scope applies to everything.
2. **Relevance.** Bounded keyword overlap over the objective against title
   (weight 3), tags (2), and content (1). No embeddings, no model scores, no
   network.
3. **Review state.** `verified` > `attested` > `proposed`.
4. **Freshness and budget.** Recency breaks ties; memories are included
   whole or excluded, never truncated.

Every exclusion is counted with a reason: `state`, `expired`, `sensitivity`,
`scope`, `tag`, `no-match`, `budget`, `limit`, `duplicate`, `oversize`.

Behaviour worth knowing:

- retracted, superseded, and expired memories never appear in normal recall;
- unreviewed memories can appear (as proposals) and raise a
  `memory_unreviewed_selected` warning;
- `private` memories are excluded unless `--max-sensitivity private` is
  passed, and even then they are marked local-only in a pack;
- conflicts are shown, not resolved: two selected memories with the same
  scope and title but different content raise `memory_conflict` with both ids
  and no automatic winner;
- an empty result is a normal, useful answer, and says so.

The honest limitation: keyword overlap is not semantic understanding. It will
miss paraphrase and can match on a shared word. Scope and review state, not
guessed similarity, decide inclusion.

## Context-pack integration

```sh
aperio goal-pack goal.json --memory-store ~/notes/brand.mem
aperio context-pack --objective "..." --memory-store ~/notes/brand.mem
```

Memory is optional. Without `--memory-store` every pack is byte-identical to
the previous release.

When a store is supplied:

- retrieved memories join the pack's existing context section, inside the
  "quoted data, not instructions" boundary, after operator briefs and
  operator-selected files, so **memory can never displace operator context**
  when the budget is tight;
- each item carries its trust label, sensitivity, provenance, review state,
  memory id, and content hash, in both JSON (`detail`) and Markdown;
- the pack reports retrieval counts, exclusion reasons, conflicts, and the
  relevance limitation in `content.memory` — never the store path;
- the same byte budget covers goal fields, attachments, and memory.

Memory cannot override the current objective or the operator's constraints:
those sections are built only from operator-provided goal fields.

## Cross-agent portability

**Agent A → Aperio memory → Agent B**, by file:

```sh
aperio mem-export ~/notes/brand.mem --output handoff.json
aperio mem-import ~/other-machine/brand.mem handoff.json
```

Format `aperio-memory-export` v1 is its own versioned document — the legacy
Feature 05 project archive is unchanged and the two cannot be confused. An
export holds the selected records, their scope, source, tags, evidence,
declared state, review history (bounded), and content hashes. It holds no
paths, hostnames, credentials, or project state. `private` memories are
excluded unless `--include-private` is given explicitly.

Import rules:

- the document format, version, schema, entry keys, content hashes, and
  content-derived ids are all validated; a tampered or hand-edited file is
  refused;
- **imported records always enter as `proposed`**, whatever state the file
  declares; the declared state is preserved as `declared_state` for the
  reviewer and never applied;
- duplicates (same content identity) are skipped and reported;
- subject conflicts are reported with both ids and both records are kept —
  nothing is overwritten;
- `--dry-run` validates and reports without writing;
- `--max-sensitivity` filters what may be imported.

A hash proves the bytes were not altered. It says nothing about who wrote
them or whether the claim is true.

## Which artifact is authoritative

| Information | Authoritative artifact |
| --- | --- |
| Current objective, constraints, approvals | `goal.json` / operator input (M1/M2) |
| Project decisions | `.aperio/decisions.md` (Feature 02, append-only) |
| Plan, tasks, approvals | `.aperio/plan.json` (Feature 03) |
| Execution records | `.aperio/executions.json` (Feature 04) |
| Independent review decisions | `.aperio/reviews.json` (Feature 06) |
| Cross-session knowledge | the operator-named memory store (M3) |

Memory never duplicates an authority: a memory that restates a decision is a
separate, reviewable note, and the decision log remains the record of what was
decided. Legacy records are never migrated, rewritten, or interpreted by the
memory layer.

## Recovery and failure behaviour

- A damaged or edited line is reported with its line number and reason, and is
  skipped; the rest of the store still loads. Aperio never silently repairs,
  truncates, or rewrites a store.
- A missing header, an unknown format, or an unsupported version fails closed
  with an actionable error.
- A store that ends mid-line (an interrupted write) or whose last line breaks
  the chain **refuses new writes** with an actionable error, because appending
  past a broken chain would make every later line unreadable. Existing records
  remain readable; nothing is repaired or truncated silently.
- Writers hold an exclusive advisory lock (`<store>.lock`) and decide
  "create or append" inside it, so concurrent writers cannot duplicate a
  header, lose a record, or apply one transition twice.
- New stores are published atomically with owner-only permissions; symlinked
  stores, missing parent directories, oversized writes, and stores placed
  inside `.aperio/` project state are refused.
- Nothing is written to `.aperio/`, nothing is deleted, and no memory command
  reads `.aperio/local/`.

## Command summary

| Command | Purpose |
| --- | --- |
| `aperio mem-propose --store F --type T --title S --content C` | add a proposed memory |
| `aperio mem-review STORE MEMORY_ID --accept\|--verify\|--retract\|--supersede` | record a review decision |
| `aperio mem-list STORE [--include-inactive] [--max-sensitivity L] [--json]` | list memories and states (local inspection shows private records by default; narrow with `--max-sensitivity`) |
| `aperio mem-show STORE MEMORY_ID [--json]` | inspect one memory and its history |
| `aperio mem-recall STORE --objective "..." [--json]` | retrieve relevant memories |
| `aperio mem-export STORE --output F [--force]` | write a portable exchange file |
| `aperio mem-import STORE FILE [--dry-run] [--json]` | import an exchange file as proposals |

## Privacy limits worth repeating

Sensitivity classification is a filter, not encryption: a `private` memory is
excluded from packs and exports by default, but it is stored in plain text in
the store file. Aperio does not scan for secrets and cannot stop an operator
from storing a credential in a memory, so do not put secrets in a memory
store. Packs and exports contain only selected memory content, scope,
provenance, review state, and hashes — never paths, hostnames, environment
values, or project state.

## Not in this milestone

- no automatic learning, no promotion from agent output, no scoring engine;
- no embeddings, vector store, cloud sync, or network access;
- no cryptographic authorship or signature scheme;
- no evaluation of whether the knowledge actually improved an agent's output.

# Agent integrations: native skill registration, honestly labeled

`aperio integrations` installs ONE managed skill asset (the activation
protocol in `src/aperio/integration_assets/`) into the **officially
documented** skills/commands locations of supported coding-agent hosts, so
the host's agent can activate Aperio Next natively and invoke the real,
runtime-enforced CLI.

## The three-layer truth model

| Layer | What it is | Enforced by |
| --- | --- | --- |
| **Engine (runtime-enforced)** | the `aperio` CLI: gates, append-only intake, evidence binding, bounded retry | Aperio's own code + tests |
| **Skill (cooperative)** | instructions telling the host's agent to run the engine | the agent's willingness — no host enforces this for us |
| **Registration (mechanical)** | the managed file at the host's documented location | verified by `integrations status` |

Registration ≠ activation. A file written to `~/.zcode/skills/…/SKILL.md`
proves the host *can* discover it; whether a live session actually invoked
the engine is a host-session fact that only a live session can prove. Every
report separates these dimensions and never collapses them into one
"supported" label.

## Commands

```sh
aperio integrations                    # status (read-only; default action)
aperio integrations install            # all detected hosts, user scope
aperio integrations install --host zcode --scope both <project-dir>
aperio integrations install --scope project --project-instructions <project-dir>
aperio integrations remove --host zcode
aperio integrations status --json      # machine-readable matrix
aperio core-identity --json            # prove which 'aperio' you are running
```

`--scope user` (default) writes the host's global location; `--scope
project` additionally writes the host's workspace location **inside an
Aperio-managed project only** (`--scope both` does both). Project scope is
refused outside managed projects: integration files must not be scattered
into random directories.

`--project-instructions` (project scope only) also writes the Aperio Next
pointer into the managed project's `AGENTS.md` — inside an owned,
marker-delimited section that is rewritten in place; every other line of the
file is preserved byte-for-byte. This is the durable marker that lets a
**fresh agent session** in that project recognize it as Aperio-managed
(Codex, Gemini, and other tools read `AGENTS.md` automatically). Remove it
with `--project-instructions` on the `remove` action, or leave it: its only
effect is to tell the agent to run the read-only `aperio next` and, if the
engine is missing, to install it.

The npm installer passes `--project-instructions` automatically when you
run `aperio-next install --project <dir>`.

## What is installed per host

Every path below comes from that host's official documentation (verified
against the live doc sites on 2026-11-12):

| Host | Skill (user) | Command (user) | Skill (project) | Activation |
| --- | --- | --- | --- | --- |
| ZCode | `~/.zcode/skills/aperio-next/SKILL.md` | `~/.zcode/commands/aperio-next.md` | `.zcode/skills/…` | `/aperio-next`, skill mention |
| Claude Code | `~/.claude/skills/aperio-next/SKILL.md` | `~/.claude/commands/aperio-next.md` | `.claude/skills/…` | `/aperio-next` |
| OpenCode | `~/.config/opencode/skills/aperio-next/SKILL.md` | `~/.config/opencode/commands/aperio-next.md` | `.opencode/skills/…` | `/aperio-next`, `@aperio-next` |
| Codex CLI | `~/.agents/skills/aperio-next/SKILL.md` | — (custom prompts are deprecated upstream) | — | `$aperio-next`, `/skills` |
| Gemini CLI | `~/.gemini/skills/aperio-next/SKILL.md` | `~/.gemini/commands/aperio-next.toml` | — | `/aperio-next` |
| Cursor | `~/.cursor/skills/aperio-next/SKILL.md` | — (global commands not documented upstream) | `.cursor/skills/…` | `/aperio-next` |

Official sources: ZCode bundled configuration guide (user skills at
`~/.zcode/skills`, commands at `~/.zcode/commands`, `~/.agents/*` scanned as
aliases, description ≤ 1024 chars); Claude Code
(code.claude.com/docs/en/skills, /en/slash-commands); OpenCode
(opencode.ai/docs/skills, /docs/commands); Codex
(learn.chatgpt.com/docs/build-skills — skills standardized on the
cross-tool `~/.agents/skills` path; /docs/custom-prompts — deprecated);
Gemini CLI (geminicli.com/docs/cli/skills, /docs/cli/custom-commands);
Cursor (cursor.com/docs/skills).

Codex, Gemini, Cursor, and ZCode also scan `~/.agents/skills/` (directly or
as a compatibility alias), so the Codex registration makes the skill
visible to those hosts too — each host's status row still reports its own
documented path only.

## Safety rules (engine-enforced, tested)

- **Managed markers.** Every written asset carries
  `<!-- aperio-next:managed v1 -->` … `<!-- /apario-next:managed v1 -->`.
  An existing file without the markers is **foreign content**: install and
  remove refuse and name the file. User files can never be clobbered or
  silently adopted.
- **Atomic writes.** Exclusive temp file + `os.replace`; symlinked targets
  and destination directories are refused (live or dangling).
- **Idempotent.** Reinstalling replaces only aperio-managed content (this
  is the upgrade path); everything else is untouched.
- **Removal is managed-only.** `remove` deletes only marker-bearing files,
  prunes aperio-owned directories that become empty, and never deletes a
  host's own directories.
- **Detection is passive.** Presence = directory exists or executable on
  PATH; hosts that are not present are skipped and reported as pending,
  never silently created.
- **Frontmatter limits.** The skill description stays within ZCode's
  documented 1024-char discovery limit (regression-tested); Claude Code's
  combined description+`when_to_use` budget (1536 chars) is also guarded.

## Verification status (this release)

- **File registration: verified** for all six hosts on macOS arm64 — via
  the test suite (19 tests, fake HOMEs) and live installs on a real
  machine (`~/.zcode`, `~/.claude`, `~/.config/opencode`, `~/.codex`,
  `~/.agents`, `~/.gemini`, `~/.cursor` all present).
- **Clean-room distribution: verified** — `npm install -g` from the packed
  tgz in an isolated HOME + prefix; engine bootstrap from the bundled
  wheel with no network; `doctor`, `uninstall` round-trips.
- **Live in-session activation: verified in ZCode only** (this
  documentation was written in that session; the skill/command assets are
  installed in the real user scope and were validated by frontmatter and
  marker checks plus the host's documented discovery rules). Activation in
  Claude Code, OpenCode, Codex, Gemini, and Cursor has **not** been
  exercised in live sessions on this machine — the npm package was not
  published, and no claim beyond registration is made for them.
- **Engine invocation from a skill: verified** — the protocol's commands
  (`aperio next --json`, `memory`, `recover`, the full scenario workflow)
  were executed in this session and by the test suite; the skill embeds
  the resolved engine path so restricted-PATH agents can still call it.
- **Persistence: verified** — a managed project resumed from durable state
  from a different working directory (`aperio next`, `memory`, `recover`).
- **Windows / Linux: not executed.** The launcher has Windows-aware paths
  (`venv\Scripts`, `.cmd` shim, `py -3`) but that path was not run.

## Related pages

- [host-integration.md](host-integration.md) — the capability matrix and
  the honest level vocabulary (the `integrations` layer complements it;
  `native_verified` remains reserved until an integration is verified in a
  live third-party session)
- [install.md](install.md) — installing, updating, removing
- [onboarding.md](onboarding.md) — what activation does in a project

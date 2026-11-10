# Host integration: capability matrix and honest limits

Which host families Aperio works in, what protection each one actually
achieves, and what is — explicitly — not claimed. Every statement on this
page is about what **Aperio itself** does: a stdlib-only Python CLI and
library that you invoke. Nothing here describes, assumes, or depends on
what any host does internally, because Aperio never observes hosts
internally.

Per the locked design decisions ([master-plan-v2.md](master-plan-v2.md),
decision 7), host-integration claims are limited to this truthful
capability matrix. The CLI remains the primary interface.

## The three protection levels (recap)

[brain-dump.md](brain-dump.md) defines these levels; they are **not
equivalent**, and this matrix reports which level each host family can
reach — not which one sounds better.

| Level | Guarantee |
| --- | --- |
| **Runtime-enforced** | Aperio code enforces it: append-only, chain-hashed intake log; nothing captured executes; verified by tests |
| **Agent-instruction-based (cooperative)** | depends on the agent honoring instructions; no runtime enforcement |
| **Manual fallback** | preservation only; the operator captures later |

## Capability matrix

| Host family | How Aperio runs there | Brain-dump protection achievable | Execution path | Notes |
| --- | --- | --- | --- | --- |
| **Terminal/CLI on any OS** (macOS, Linux, Windows) | Aperio *is* the program: you invoke `aperio` or import the library | **Runtime-enforced**: `aperio capture` writes the intake log; `aperio inbox` is a read-only replay | `manual` adapter (writes a portable run-handoff for any agent) or a configured `local-process`/provider CLI launched with `--authorize-exec` | Primary supported path. Everything this repository's test suite verifies runs here. |
| **Any text-chat agent** (ChatGPT, Claude web, Gemini, GLM, …) via copy/paste | It does not run there. Aperio runs in your terminal; pasting is the transport | **Cooperative only** — the Brain Dump Guard protocol as pasted instructions; manual fallback inside the host (write thoughts to a file) and capture them later from a terminal | None inside the host. Aperio evaluates saved answers from the terminal (`quality-check`); provider CLIs are the separate row below, not the chat host | Aperio cannot intercept these hosts' chats. Cooperation is exactly as strong as the agent's willingness to follow instructions. See [agent-workflows.md](agent-workflows.md). |
| **Coding-agent CLIs driven by Aperio** (`codex-cli`, `claude-cli`) | Aperio launches the installed CLI as a supervised local subprocess on its documented non-interactive interface (`codex exec --json`, `claude -p`) | Runtime-enforced for capture (you run it in the terminal); the launched provider is an instruction-constrained agent supervised mechanically — not an enforced one | `run-start` with two consents; capability probe (`--version`/`--help`) before any authorized launch; read-only by default | Aperio composes the prompt from the approved task and reads bounded structured metadata. It does not control the host's interactive chat. See [providers.md](providers.md), [feature-07.md](feature-07.md). |
| **ZCode Desktop and similar GUI agent hosts** | It does not run there. Aperio's own CLI runs alongside, in a terminal; the host's chat is never intercepted or controlled | Cooperative at best — the protocol may be installed as instructions/skills for the host's agent; manual fallback otherwise. Runtime enforcement applies only when *you* run `aperio capture` yourself | None inside the host. Aperio's own execution paths (manual adapter, provider CLIs) remain available from the CLI | No undocumented or invented host APIs are used or claimed. Host-native sub-agents, if a host offers them, are outside Aperio's enforcement boundary and are not claimed as integration. |

No other host families are claimed; this matrix is the entire set of
host-integration claims Aperio makes.

## Terminal/CLI — the primary supported path

In a terminal, Aperio's guarantees are its own code. Capture never
executes anything, the inbox is append-only history, and dispatch needs
your explicit flags:

```sh
aperio capture ./project --message "mid-task thoughts, preserved verbatim"
aperio inbox ./project                       # review routing
aperio inbox-resolve ./project INT-0001 0 --action acknowledge --actor you

aperio run-start ./project TASK-001 --agent manual          # launches nothing
aperio run-start ./project TASK-001 --agent codex-cli \
  --config .aperio/local/codex.json --authorize-exec        # read-only by default
```

Without `--authorize-exec`, provider and `local-process` dispatch is
read-only preparation: no record, no task claim, no process.

## Text-chat agents — cooperative by definition

For ChatGPT, Claude web, Gemini, GLM, or any agent that reads prose,
Aperio has no presence in the host. The briefs and boundary statements
travel as self-contained text; the receiving agent decides what it does
with them. Captures performed *in* the chat are cooperative at best, and
capture itself falls back to you: write the thoughts to a file, then run
`aperio capture` from a terminal when you are back at one.

The same-chat instruction protocol is defined once, in full, in
[brain-dump.md](brain-dump.md) ("Same-chat instruction protocol"); that
page also notes the built-in `brain-dump-guard` skill pack, which carries
the same protocol. It is **instruction-based cooperation**: honest,
useful, and weaker than the runtime path by definition. This page
deliberately does not repeat the protocol text; there is exactly one
source of truth for it.

## Coding-agent CLIs driven by Aperio — supervised, not interactive

`codex-cli` and `claude-cli` are the only provider adapters. Aperio
drives them only through their documented non-interactive interfaces,
probes the flags it relies on before any launch, and treats the provider
as an instruction-constrained agent: the prompt tells it not to alter
`.aperio/` control state, but a shell-capable agent can attempt to do so
— governance checks and independent review remain the trust boundary
([feature-07.md](feature-07.md), "Honest limitations").

Aperio composes the prompt from the approved task; it does not connect to
the host's interactive chat, cannot steer an interactive session, and
makes no claim about any behavior the provider CLI exhibits outside the
launched run.

## GUI agent hosts (ZCode Desktop and similar) — no interception, no invented APIs

ZCode Desktop, Cursor, and similar GUI agent hosts are **not
integrated**. Aperio does not intercept or control the host's chat, and
it does not use or claim any undocumented or invented host API. What is
possible, and only that:

- The cooperative protocol (the Brain Dump Guard text) can be installed
  as instructions or skills for the host's own agent — by you, through
  the host's own documented configuration. That is cooperation, not
  enforcement.
- Host-native sub-agents, if a host offers them, run inside the host's
  own machinery. They are outside Aperio's enforcement boundary and are
  not claimed as integration.
- Aperio's own runtime (the CLI) still works alongside: capture, inbox,
  review, dispatch, and export/import all function independently of the
  host.

## What is not supported / not claimed

- **No native chat interception anywhere.** No component reads, watches,
  injects into, or controls any third-party chat — web or desktop, in any
  host family. Captures enter Aperio only through `aperio capture` or the
  manual fallback.
- **No hidden system-prompt assumptions.** Aperio never assumes anything
  about an agent's hidden system prompt, internal memory, or tool
  permissions. Material exchanged with agents is self-contained text that
  states its own limits.
- **No provider lock-in.** The universal layer works by copy/paste with
  no provider installed. Adapters are optional and provider-neutral;
  adding one is one documented adapter, not a core change.
- **No invented GUI/IDE APIs.** No undocumented endpoints, extension
  points, or automation interfaces for GUI hosts are used, assumed, or
  claimed. Where Aperio touches a provider CLI at all, it is that CLI's
  documented interface — and GUI hosts are not touched at all.
- **No background monitoring services.** Aperio does nothing between
  invocations: no daemon, no polling, no listening, no jobs of its own.
  It runs when you run it.

## How to choose

| If you want | Use |
| --- | --- |
| Runtime-enforced capture and a durable inbox | the CLI capture path: `aperio capture ./project --message "…"` |
| Convenience while chatting with a web/chat agent | the cooperative protocol, plus periodic capture from a file you keep there: `aperio capture ./project --file thoughts.txt` |
| A locked-down host where you cannot run Aperio | manual fallback: write thoughts to any file; run `aperio capture ./project --file that-file` later, from a machine where the CLI is installed |
| Aperio to drive an installed coding CLI | the provider adapters with the two consents ([providers.md](providers.md)) |

The first row is the only one with runtime enforcement. The others are
cooperation or preservation, and are labeled as such everywhere Aperio
mentions them.

## Consents per host family

`--authorize-exec` and `--authorize-write` are two independent, explicit
operator consents, given at dispatch time on the CLI invocation:

| Consent | Meaning |
| --- | --- |
| `--authorize-exec` | Launch a real subprocess at all (`local-process`, `codex-cli`, `claude-cli`). Without it, dispatch is read-only preparation. |
| `--authorize-write` (alias `--authorize-workspace-write`) | Allow provider workspace-write or shell-capable modes. Providers default to read-only; a write request without this consent fails before any run record or task claim. |

Where they apply, per host family:

| Host family | `--authorize-exec` | `--authorize-write` |
| --- | --- | --- |
| Terminal/CLI | Required to launch `local-process` or a provider CLI; the `manual` adapter never launches anything, so it needs neither | Required only for write-capable modes |
| Text-chat agents | n/a — Aperio launches nothing in these hosts | n/a |
| Coding-agent CLIs driven by Aperio | Required (`run-start … --authorize-exec`) | Required for `workspace-write` / `acceptEdits`; `Bash` additionally requires an explicit `allow_shell` |
| GUI agent hosts | n/a inside the host; applies only to Aperio's own dispatch from the CLI | Same |

Governance is separate and never a substitute: a policy approval
(Feature 06) is a governance decision and **never substitutes for**
`--authorize-exec` or `--authorize-write`. Both flags are human decisions
made per dispatch; actor identifiers are declarations, not
authentication ([security-model.md](security-model.md)).

## Verification status

- **Verified locally** (this repository's test suite): the capture/inbox
  runtime path; the adapter contract with test doubles (consents,
  capability probes, argv/stdin handling, timeouts, privacy boundaries);
  manual-workflow commands run end to end. Paste steps are human
  procedures by nature — described, not simulated.
- **Not verified**: end-to-end execution against the real Codex or Claude
  services (no credentials are used in this project's verification), and
  every host-internal behavior. Aperio makes no claims about what a host
  might do internally, because it never observes it.

## Related pages

- [brain-dump.md](brain-dump.md) — the Brain Dump Guard and the cooperative protocol
- [providers.md](providers.md) — provider adapter support, safety, verified limits
- [feature-07.md](feature-07.md) — adapter mechanics and security acceptance matrix
- [agent-workflows.md](agent-workflows.md) — copy/paste workflows for any text agent
- [security-model.md](security-model.md) — authority, trust, and what Aperio does not claim

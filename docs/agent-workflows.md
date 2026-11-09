# Manual agent workflows (M7)

How to use Aperio with **any** text agent — ChatGPT, Claude, Gemini, GLM,
or anything else that reads prose — through ordinary copy/paste. No API,
no key, no plugin, no provider CLI required. The provider-CLI integrations
(codex-cli, claude-cli) are a separate, optional path documented in
[providers.md](providers.md) and [feature-07.md](feature-07.md).

Every Aperio-side command on this page is executed by the test suite
(`tests/test_guidance.py::test_manual_workflow_commands_run_end_to_end`),
so the documented procedure is the tested one. The paste steps are human
procedures by nature; they are described, not simulated.

Aperio never hardcodes assumptions about any agent's hidden system prompt
or internal memory: the brief is self-contained text, the boundary
statements travel inside it, and the receiving agent decides what it does
with them — which is exactly why the brief states its own limits.

## The core loop (all agents, all domains)

```text
1. Prepare      aperio goal-check goal.json
                aperio preflight goal.json
2. Brief        aperio agent-brief goal.json --output brief.md
3. Paste        paste brief.md into your agent; it answers in the chat
4. Save         save the answer to answer.md
5. Evaluate     aperio quality-check goal.json --deliverable answer.md
6. Feedback     write feedback.json; review it; store the lesson
7. Reuse        next brief carries the accepted lesson (--memory-store)
```

One-time setup: none. A goal file and the CLI are enough; a project is
never required for one-off tasks.

## Worked example — design (poster brief)

```sh
aperio goal-check poster-goal.json          # validate the assignment
aperio preflight poster-goal.json           # what could go wrong
aperio agent-brief poster-goal.json --output brief.md
# paste brief.md into ChatGPT/Claude/Gemini/GLM; save the answer:
aperio quality-check poster-goal.json --deliverable answer.md \
  --require-section "Print specs"
```

The quality report names confirmed gaps (a missing required section, a
"must include" constraint that never appeared), warnings, and — honestly —
what only a human can judge.

## Worked example — research (cited evidence summary)

```sh
aperio preflight research-goal.json --memory-store ~/notes/brand.mem
aperio agent-brief research-goal.json \
  --memory-store ~/notes/brand.mem --output brief.md
# the brief quotes reviewed knowledge with its trust labels and lists
# unverified proposals separately
# ... paste, answer, save ...
aperio quality-check research-goal.json --deliverable answer.md
```

With an evidence requirement in the goal, the report flags missing
attribution, absolute claims, and — when sources *are* cited — states
plainly that whether the sources support the claims is **unknown** to the
tool.

## Worked example — business / writing / general

The same shape. Business goals carry assumptions and risks (preflight
reports them as recommendations to validate); writing goals carry
audience and length bounds (quality-check enforces stated word bounds);
a general question needs only objective + intended outcome, and a one-off
task needs no work decomposition.

## Cross-agent handoff

To move the work to a different agent or machine:

```sh
aperio exchange-export --goal goal.json --context-pack pack.json \
  --quality-report report.json --note "Date not fixed" --output x.json
aperio exchange-import x.json --dest-dir ./next-agent   # preview, then --apply
```

See [exchange.md](exchange.md). The receiving agent gets the goal,
context, provenance, lessons, and open uncertainties in one document.

## Choosing between the paths

| Need | Path |
| --- | --- |
| Paste into a web/chat agent | `agent-brief` (this page) — always works |
| Move work between machines/agents | `exchange-export` / `exchange-import` |
| Non-interactive provider CLI on this machine | provider adapters (providers.md), optional |
| Whole `.aperio/` project on another machine | Feature 05 `export`/`import` archive |

## What Aperio does not do in these workflows

- It does not contact any provider; pasting is the transport.
- It cannot control or sandbox the receiving agent; the brief says so.
- It does not judge the answer's truth or taste; quality-check names only
  what is objectively checkable and labels the rest as warnings,
  unknowns, or recommendations.
- It does not promote the agent's claims: lessons enter memory as
  proposals and stay there until a recorded review.

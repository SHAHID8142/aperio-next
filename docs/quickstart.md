# Quick start

This page has two tracks. **Track A** is a new project with no code and no API
key. **Track B** is a task you hand to any AI agent through a portable brief.

For how to install Aperio on your operating system, see [install.md](install.md).

## Track A — start a project (about five minutes)

The easiest path is the guided wizard. It shows one question at a time, lets
you pause and resume, and asks before it creates anything.

```sh
mkdir my-project
cd my-project
aperio next .        # read-only: what to do next for this folder
aperio start .       # opt-in interactive wizard (needs a real terminal)
```

The wizard first explains that it will create the `.aperio/` folder and asks for
your approval. During the interview:

- type an answer in your own words;
- type `Not sure yet` or `Decide later` when you do not know;
- type `/pause` to save and stop, `/quit` to cancel, `/back` to revise where offered.

Run `aperio start .` again later to resume.

### Scripted or agent-driven form

`aperio next` stays a strictly read-only protocol, so scripts and agents can
drive onboarding without a terminal:

```sh
aperio next .                                            # one next action, JSON with --json
aperio onboarding-confirm-init . --approve               # the only first mutation
aperio onboarding-answer . project.category --answer "Website"
aperio onboarding-answer . foundation.goal --answer "Build a portfolio site"
aperio onboarding-readiness .                            # are the required answers in?
```

Onboarding gathers information and checks readiness. It does **not** build or
deploy anything; those are separate, explicitly authorized steps.

## Track B — hand a task to any agent

Aperio does not need a project for this part. Write down the assignment, check
it, then produce a paste-ready brief for whichever agent you use.

**1. Write down the assignment** — save as `poster-goal.json`:

```json
{
  "objective": "Design a spring campaign poster for the neighbourhood garden open day",
  "intended_outcome": "A printable A3 poster the committee can send to the print shop",
  "profile": "design",
  "constraints": ["Must include the garden logo", "Accessible contrast"],
  "deliverables": ["A3 poster layout"],
  "uncertainties": ["Final event date is not fixed"]
}
```

**2. Check it before you start** — what could go wrong?

```sh
aperio goal-check poster-goal.json
aperio preflight poster-goal.json
```

**3. Get the paste-ready brief** and paste it into ChatGPT, Claude,
Gemini, GLM — whichever agent you use:

```sh
aperio agent-brief poster-goal.json --output brief.md
```

**4. Save the agent's answer** as `answer.md`, then check it against what
you actually asked for:

```sh
aperio quality-check poster-goal.json --deliverable answer.md
```

Every finding is labelled: **confirmed** (a rule proved it — fix it),
**warning** (a signal — judge it), **unknown** (the tool says it cannot
decide — you decide), **recommendation** (advice).

**5. Keep the lesson.** When you notice something worth remembering:

```sh
aperio feedback-check feedback.json          # describe the problem + lesson
aperio feedback-review feedback.json --decision accepted \
  --actor you --output reviewed.json         # you decide, explicitly
aperio feedback-lesson reviewed.json --store ~/notes/lessons.mem --actor you
aperio mem-review ~/notes/lessons.mem MEM-… --accept   # you promote it
```

The next brief (`--memory-store ~/notes/lessons.mem`) carries the accepted
lesson automatically.

**6. Switch agents or machines** without losing the thread:

```sh
aperio exchange-export --goal poster-goal.json --output exchange.json
# on the other machine:
aperio exchange-import exchange.json --dest-dir ./work   # preview first
```

## Where to go next

| Want | Read |
| --- | --- |
| The full goal format | [goal-contract.md](goal-contract.md) |
| Memory, trust, and what `verified` really means | [universal-memory.md](universal-memory.md) |
| All quality rules and their limits | [quality.md](quality.md) |
| Feedback and improvement | [feedback.md](feedback.md) |
| Moving work between agents | [exchange.md](exchange.md) |
| What Aperio does NOT promise | [security-model.md](security-model.md) |
| Every command | [cli-reference.md](cli-reference.md) |

If something goes wrong, see [troubleshooting.md](troubleshooting.md).

# Quick start (no project, no code, no API key)

Aperio helps you prepare work for any AI agent, check the result, remember
what you learned, and carry everything to the next agent — through
ordinary files and copy/paste.

Install (development), or see [install.md](install.md) for the full
fresh-machine path (Windows/macOS/Linux):

```sh
python -m pip install -e ".[dev]"     # or: pip install <the built wheel>
aperio --version
```

## First run in five minutes (non-coding example)

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

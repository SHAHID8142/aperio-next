# Python API examples

Everything the CLI does is a library call first; the CLI only formats and
maps errors. All examples run with the standard library alone.

```python
from aperio.goals import (
    build_goal_context_pack, describe_goal, goal_content_sha256,
    goal_identity, load_goal_file, validate_goal,
)
from aperio.context_pack import render_context_pack_markdown
from aperio.profiles import get_profile, list_profiles, profile_suggestions
```

## Goals and packs (M1/M2)

```python
goal = load_goal_file("poster-goal.json")     # validated + normalized
report = describe_goal(goal)                  # identity, trust, gaps, graph
print(goal_identity(goal), goal_content_sha256(goal))
pack = build_goal_context_pack(goal)          # provider-neutral context pack
print(render_context_pack_markdown(pack))
suggestions = profile_suggestions(goal["profile"])   # advisory only
```

## Quality evaluation (M4)

```python
from aperio.quality import (
    run_preflight, run_quality_check, render_quality_report_markdown,
    validate_quality_request,
)

preflight = run_preflight(goal)               # read-only, no writes
quality = run_quality_check(
    goal,
    deliverable_text=Path("answer.md").read_text(encoding="utf-8"),
    request=validate_quality_request({
        "required_sections": ["Limitations"],
        "max_words": 1500,
    }),
)
for issue in quality["content"]["issues"]:
    print(issue["status"], issue["severity"], issue["category"],
          issue["rule_id"], issue["evidence"])
print(render_quality_report_markdown(quality))
```

## Feedback and lessons (M5)

```python
from aperio.feedback import (
    validate_feedback, record_review, lesson_memory_record,
    feedback_identity, compare_quality_reports,
)
from aperio.memstore import MemoryStore

fb = validate_feedback({
    "problem": "Briefs must cite a source per factual claim",
    "origin": "operator", "actor": "editor-1",
    "lesson": "Every council brief cites at least one source per claim",
    "scope": {"kind": "profile", "profile": "research"},
})
reviewed = record_review(fb, decision="accepted", reviewer="editor-2",
                         at="2026-11-10T09:00:00Z")
store = MemoryStore("~/notes/lessons.mem")
memory = store.propose(lesson_memory_record(reviewed, actor="editor-2"))["memory"]
# memory["state"] == "proposed": review it with store.review(...) to accept
comparison = compare_quality_reports("report-before.json", "report-after.json")
print(comparison["content"]["outcome"])
```

## Universal memory (M3)

```python
store = MemoryStore("~/notes/brand.mem")
record = store.propose({
    "type": "requirement", "title": "Brand colour",
    "content": "Primary brand colour is deep green #0B5D3B.",
    "scope": {"kind": "global"}, "tags": ["brand"], "actor": "designer",
})["memory"]
store.review(record["memory_id"], "accept", actor="ops-lead")
store.review(record["memory_id"], "verify", actor="reviewer-1",
             evidence=["brand-guide-v3.pdf"])
snapshot = store.snapshots()["memories"][record["memory_id"]]
assert snapshot["trust"] == "verified"

from aperio.memrecall import recall_memories, memory_context_items
selection = recall_memories(store.snapshots()["memories"],
                            objective="spring campaign poster")
items = memory_context_items(selection["selected"])
```

## Exchange (M6) and briefs (M7)

```python
from aperio.exchange import (
    build_exchange, goal_section, quality_report_section, notes_section,
    plan_import, apply_import,
)
exchange = build_exchange([
    goal_section(goal),
    quality_report_section(quality),
    notes_section([{"text": "Final event date is not fixed"}]),
])
plan = plan_import(exchange, "work/")            # preview
report = apply_import(exchange, "work/")         # writes imported-*.json

from aperio.guidance import build_agent_brief, render_agent_brief_markdown
brief = build_agent_brief(goal, memory_selection=selection)
print(render_agent_brief_markdown(brief))        # paste into any agent
```

## Benchmark (M8)

```python
from aperio.benchmark import builtin_cases, run_benchmark
report = run_benchmark(builtin_cases(), Path("./work"))
assert report["content"]["cases_passed"] == report["content"]["cases_run"]
```

## Error handling

```python
from aperio.errors import AperioError, ValidationError, StateError, PathError

try:
    validate_goal({"objective": "missing the outcome"})
except ValidationError as exc:
    print("one actionable line:", exc)   # the CLI prints the same message
```

Invariants for library callers: everything in the universal layer is
read-only unless a function documents a single explicit output path;
memory stores are never created or discovered implicitly; no call
launches a process, contacts a provider, or promotes trust.

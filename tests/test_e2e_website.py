"""Chunk 08 E2E: a reproducible A-Z simulation of a client website
assignment — empty folder → operator goal → gated bridge plan → approval
→ orchestration → brain dumps → quality → feedback/lessons → skills →
checkpoint/revision/stale recovery → deployment gates → consolidated
work status → 30-day SEO engagement (priorities, honest reporting) →
handover. Everything runs through the public CLI (in-process ``main``)
or the same library calls the CLI makes — no providers, no network.

``test_deployment_requires_explicit_authorization`` covers the codex-cli
gate with the fake provider CLI from ``test_provider_adapters``;
``test_seo_report_honest_without_access`` covers SEO report honesty
labels (invented numbers flagged, honest unknowns labeled unknown).

Behavior note (actual CLI behavior, matching docs/website-delivery.md
section 11): ``run-start`` WITHOUT ``--authorize-exec`` on an available
executable is a documented read-only PREVIEW (exit 0), not an error.
Refusal gates that DO fire (missing executable, draft plan) fail cleanly
with exit 1 before any record, claim, or process exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import aperio.skills as _skills_module
from aperio.adapters import builtin_adapters
from aperio.cli import main
from aperio.memstore import MemoryStore
from aperio.orchestrate import ADAPTER_CAPABILITIES
from aperio.planstore import approve_plan, create_plan, read_plan, revise_plan
from aperio.runtime import dispatch_ordering, read_executions
from tests.test_provider_adapters import PLAN_INPUT, _config, _fake_cli

CANONICAL_DUMP = ("Fix the mobile Hero spacing. The Navbar dropdown is "
                  "broken. We should add an image gallery to the CMS. Dark "
                  "mode might be nice someday.")


def _work_task(tid, title, action, deliverable, criterion, deps=()):
    """Goal work task (goal-schema v1); carries no priority by design."""
    return {"id": tid, "title": title, "action": action,
            "deliverables": [deliverable],
            "completion_criteria": [criterion], "depends_on": list(deps)}


def _plan_tasks(work_tasks, *, priorities=None, retitles=None):
    """Mirror the bridge: work ids become TASK-00N in declared order."""
    order = {t["id"]: f"TASK-{i:03d}" for i, t in enumerate(work_tasks, 1)}
    planned = []
    for i, task in enumerate(work_tasks, 1):
        tid = order[task["id"]]
        entry = {"id": tid,
                 "title": (retitles or {}).get(tid, task["title"]),
                 "objective": task["action"],
                 "deliverables": list(task["deliverables"]),
                 "acceptance_criteria": list(task["completion_criteria"]),
                 "depends_on": [order[d] for d in task.get("depends_on", [])]}
        if (priorities or {}).get(tid):
            entry["priority"] = priorities[tid]
        planned.append(entry)
    return planned


def _plan_input(goal, tasks, *, scope=(), risks=(), strategy="s",
                verification="recorded evidence"):
    return {"objective": goal["objective"],
            "expected_outcome": goal["intended_outcome"],
            "scope": list(scope), "exclusions": [], "assumptions": [],
            "risks": list(risks), "strategy": strategy,
            "acceptance_criteria": list(goal["acceptance_criteria"]),
            "verification_strategy": verification, "tasks": tasks}


WEBSITE_WORK = [
    _work_task("design", "Design the pages", "Produce designs",
               "design spec", "Designs approved"),
    _work_task("frontend", "Build the frontend", "Implement the pages",
               "hero page markup", "Pages render", deps=["design"]),
    _work_task("backend", "Build the backend", "Implement the API",
               "api endpoints", "The API contract holds", deps=["design"]),
    _work_task("cms", "Configure the CMS", "Model content and publishing",
               "content model", "An editor can publish", deps=["backend"]),
    _work_task("security", "Run the security pass", "Review auth and secrets",
               "security checklist", "Reviewed",
               deps=["frontend", "backend"]),
]

WEBSITE_GOAL = {
    "goal_id": "WEBSITE-CLIENT-AZ",
    "objective": "Build and deliver the client's marketing website",
    "intended_outcome": "A deployed, maintainable site with recorded evidence",
    "profile": "software",
    "deliverables": ["deployed website", "handover notes"],
    "acceptance_criteria": ["The site builds and the pages render"],
    "work": {"tasks": WEBSITE_WORK},
}

# Revision content for the recovery step: same task ids as the bridge
# produced, so lifecycle state carries over (one retitled task).
WEBSITE_PLAN_V2 = _plan_input(
    WEBSITE_GOAL,
    _plan_tasks(WEBSITE_WORK, retitles={
        "TASK-005": "Run the security pass (revised: audit dependencies)"}),
    scope=["website"],
    risks=["Third-party scripts may change underneath the site"],
    strategy="design first, then sequential frontend/backend/CMS/security",
    verification="quality checks plus recorded evidence",
)

SEO_WORK = [
    _work_task("w1-audit", "W1: Technical SEO baseline audit",
               "Inspect the technical baseline", "Findings list",
               "Every finding carries a status label"),
    _work_task("w1-access", "W1: Search Console access setup or verification",
               "Confirm access before measuring", "Access status entry",
               "Access recorded as confirmed or unknown"),
    _work_task("w2-corrections", "W2: Approved technical corrections",
               "Ship approved corrections", "Correction records",
               "Every change records how it was checked",
               deps=["w1-audit", "w1-access"]),
    _work_task("w4-final-report",
               "W4: Monitoring follow-up and the final 30-day report",
               "Compile measured and unknown items", "Final 30-day report",
               "The report separates measured and unknown",
               deps=["w2-corrections"]),
]

SEO_GOAL = {
    "goal_id": "SEO-30DAY-AZ",
    "objective": ("Deliver a 30-day SEO service window for the client's "
                  "production website: audit, corrections, final report"),
    "intended_outcome": ("Corrections shipped with labeled evidence; a "
                         "final report separating measured, estimated, "
                         "and unknown items"),
    "profile": "research",
    "constraints": [
        "No ranking, traffic, or indexing promises appear in any deliverable",
        "Measured search data requires confirmed analytics access; without "
        "access, affected items are recorded as unknown",
    ],
    "deliverables": ["Client-facing audit summary", "Final 30-day report"],
    "acceptance_criteria": ["The final report separates measured data, "
                            "estimates, assumptions, and unknowns"],
    "evidence_requirements": ["Name the external tool and retrieval date "
                              "for every cited measurement"],
    "uncertainties": ["Access may not be granted during the window"],
    "work": {"tasks": SEO_WORK},
}

# Planned week-phase priorities (the bridge never invents them).
SEO_PLAN_V2 = _plan_input(
    SEO_GOAL,
    _plan_tasks(SEO_WORK, priorities={
        "TASK-001": "high", "TASK-002": "critical", "TASK-003": "high"}),
    risks=["Search engines re-crawl on their own schedule"],
    strategy="audit and access first, approved corrections, then report",
    verification="recorded tool output cited as evidence",
)


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def ok(capsys, *argv: str) -> str:
    """Run the CLI, assert a clean exit, return stdout."""
    code, out, err = run_cli(capsys, *argv)
    assert code == 0, err
    return out


def refused(capsys, *argv: str) -> str:
    """Run the CLI, assert a clean exit-1 refusal, return stderr."""
    code, out, err = run_cli(capsys, *argv)
    assert code == 1, f"expected a clean refusal, got exit {code}: {out}"
    assert err.startswith("aperio: error:") and "Traceback" not in err
    return err


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _json_file(path: Path, data) -> Path:
    return _write(path, json.dumps(data, indent=2, ensure_ascii=False))


def _issue_rules(report: dict) -> set[str]:
    return {i["rule_id"] for i in report["content"]["issues"]}


def test_full_client_website_az_simulation(tmp_path: Path, capsys) -> None:
    root = tmp_path / "garden-site"
    goal_file = _json_file(tmp_path / "website-goal.json", WEBSITE_GOAL)

    # 1. init from an EMPTY folder: project state only, never a website.
    assert not root.exists()
    assert "initialized" in ok(capsys, "init", str(root))
    aperio = root / ".aperio"
    assert (aperio / "state.json").is_file()
    for website_file in ("index.html", "styles.css", "package.json", "cms"):
        assert not (root / website_file).exists(), \
            "init never scaffolds a website"
    ok(capsys, "init", str(root))  # safe to re-run

    # 2. the operator's goal validates; preflight runs.
    assert "Goal valid" in ok(capsys, "goal-check", str(goal_file))
    ok(capsys, "preflight", str(goal_file))

    # 3. the goal→plan bridge proposes; --create makes a DRAFT plan.
    out = ok(capsys, "plan-propose-from-goal", str(goal_file), str(root),
             "--create")
    assert "DRAFT" in out and "approval is still required" in out
    plan = read_plan(aperio)
    assert plan["approval"]["status"] == "draft"
    assert [t["id"] for t in plan["tasks"]] == [
        "TASK-001", "TASK-002", "TASK-003", "TASK-004", "TASK-005"]

    # 4. genuine approval, then the ready task is claimed.
    ok(capsys, "plan-approve", str(root))
    ok(capsys, "task-status", str(root), "TASK-001",
       "--status", "in-progress", "--owner", "agent-a")

    # 5. orchestration: assessment, delegation plan, scoped brief.
    caps = [x for tid in ("TASK-002", "TASK-003", "TASK-004", "TASK-005")
            for x in ("--capability", f"{tid}:coding")]
    # machine-independent expectation: the coding capability is advice
    # that needs a subprocess adapter actually installed to be servable
    automated = [a for a in builtin_adapters() if a.available()[0]]
    expected = ("specialist-delegation" if any(
        "coding" in ADAPTER_CAPABILITIES.get(a.id, ()) for a in automated)
        else "single-agent-with-manual-fallback")
    assert expected in ok(capsys, "orchestrate-assess", str(root),
                          str(goal_file), *caps)
    ok(capsys, "orchestrate-plan", str(root), str(goal_file), *caps,
       "--actor", "operator")
    assert (aperio / "orchestration.json").is_file()
    by_task = {d["task_id"]: d for d in json.loads(
        (aperio / "orchestration.json").read_text(
            encoding="utf-8"))["delegations"]}
    assert by_task["TASK-001"]["specialist_adapter"] == "manual"
    assert "--authorize-exec" in json.dumps(
        by_task["TASK-002"]["authorization"])
    out = ok(capsys, "orchestrate-brief", str(root), "TASK-002",
             str(goal_file))
    assert "hero page markup" in out, "the specialist sees its own scope"
    assert "design spec" not in out and "api endpoints" not in out, \
        "other tasks' deliverables are absent"
    assert "TASK-001" in out, "dependency context stays visible"
    assert "cannot approve its own work" in out

    # 6. BRAIN DUMP mid-task: the canonical four-part client message.
    out = ok(capsys, "capture", str(root), "--message", CANONICAL_DUMP,
             "--actor", "operator")
    assert "Captured 4 item(s):" in out
    assert "Current task TASK-001 continues" in out and \
        "Nothing executed." in out
    canonical = json.loads(
        ok(capsys, "inbox", str(root), "--json"))["pending"][0]["items"]
    assert [i["category"] for i in canonical] == [
        "feature-request", "bug-report", "feature-request", "idea"]
    assert canonical[3]["routing"] == "idea-note", "idea routed as idea"
    assert all(i["execution_authorization"] == "none"
               for i in canonical), "nothing captured executes"
    ws = json.loads(ok(capsys, "work-status", str(root), "--json"))["content"]
    assert ws["current_work"]["active_task"]["task_id"] == "TASK-001", \
        "the active task is unchanged by the dump"
    assert ws["brain_dump"]["ideas"]

    # 7. fake urgency: text urgency language never carries authority.
    assert "Nothing executed." in ok(
        capsys, "capture", str(root), "--message", "URGENT add analytics",
        "--actor", "operator")
    urgent = next(c for c in json.loads(ok(
        capsys, "inbox", str(root), "--json"))["pending"]
        if c["capture_id"] == "INT-0002")
    item = urgent["items"][0]
    assert item["urgency_language_detected"] is True
    assert item["authority"] == "none"
    assert item["routing"] != "urgent-operator-requested"

    # 8. quality evaluation of the hero spec: the failing case is a report.
    hero_spec = _write(tmp_path / "hero-spec.md",
                       "# Hero component spec\n\n"
                       "The hero renders headline, subline, and call to "
                       "action. Breakpoints: 375, 768, 1280. Colors come "
                       "from the recorded tokens.\n")
    report_v1 = json.loads(ok(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(hero_spec), "--require-section", "Print specs", "--json"))
    assert "Q-required-section" in _issue_rules(report_v1)

    # 9. feedback → explicit review → stored lesson → future briefs.
    feedback_file = _json_file(tmp_path / "feedback.json", {
        "problem": "The hero spec was missing the required print section",
        "origin": "operator", "actor": "editor-1", "severity": "medium",
        "lesson": "Every component spec record includes a Print specs "
                  "section before handoff",
        "scope": {"kind": "profile", "profile": "software"},
        "created_at": "2026-11-11T09:00:00Z",
        "evaluation": {"source": "quality-report", "result": "failed",
                       "report_sha256": report_v1["content_sha256"]},
    })
    ok(capsys, "feedback-check", str(feedback_file))
    reviewed_file = tmp_path / "reviewed.json"
    ok(capsys, "feedback-review", str(feedback_file), "--decision",
       "accepted", "--actor", "editor-2", "--output", str(reviewed_file))
    lessons_file = tmp_path / "lessons.mem"
    ok(capsys, "feedback-lesson", str(reviewed_file), "--store",
       str(lessons_file), "--actor", "editor-2")
    lessons = MemoryStore(lessons_file)
    lesson_id = next(iter(lessons.snapshots()["memories"]))
    assert lessons.snapshots()["memories"][lesson_id]["state"] == "proposed"
    ok(capsys, "mem-review", str(lessons_file), lesson_id, "--accept",
       "--actor", "editor-3")
    out = ok(capsys, "agent-brief", str(goal_file), "--memory-store",
             str(lessons_file))
    assert "Accepted lessons from earlier feedback" in out

    # 10. skill composition from a builtin pack: untrusted until reviewed.
    packs_dir = Path(_skills_module.__file__).parent / "skill_packs"
    store = tmp_path / "skill-store"
    assert "untrusted" in ok(capsys, "skill-import", str(store),
                             str(packs_dir / "frontend-engineering.md"))
    ok(capsys, "skill-review", str(store), "frontend-engineering",
       "--action", "approve", "--actor", "operator")
    out = ok(capsys, "agent-brief", str(goal_file), "--skills-store",
             str(store), "--skill", "frontend-engineering")
    assert "Selected skills (quoted guidance)" in out

    # 11. the fixed spec passes; completion records evidence + limitations.
    hero_spec_v2 = _write(tmp_path / "hero-spec-v2.md",
                          "# Hero component spec\n\n"
                          "The hero renders headline, subline, and call to "
                          "action. Breakpoints: 375, 768, 1280.\n\n"
                          "## Print specs\n\n"
                          "Print stylesheet scales the hero to one column "
                          "and drops the navigation.\n")
    report_v2 = json.loads(ok(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(hero_spec_v2), "--require-section", "Print specs", "--json"))
    assert "Q-required-section" not in _issue_rules(report_v2)
    ok(capsys, "task-status", str(root), "TASK-001", "--status", "completed",
       "--result", "passed",
       "--evidence", "quality report on hero-spec-v2.md: required "
                     "sections present",
       "--limitations", "text-level checks only; rendered pages were not "
                        "inspected by aperio")

    # 12. checkpoint before pausing.
    ok(capsys, "checkpoint-write", str(root), "--reason", "before pausing")

    # 13. plan movement: a revision resets approval; recovery goes stale.
    # (task-status transitions bump plan_version too — lifecycle movement.)
    version_before = read_plan(aperio)["plan_version"]
    revise_plan(aperio, WEBSITE_PLAN_V2, revises=version_before,
                actor="planner-1")
    plan = read_plan(aperio)
    assert plan["plan_version"] == version_before + 1
    assert plan["approval"]["status"] == "draft"
    recovery = json.loads(ok(capsys, "recover", str(root), "--json"))
    assert recovery["status"] == "stale"
    assert recovery["resume_eligibility"]["eligible"] is False
    assert any("not approved" in r
               for r in recovery["resume_eligibility"]["reasons"])

    # 14. deployment gate: every refused dispatch stays clean and inert.
    fake = _fake_cli(tmp_path, "fake-codex-e2e")
    missing_cfg = _json_file(tmp_path / "missing-codex.json", {
        "executable": str(tmp_path / "no-such-codex"),
        "working_dir": str(root), "timeout_seconds": 20,
        "env_policy": "minimal"})
    codex_cfg = _json_file(tmp_path / "codex.json", _config(fake, root))

    def _inert(plan_bytes: bytes) -> None:
        assert read_executions(aperio) is None, "no record was created"
        assert not fake.with_suffix(".capture.json").exists(), \
            "no process was started"
        assert (aperio / "plan.json").read_bytes() == plan_bytes, \
            "the plan was NOT modified"

    plan_bytes = (aperio / "plan.json").read_bytes()
    err = refused(capsys, "run-start", str(root), "TASK-002", "--agent",
                  "codex-cli", "--config", str(missing_cfg))
    assert "codex-cli" in err  # refused before any mutation
    err = refused(capsys, "run-start", str(root), "TASK-002", "--agent",
                  "codex-cli", "--config", str(codex_cfg))
    assert "not eligible" in err and "approved plan" in err, \
        "a draft revision blocks dispatch cleanly"
    _inert(plan_bytes)
    ok(capsys, "plan-approve", str(root))  # renewed approval resumes tracking
    plan_bytes = (aperio / "plan.json").read_bytes()
    out = ok(capsys, "run-start", str(root), "TASK-002", "--agent",
             "codex-cli", "--config", str(codex_cfg))
    assert "read-only preparation (no --authorize-exec)" in out
    _inert(plan_bytes)

    # 15. work-status consolidates evidence, intake, and recovery.
    ws = json.loads(ok(capsys, "work-status", str(root), "--json"))["content"]
    evidence = ws["execution"]["verification_evidence"]
    assert [e["task_id"] for e in evidence] == ["TASK-001"]
    assert "quality report" in evidence[0]["evidence"]
    assert len(ws["brain_dump"]["pending_items"]) == 5
    assert ws["brain_dump"]["ideas"]
    assert ws["recovery"]["status"] == "stale"
    assert ws["current_work"]["approval_status"] == "approved"

    # 16. the 30-day SEO engagement: a second goal in a second workspace
    # (plan-create is create-only; a new engagement is a new workspace).
    seo_root = tmp_path / "seo-engagement"
    seo_goal_file = _json_file(tmp_path / "seo-goal.json", SEO_GOAL)
    ok(capsys, "init", str(seo_root))
    assert "Goal valid" in ok(capsys, "goal-check", str(seo_goal_file))
    ok(capsys, "preflight", str(seo_goal_file))
    assert "DRAFT" in ok(capsys, "plan-propose-from-goal", str(seo_goal_file),
                         str(seo_root), "--create")
    seo_aperio = seo_root / ".aperio"
    seo_plan = read_plan(seo_aperio)
    assert seo_plan["approval"]["status"] == "draft"
    titles = [t["title"] for t in seo_plan["tasks"]]
    assert titles[0].startswith("W1:")
    assert "final 30-day report" in titles[-1].lower()
    ok(capsys, "plan-approve", str(seo_root))
    assert all(t.get("priority", "normal") == "normal"
               for t in seo_plan["tasks"]), "the bridge invents no priority"

    # Scheduling priorities are planned through the plan gate.
    revise_plan(seo_aperio, SEO_PLAN_V2, revises=seo_plan["plan_version"],
                actor="planner-2")
    ok(capsys, "plan-approve", str(seo_root))
    ordering = dispatch_ordering(read_plan(seo_aperio), [])
    assert [e["task_id"] for e in ordering] == ["TASK-002", "TASK-001"], \
        "critical outranks high despite the higher task id"
    assert ordering[0]["priority"] == "critical"
    assert "priority" in ordering[0]["ordering_basis"]

    # 17. the SEO final report is checked for measurement honesty.
    bad_report = _write(tmp_path / "seo-report-v1.md",
                        "# Final 30-day report\n\n"
                        "Rankings improved by 40% after our work and "
                        "impressions increased week over week. Our "
                        "corrections guarantee lasting visibility gains "
                        "for every target page.\n")
    fired = _issue_rules(json.loads(ok(
        capsys, "quality-check", str(seo_goal_file), "--deliverable",
        str(bad_report), "--json")))
    assert "Q-missing-attribution" in fired
    assert "Q-unsupported-absolute-claim" in fired

    # 18. handover: integrity and the dry-run export, both engagements.
    for project_root in (root, seo_root):
        assert json.loads(ok(capsys, "integrity", str(project_root),
                             "--json"))["ok"] is True
        assert "nothing written" in ok(capsys, "export", str(project_root),
                                       "--dry-run")


def test_deployment_requires_explicit_authorization(tmp_path: Path,
                                                    capsys) -> None:
    root = tmp_path / "proj"
    assert main(["init", str(root)]) == 0
    capsys.readouterr()
    aperio = root / ".aperio"
    create_plan(aperio, PLAN_INPUT)
    approve_plan(aperio)
    fake = _fake_cli(tmp_path, "fake-codex-auth")
    config_file = _json_file(tmp_path / "codex.json", _config(fake, root))

    # Without --authorize-exec: read-only preparation. No process runs,
    # no record exists, no claim is taken.
    out = ok(capsys, "run-start", str(root), "TASK-001", "--agent",
             "codex-cli", "--config", str(config_file))
    assert "read-only preparation (no --authorize-exec)" in out
    assert not fake.with_suffix(".capture.json").exists(), \
        "no process ran without authorization"
    assert read_executions(aperio) is None, "no record was created"
    assert read_plan(aperio)["tasks"][0]["status"] == "pending", "no claim"

    # With --authorize-exec: the dispatch runs the fake provider and the
    # record stays honest — exit 0 is recorded, completion is NOT claimed.
    record = json.loads(ok(capsys, "run-start", str(root), "TASK-001",
                           "--agent", "codex-cli", "--config",
                           str(config_file), "--authorize-exec", "--json"))
    assert record["status"] == "completed" and record["exit_code"] == 0
    assert record["adapter_id"] == "codex-cli"
    assert record["provider_result"]["parse_status"] == "ok"
    assert fake.with_suffix(".capture.json").exists(), \
        "the authorized process ran"
    capture = json.loads(fake.with_suffix(".capture.json").read_text())
    assert capture["argv"][
        capture["argv"].index("--sandbox") + 1] == "read-only"
    serialized = (aperio / "executions.json").read_text(encoding="utf-8")
    assert str(fake) not in serialized, "machine paths never enter records"
    assert read_plan(aperio)["tasks"][0]["status"] == "in-progress", \
        "exit 0 is not verification; the task is not completed"


def test_seo_report_honest_without_access(tmp_path: Path, capsys) -> None:
    goal_file = _json_file(tmp_path / "seo-goal.json", {
        "goal_id": "SEO-REPORT-HONESTY",
        "objective": "Deliver the 30-day SEO service final report",
        "intended_outcome": ("A report separating measured data, "
                             "estimates, assumptions, and unknowns"),
        "profile": "research",
        "deliverables": ["Final 30-day report"],
        "evidence_requirements": [
            "Every measured number cites its tool and retrieval date"
        ],
    })

    # A report that invents numbers: attribution and absolute-claim rules
    # fire because the goal requires evidence.
    claiming = _write(tmp_path / "seo-report-claiming.md",
                      "# Final 30-day report\n\n"
                      "Impressions increased 40% during the engagement and "
                      "rankings improved across the board. Our "
                      "optimisations guarantee the gains hold for every "
                      "target page.\n")
    fired = _issue_rules(json.loads(ok(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(claiming), "--json")))
    assert "Q-missing-attribution" in fired
    assert "Q-unsupported-absolute-claim" in fired

    # The honest report: unknown labels, cited provenance, no invented
    # numbers. Attribution passes; whether the source supports the claims
    # stays honestly unknown to the tool.
    honest = _write(tmp_path / "seo-report-honest.md",
                    "# Final 30-day report\n\n"
                    "## Access and data status\n\n"
                    "No confirmed Search Console or analytics access "
                    "existed during the window, so every traffic- and "
                    "ranking-shaped question is recorded as unknown; no "
                    "measured number is quoted.\n\n"
                    "source: client-run manual check (site search, "
                    "robots.txt, sitemap.xml), recorded 2026-11-20 [1].\n\n"
                    "## Unknowns and unmeasured items\n\n"
                    "Impressions, clicks, and positions: unknown until real "
                    "access exists. Next-month recommendations carry no "
                    "prediction of results.\n")
    issues = {i["rule_id"]: i for i in json.loads(ok(
        capsys, "quality-check", str(goal_file), "--deliverable",
        str(honest), "--json"))["content"]["issues"]}
    assert "Q-missing-attribution" not in issues
    assert "Q-unsupported-absolute-claim" not in issues
    assert "Q-source-support-unknown" in issues, \
        "honest uncertainty is labeled unknown, not silently resolved"
    assert issues["Q-source-support-unknown"]["status"] == "unknown"

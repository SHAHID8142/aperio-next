"""Cross-domain benchmark and evaluation (M8, aperio-benchmark-report v1).

A reproducible, offline evaluation framework that measures what Aperio's
tooling deterministically does on **versioned fixture cases**: planted,
objectively checkable flaws in goals and deliverables, trust-boundary
attempts, cross-agent information transfer, memory correctness, and
execution overhead — across six domains, with software deliberately one
among many.

What this framework measures, honestly:

* **Detection** — whether the assisted pipeline (preflight, quality-check,
  memory recall) finds the issues a case deliberately plants. The baseline
  workflow ("submit the deliverable with no checks") detects zero issues
  *by definition*; the comparison shows what the tooling adds on the
  fixture set, nothing more.
* **Trust-boundary enforcement** — attempts to promote unproven claims are
  counted as blocked (or, if one ever slipped through, as a failure).
* **Information preservation** — fields planted into an exchange must
  arrive verbatim, with provenance intact and trust un-promoted.
* **Missing-information handling** — declared uncertainties must be
  reported as missing, never invented away.
* **Overhead** — wall-clock seconds per stage, reported as measured
  durations and excluded from the content hash.

What this framework deliberately does **not** claim:

* It does **not** measure whether AI agents perform better with Aperio.
  That requires a human evaluator (or an external ground-truth source)
  judging real agent output; those metrics are listed per case under
  ``requires_human`` and are never scored here.
* It produces no aggregate quality score. Rates are counts of planted
  findings detected, which is a tool property, not a quality judgment.

Fixture cases are versioned JSON (benchmark-case v1) shipped in
``aperio/benchmark_cases/``; the runner is pure library + CLI, works
offline with no provider, and any regression (a case whose planted issue
stops being detected) is reported as a failure, never hidden.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from aperio.errors import ValidationError
from aperio.exchange import (
    apply_import,
    build_exchange,
    goal_section,
    notes_section,
)
from aperio.feedback import compare_quality_reports
from aperio.goals import build_goal_context_pack, validate_goal
from aperio.memrecall import recall_memories
from aperio.memstore import resolve_trust
from aperio.quality import run_preflight, run_quality_check

BENCHMARK_REPORT_FORMAT = "aperio-benchmark-report"
BENCHMARK_REPORT_VERSION = 1
CASE_SCHEMA_VERSION = 1

BENCHMARK_DOMAINS = ("general", "design", "research", "writing", "business",
                     "software")

MAX_CASE_FILE_BYTES = 256 * 1024
MAX_PLANTED_ISSUES = 20
MAX_EXPECTED_RULES = 30
MAX_MEMORY_FIXTURES = 20
BASELINE_DETECTION = 0  # the no-checks workflow detects nothing, by definition


# ---------------------------------------------------------------------------
# case schema (benchmark-case v1)
# ---------------------------------------------------------------------------

CASE_KEYS = (
    "case_schema_version",
    "case_id",
    "domain",
    "scenario",
    "checks",
    "goal",
    "deliverable_text",
    "planted_issues",
    "expected_rules",
    "forbidden_rules",
    "memory_fixtures",
    "trust_attempts",
    "exchange_expectations",
    "uncertainty_expectations",
    "requires_human",
)


def validate_case(data: object) -> dict:
    """Validate one benchmark-case v1 document."""

    if not isinstance(data, dict):
        raise ValidationError("benchmark case: the document must be a JSON object")
    extra = sorted(set(data) - set(CASE_KEYS))
    if extra:
        raise ValidationError(
            "benchmark case: unknown fields: " + ", ".join(extra)
        )
    version = data.get("case_schema_version", CASE_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != CASE_SCHEMA_VERSION:
        raise ValidationError(
            "benchmark case: unsupported case_schema_version; this aperio "
            f"supports version {CASE_SCHEMA_VERSION}"
        )
    case_id = data.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip() \
            or "\n" in case_id or "\x00" in case_id:
        raise ValidationError(
            "benchmark case: 'case_id' must be single-line non-empty text"
        )
    domain = data.get("domain")
    if domain not in BENCHMARK_DOMAINS:
        raise ValidationError(
            "benchmark case: 'domain' must be one of: "
            + ", ".join(BENCHMARK_DOMAINS)
        )
    scenario = data.get("scenario")
    if not isinstance(scenario, str) or not scenario.strip() \
            or "\n" in scenario or "\x00" in scenario:
        raise ValidationError(
            "benchmark case: 'scenario' must be single-line non-empty text"
        )
    checks = data.get("checks", ["quality"])
    if not isinstance(checks, list) or not checks or any(
        c not in ("preflight", "quality", "memory", "exchange", "feedback")
        for c in checks
    ):
        raise ValidationError(
            "benchmark case: 'checks' must be a non-empty list drawn from: "
            "preflight, quality, memory, exchange, feedback"
        )
    goal = validate_goal(data.get("goal"))
    deliverable_text = data.get("deliverable_text")
    if deliverable_text is not None and (
        not isinstance(deliverable_text, str)
        or len(deliverable_text.encode("utf-8")) > 1024 * 1024
        or "\x00" in deliverable_text
    ):
        raise ValidationError(
            "benchmark case: 'deliverable_text' must be UTF-8 text at most "
            "1 MiB without NUL"
        )

    planted = data.get("planted_issues", [])
    if not isinstance(planted, list) or len(planted) > MAX_PLANTED_ISSUES:
        raise ValidationError(
            f"benchmark case: 'planted_issues' must be a list of at most "
            f"{MAX_PLANTED_ISSUES} objects"
        )
    for index, issue in enumerate(planted):
        if not isinstance(issue, dict) or not isinstance(
            issue.get("category"), str
        ) or not issue.get("description"):
            raise ValidationError(
                f"benchmark case: planted_issues[{index}] must carry a "
                "category and description"
            )

    def rule_list(key: str) -> list[str]:
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > MAX_EXPECTED_RULES \
                or any(not isinstance(r, str) for r in value):
            raise ValidationError(
                f"benchmark case: '{key}' must be a list of at most "
                f"{MAX_EXPECTED_RULES} rule ids"
            )
        return list(value)

    memory_fixtures = data.get("memory_fixtures", [])
    if not isinstance(memory_fixtures, list) \
            or len(memory_fixtures) > MAX_MEMORY_FIXTURES:
        raise ValidationError(
            f"benchmark case: 'memory_fixtures' must hold at most "
            f"{MAX_MEMORY_FIXTURES} raw memory records"
        )

    trust_attempts = data.get("trust_attempts", [])
    if not isinstance(trust_attempts, list):
        raise ValidationError(
            "benchmark case: 'trust_attempts' must be a list"
        )

    return {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "case_id": case_id,
        "domain": domain,
        "scenario": scenario,
        "checks": list(checks),
        "goal": goal,
        "deliverable_text": deliverable_text,
        "planted_issues": list(planted),
        "expected_rules": rule_list("expected_rules"),
        "forbidden_rules": rule_list("forbidden_rules"),
        "memory_fixtures": list(memory_fixtures),
        "trust_attempts": list(trust_attempts),
        "exchange_expectations": data.get("exchange_expectations"),
        "uncertainty_expectations": bool(
            data.get("uncertainty_expectations", False)
        ),
        "requires_human": list(data.get("requires_human", [])),
    }


def load_case_file(path: str | Path) -> dict:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValidationError("refusing to follow a symlink for a case file")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"cannot read case file: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_CASE_FILE_BYTES:
        raise ValidationError(
            f"case file must be at most {MAX_CASE_FILE_BYTES} UTF-8 bytes"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"case file is not valid UTF-8 JSON: {exc}"
        ) from None
    return validate_case(data)


def builtin_cases() -> list[dict]:
    """The versioned case set shipped with the package."""

    import importlib.resources

    cases: list[dict] = []
    root = importlib.resources.files("aperio") / "benchmark_cases"
    for entry in sorted(root.iterdir()):
        if entry.name.endswith(".json"):
            with entry.open("r", encoding="utf-8") as handle:
                cases.append(validate_case(json.load(handle)))
    if not cases:
        raise ValidationError(
            "the built-in benchmark case set is empty; this installation "
            "is broken"
        )
    return cases


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def _memory_selection(case: dict):
    if not case["memory_fixtures"]:
        return None
    records = {}
    for fixture in case["memory_fixtures"]:
        resolved = resolve_trust(fixture, label="memory fixture")
        records[fixture["memory_id"]] = {**fixture,
                                         "trust": resolved["trust"],
                                         "verification": resolved["verification"]}
    return recall_memories(
        records,
        objective=case["goal"]["objective"],
        profile=case["goal"]["profile"],
    )


def _run_memory_metrics(case: dict, selection) -> dict:
    metrics: dict = {"applicable": True}
    excluded = (selection.get("excluded") or {}).get("by_reason") or {}
    expired = int(excluded.get("expired", 0))
    selected = selection.get("selected", [])
    metrics["expired_excluded"] = expired
    metrics["selected_count"] = len(selected)
    metrics["unreviewed_flagged"] = any(
        w.get("code") == "memory_unreviewed_selected"
        for w in selection.get("warnings", [])
    )
    metrics["conflicts_reported"] = bool(selection.get("conflicts"))
    metrics["private_never_selected"] = all(
        m.get("sensitivity") != "private" for m in selected
    )
    return metrics


def _run_exchange_metrics(case: dict, workdir: Path) -> dict:
    """Round-trip preservation: planted fields must arrive verbatim, with
    provenance intact and memory still proposed."""

    expectations = case.get("exchange_expectations") or {}
    if not expectations:
        return {"applicable": False}
    from aperio.exchange import memory_section, plan_import
    from aperio.memportable import export_memories
    from aperio.memstore import MemoryStore

    sections = [goal_section(case["goal"])]
    if case["memory_fixtures"]:
        store = MemoryStore(workdir / "source.mem")
        for fixture in case["memory_fixtures"]:
            # propose derives identity from content, so strip every
            # lifecycle/derived field; the store owns what lands.
            record = {k: v for k, v in fixture.items()
                      if k in ("type", "title", "content", "scope", "source",
                               "tags", "sensitivity", "evidence",
                               "valid_from", "valid_until", "supersedes",
                               "actor", "declared_state")}
            record.pop("memory_id", None)
            store.propose(record)
        if case.get("trust_attempts"):
            pass  # trust attempts are evaluator-side, not store content
        doc = export_memories(store)
        sections.append(memory_section(doc))
    notes = expectations.get("notes") or []
    if notes:
        sections.append(notes_section([{"text": n} for n in notes]))
    exchange = build_exchange(sections)
    dest = workdir / "dest"
    dest.mkdir(exist_ok=True)
    store_path = None
    if case["memory_fixtures"]:
        store_path = workdir / "dest.mem"
    report = apply_import(
        exchange, dest,
        memory_store_path=store_path,
    )

    preserved: list[str] = []
    lost: list[str] = []
    imported_goal_file = next(
        (e["file"] for e in report["content"]["planned"]
         if e["kind"] == "goal" and e.get("file")), None
    )
    if imported_goal_file:
        arrived = json.loads((dest / imported_goal_file).read_text())
        for field in expectations.get("goal_fields_verbatim", []):
            if arrived.get(field) == case["goal"].get(field):
                preserved.append(f"goal.{field}")
            else:
                lost.append(f"goal.{field}")
    memory_metrics = report["content"].get("memory") or {}
    if case["memory_fixtures"]:
        imported_ids = memory_metrics.get("imported") or []
        declared = memory_metrics.get("declared_states") or {}
        dest_snapshots = MemoryStore(store_path).snapshots()["memories"]
        all_proposed = all(
            snap["state"] == "proposed" for snap in dest_snapshots.values()
        )
        if all_proposed and len(dest_snapshots) == len(imported_ids):
            preserved.append("memory_trust_not_promoted")
        else:
            lost.append("memory_trust_not_promoted")
        unsupported = memory_metrics.get("unsupported_trust_claims") or {}
        if unsupported:
            preserved.append("unsupported_claims_flagged")
        # identity is content-derived, so expectations are keyed on title
        for title in expectations.get("memory_titles_imported", []):
            arrived = any(snap["title"] == title
                          for snap in dest_snapshots.values())
            (preserved if arrived else lost).append(f"memory:{title!r}")
    note_files = [e for e in report["content"]["planned"]
                  if e["kind"] == "operator-notes" and e.get("file")]
    for index, note in enumerate(notes):
        if note_files:
            arrived = json.loads(
                (dest / note_files[0]["file"]).read_text()
            )
            texts = [item.get("text") for item in arrived.get("items", [])]
            (preserved if note in texts else lost).append(f"note[{index}]")
    return {
        "applicable": True,
        "preserved": preserved,
        "lost": lost,
        "imported_files": sorted(
            e["file"] for e in report["content"]["planned"] if e.get("file")
        ),
    }


def _run_trust_attempts(case: dict) -> dict:
    """Attempted promotions must be refused or demoted — never honored."""

    results = []
    for attempt in case.get("trust_attempts", []):
        kind = attempt.get("kind")
        blocked = False
        detail = ""
        if kind == "declared-verified-without-history":
            forged = dict(attempt["record"])
            try:
                resolved = resolve_trust(forged, label="attempt")
                blocked = resolved["trust"] != "verified"
                detail = f"derived trust: {resolved['trust']}"
            except ValidationError as exc:
                blocked = True
                detail = f"refused: {str(exc)[:120]}"
        elif kind == "agent-goal-as-brief":
            from aperio.guidance import build_agent_brief

            try:
                build_agent_brief(validate_goal(attempt["goal"]))
                blocked = False
                detail = "brief built from an untrusted goal"
            except ValidationError as exc:
                blocked = True
                detail = f"refused: {str(exc)[:120]}"
        elif kind == "untrusted-goal-instructions-in-pack":
            pack = build_goal_context_pack(validate_goal(attempt["goal"]))
            authority = pack["content"]["goal"]["authority"]
            blocked = authority == "untrusted"
            detail = f"pack authority: {authority}"
        else:
            blocked = False
            detail = f"unknown attempt kind {kind!r}"
        results.append({
            "kind": kind,
            "blocked": blocked,
            "detail": detail,
        })
    return {
        "applicable": bool(case.get("trust_attempts")),
        "attempts": results,
        "all_blocked": all(r["blocked"] for r in results) if results else None,
    }


def _run_feedback_metrics(case: dict, workdir: Path, goal: dict,
                          before_text: str, after_text: str,
                          worse_text: str) -> dict:
    """Feedback-based improvement: deterministic before/after comparison,
    with the regression case reported as its own outcome."""

    before = run_quality_check(goal, deliverable_text=before_text)
    after = run_quality_check(goal, deliverable_text=after_text)
    worse = run_quality_check(goal, deliverable_text=worse_text)
    before_file = workdir / "before.json"
    after_file = workdir / "after.json"
    worse_file = workdir / "worse.json"
    before_file.write_text(json.dumps(before), encoding="utf-8")
    after_file.write_text(json.dumps(after), encoding="utf-8")
    worse_file.write_text(json.dumps(worse), encoding="utf-8")
    improved = compare_quality_reports(before_file, after_file)
    regressed = compare_quality_reports(after_file, worse_file)
    return {
        "applicable": True,
        "improved_outcome": improved["content"]["outcome"],
        "regressed_outcome": regressed["content"]["outcome"],
        "regressions_reported": bool(
            regressed["content"]["regression_issue_ids"]
        ),
    }


def run_case(case: dict, workdir: Path) -> dict:
    """Execute one case; returns its measured metrics and pass/fail."""

    workdir.mkdir(parents=True, exist_ok=True)
    goal = case["goal"]
    failures: list[str] = []
    started = time.perf_counter()

    preflight_report = None
    quality_report = None
    selection = None

    if "preflight" in case["checks"]:
        stage = time.perf_counter()
        selection = _memory_selection(case)
        preflight_report = run_preflight(goal, memory=selection)
        preflight_seconds = time.perf_counter() - stage

    if "quality" in case["checks"]:
        stage = time.perf_counter()
        if case["deliverable_text"] is None:
            failures.append("quality check requested without deliverable_text")
        else:
            quality_report = run_quality_check(
                goal, deliverable_text=case["deliverable_text"]
            )
        quality_seconds = time.perf_counter() - stage

    memory_metrics = {"applicable": False}
    if "memory" in case["checks"]:
        if selection is None:
            selection = _memory_selection(case)
        memory_metrics = _run_memory_metrics(case, selection)

    exchange_metrics = {"applicable": False}
    if "exchange" in case["checks"]:
        stage = time.perf_counter()
        exchange_metrics = _run_exchange_metrics(case, workdir)
        exchange_seconds = time.perf_counter() - stage
    else:
        exchange_seconds = None

    feedback_metrics = {"applicable": False}
    if "feedback" in case["checks"]:
        expectations = case.get("exchange_expectations") or {}
        feedback_metrics = _run_feedback_metrics(
            case, workdir, goal,
            expectations.get("before_text", ""),
            expectations.get("after_text", ""),
            expectations.get("worse_text", ""),
        )

    # detection accounting (quality + preflight rules)
    fired: dict[str, str] = {}
    if quality_report:
        for issue in quality_report["content"]["issues"]:
            fired.setdefault(issue["rule_id"], issue["status"])
    if preflight_report:
        for issue in preflight_report["content"]["issues"]:
            fired.setdefault(issue["rule_id"], issue["status"])
    expected = set(case["expected_rules"])
    detected = sorted(expected & set(fired))
    missed = sorted(expected - set(fired))
    extra_confirmed = sorted(
        rule for rule, status in fired.items()
        if rule not in expected and status == "confirmed"
    )
    for rule in case["forbidden_rules"]:
        if rule in fired:
            failures.append(f"forbidden rule fired: {rule}")
    if missed:
        failures.append("planted issues not detected: " + ", ".join(missed))
    if extra_confirmed:
        failures.append(
            "unexpected confirmed findings: " + ", ".join(extra_confirmed)
        )

    trust = _run_trust_attempts(case)
    if trust["applicable"] and not trust["all_blocked"]:
        failures.append("a trust-boundary attempt was not blocked")

    if exchange_metrics.get("applicable") and exchange_metrics.get("lost"):
        failures.append(
            "information lost in transfer: "
            + ", ".join(exchange_metrics["lost"])
        )

    if feedback_metrics.get("applicable"):
        if feedback_metrics["improved_outcome"] not in ("improved", "mixed"):
            failures.append(
                "expected an improved/mixed outcome after the fix, got "
                + feedback_metrics["improved_outcome"]
            )
        if not feedback_metrics["regressions_reported"]:
            failures.append("the regression case was not reported")

    uncertainty_metrics = {"applicable": case["uncertainty_expectations"]}
    if case["uncertainty_expectations"]:
        pack = build_goal_context_pack(goal)
        missing = pack["content"]["missing_information"]
        uncertainties = goal["uncertainties"]
        reported = all(u in missing for u in uncertainties)
        uncertainty_metrics["reported_as_missing"] = reported
        uncertainty_metrics["never_invented"] = (
            len(missing) == len(uncertainties)
        )
        if not reported:
            failures.append("declared uncertainties were not reported as "
                            "missing information")

    elapsed = time.perf_counter() - started
    return {
        "case_id": case["case_id"],
        "domain": case["domain"],
        "scenario": case["scenario"],
        "passed": not failures,
        "failures": failures,
        "metrics": {
            "planted_issues": len(case["planted_issues"]),
            "expected_rules": case["expected_rules"],
            "detected_rules": detected,
            "missed_rules": missed,
            "unexpected_confirmed": extra_confirmed,
            "fired_rules": sorted(fired),
            "baseline_detected": BASELINE_DETECTION,
            "memory": memory_metrics,
            "exchange": exchange_metrics,
            "trust": trust,
            "feedback": feedback_metrics,
            "uncertainties": uncertainty_metrics,
        },
        "requires_human": case["requires_human"],
        "overhead_seconds": {
            "total": round(elapsed, 4),
            "preflight": round(preflight_seconds, 4)
            if preflight_report else None,
            "quality": round(quality_seconds, 4)
            if quality_report else None,
            "exchange": round(exchange_seconds, 4)
            if exchange_seconds is not None else None,
        },
    }


def run_benchmark(cases: list[dict], workdir: Path) -> dict:
    """Run every case and aggregate per domain. No aggregate quality score
    is produced — only counts of planted findings detected."""

    results = [run_case(case, workdir / case["case_id"]) for case in cases]
    by_domain: dict[str, dict] = {}
    for result in results:
        domain = result["domain"]
        entry = by_domain.setdefault(domain, {
            "cases": 0,
            "passed": 0,
            "expected_total": 0,
            "detected_total": 0,
        })
        entry["cases"] += 1
        entry["passed"] += 1 if result["passed"] else 0
        entry["expected_total"] += len(result["metrics"]["expected_rules"])
        entry["detected_total"] += len(result["metrics"]["detected_rules"])
    for entry in by_domain.values():
        entry["detection_rate"] = (
            round(entry["detected_total"] / entry["expected_total"], 4)
            if entry["expected_total"] else None
        )
    failures = [r for r in results if not r["passed"]]
    content = {
        "kind": "benchmark-report",
        "scope": (
            "measures what Aperio's tooling deterministically detects on "
            "versioned fixture cases with planted, objectively checkable "
            "flaws; the baseline workflow (no checks) detects zero by "
            "definition. This is NOT a measurement that agents perform "
            "better with Aperio: output quality and factual accuracy "
            "require a human evaluator and are listed per case under "
            "'requires_human', never scored here"
        ),
        "cases_run": len(results),
        "cases_passed": len(results) - len(failures),
        "regressions": [
            {"case_id": r["case_id"], "failures": r["failures"]}
            for r in failures
        ],
        "by_domain": dict(sorted(by_domain.items())),
        "results": results,
        "limitations": [
            "detection counts reflect fixture wording: paraphrased or "
            "semantic-only flaws are out of scope by design",
            "wall-clock overhead is machine-dependent and excluded from "
            "the content hash",
            "no model API, provider, or network is used; cases run offline",
            "an aggregate 'quality score' is deliberately not computed: "
            "counting planted findings detected is a tool property, not a "
            "measure of output quality",
        ],
    }
    return {
        "format": BENCHMARK_REPORT_FORMAT,
        "format_version": BENCHMARK_REPORT_VERSION,
        "generated_at": _utc_now(),
        "aperio_version": _aperio_version(),
        "content_sha256": _digest_content(content),
        "content": content,
    }


def _digest_content(content: dict) -> str:
    import copy

    stripped = copy.deepcopy(content)
    for result in stripped["results"]:
        result["overhead_seconds"] = None
    return hashlib.sha256(
        json.dumps(
            stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def _aperio_version() -> str:
    from aperio import __version__

    return __version__


def render_benchmark_markdown(report: dict) -> str:
    content = report["content"]
    lines = [
        "# Aperio benchmark report",
        "",
        f"Cases: {content['cases_run']} run, {content['cases_passed']} "
        "passed.",
        "",
        content["scope"],
        "",
        "## Per-domain detection (planted findings the tooling detected)",
        "",
        "| Domain | Cases | Passed | Expected rules | Detected | Rate |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for domain, entry in content["by_domain"].items():
        rate = entry["detection_rate"]
        lines.append(
            f"| {domain} | {entry['cases']} | {entry['passed']} | "
            f"{entry['expected_total']} | {entry['detected_total']} | "
            f"{rate if rate is not None else 'n/a'} |"
        )
    lines.append("")
    if content["regressions"]:
        lines.append("## Regressions (cases that failed)")
        lines.append("")
        for regression in content["regressions"]:
            lines.append(f"- **{regression['case_id']}**: "
                         + "; ".join(regression["failures"]))
        lines.append("")
    lines.append("## Cases")
    lines.append("")
    for result in content["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(
            f"### {result['case_id']} — {result['domain']} — **{status}**"
        )
        lines.append("")
        lines.append(f"Scenario: {result['scenario']}")
        metrics = result["metrics"]
        lines.append(
            f"- Planted: {metrics['planted_issues']}; detected: "
            f"{len(metrics['detected_rules'])}"
            + (f"; missed: {', '.join(metrics['missed_rules'])}"
               if metrics["missed_rules"] else "")
        )
        if metrics["trust"]["applicable"]:
            lines.append(
                f"- Trust attempts blocked: "
                f"{sum(1 for a in metrics['trust']['attempts'] if a['blocked'])}"
                f"/{len(metrics['trust']['attempts'])}"
            )
        if metrics["exchange"]["applicable"]:
            exchange = metrics["exchange"]
            lines.append(
                f"- Transfer: {len(exchange['preserved'])} preserved, "
                f"{len(exchange['lost'])} lost"
            )
        if result["requires_human"]:
            lines.append(
                "- Requires human evaluation (not scored): "
                + "; ".join(result["requires_human"])
            )
        lines.append(f"- Overhead: {result['overhead_seconds']['total']}s")
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in content["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)

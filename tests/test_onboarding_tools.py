"""Tool, MCP, and host discovery: honest status, safety, authorization."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aperio.errors import StateError, ValidationError
from aperio.onboarding_discovery import discover_workspace
from aperio.onboarding_reconstruct import reconstruct_workspace
from aperio.onboarding_session import add_tool_record, create_session, read_session, record_approval
from aperio.onboarding_tools import (
    INSTALL_ACTION,
    MAX_CONFIG_BYTES,
    build_install_plan,
    discover_tools,
    execute_install,
    probe_tool,
    recommend_tools,
)
from aperio.project import init_project


@pytest.fixture
def ws(project_dir):
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def apdir(ws):
    init_project(str(ws))
    return ws / ".aperio"


def _report(ws, **kwargs):
    discovery = discover_workspace(str(ws))
    return discover_tools(str(ws), discovery, **kwargs)


def test_missing_tools_reported_honestly(ws):
    report = _report(ws)
    statuses = {t.tool_id: t.status for t in report.executables}
    # python3 exists (tests run under it); pick something almost never present.
    assert statuses.get("codex") in ("missing", "installed")
    # The report never upgrades "missing" to anything stronger.
    for tool in report.executables:
        if tool.status == "missing":
            assert tool.path is None
            assert tool.version is None


def test_installed_tool_found_on_path(ws):
    report = _report(ws)
    python3 = next(t for t in report.executables if t.tool_id == "python3")
    assert python3.status == "installed"
    assert python3.path and Path(python3.path).exists()


def test_installed_does_not_imply_authenticated_or_working(ws):
    report = _report(ws)
    for tool in report.executables:
        if tool.status == "installed":
            assert tool.version is None  # no probe ran
    assert any("installed does not imply" in n for n in report.to_dict()["notes"])


def test_mcp_config_detected_but_only_configured(ws):
    (ws / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "definitely-not-real-cmd"}}})
    )
    report = _report(ws)
    server = next(t for t in report.project_local if t.tool_id == "mcp:fs")
    assert server.status == "configured"
    assert any("NOT found" in e for e in server.evidence)
    assert "not authenticated, not probed" in server.detail


def test_mcp_server_command_found_on_path_is_still_only_configured(ws):
    (ws / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"py": {"command": "python3"}}})
    )
    report = _report(ws)
    server = next(t for t in report.project_local if t.tool_id == "mcp:py")
    assert server.status == "configured"
    assert "not probed" in server.detail


def test_corrupt_mcp_config_is_ignored_not_crashed(ws):
    (ws / ".mcp.json").write_text("{ broken")
    report = _report(ws)
    assert not [t for t in report.project_local if t.tool_id.startswith("mcp:")]


def test_host_markers_detected(ws):
    (ws / "CLAUDE.md").write_text("# rules\n")
    (ws / ".cursorrules").write_text("rules\n")
    report = _report(ws)
    detected = {t.tool_id for t in report.project_local if t.status == "detected"}
    assert "claude-code" in detected
    assert "cursor" in detected


def test_symlinked_marker_not_followed(ws):
    os.symlink(ws.parent, ws / ".codex")
    report = _report(ws)
    codex = next(t for t in report.project_local if t.tool_id == "codex-cli")
    assert codex.status == "unknown"


def test_user_config_scan_requires_opt_in(ws):
    report = _report(ws)
    assert report.user_config == []
    assert report.user_config_scanned is False


def test_user_config_opt_in_scans_documented_paths_only(ws):
    # Even with opt-in, only documented paths are consulted; a random file
    # elsewhere in $HOME is never read.
    report = _report(ws, include_user_config=True)
    assert report.user_config_scanned is True
    for finding in report.user_config:
        assert finding.tool_id in {"claude-code", "codex-cli", "cursor", "opencode"}


def test_host_matrix_is_honest(ws):
    report = _report(ws)
    by_id = {h["host_id"]: h for h in report.hosts}
    assert by_id["terminal"]["support"] == "runtime-enforced"
    assert by_id["text-chat"]["support"] == "cooperative"
    assert "not integrated" in by_id["gui-hosts"]["support"]
    for host in report.hosts:
        if host["host_id"] not in ("terminal", "text-chat"):
            assert "no native slash-command claimed" in host["invocation"] or "manual" in host["invocation"] or "fallback" in host["invocation"]


def test_report_is_json_safe(ws):
    payload = _report(ws).to_dict()
    decoded = json.loads(json.dumps(payload))
    assert decoded["format"] == "aperio-onboarding-tools"
    assert decoded["version"] == 1


def test_probe_refused_for_missing_tool(ws):
    report = _report(ws)
    missing = next(t for t in report.executables if t.status == "missing")
    with pytest.raises(StateError, match="not installed"):
        probe_tool(report, missing.tool_id)


def test_probe_runs_bounded_version_command(ws):
    report = _report(ws)
    python3 = next(t for t in report.executables if t.tool_id == "python3")
    probed = probe_tool(report, "python3", argv=(python3.path, "--version"))
    assert probed.status == "verified_by_probe"
    assert probed.version  # something like "Python 3.14.7"
    assert "proves only that the binary runs" in probed.detail


def test_probe_reports_unknown_on_failure(ws):
    report = _report(ws)
    python3 = next(t for t in report.executables if t.tool_id == "python3")
    probed = probe_tool(report, "python3", argv=(python3.path, "--definitely-not-a-flag"))
    assert probed.status == "unknown"


def test_probe_rejects_oversized_argv(ws):
    report = _report(ws)
    with pytest.raises(ValidationError):
        probe_tool(report, "python3", argv=tuple(["x"] * 9))


def test_recommendations_are_project_relevant(ws):
    discovery = discover_workspace(str(ws))
    reconstruction = reconstruct_workspace(str(ws))
    report = _report(ws)
    recs = recommend_tools(discovery, reconstruction, report, project_category="Desktop application")
    # Browser tooling is relevant only through the web profiles or the
    # "testing" coverage topic. A desktop app with no documented testing
    # coverage gets pytest guidance, not browser automation.
    ids = [r["tool_id"] for r in recs]
    assert "chrome-devtools-mcp" not in ids
    assert "playwright-mcp" not in ids or any(
        "missing coverage: testing" in r["relevance"]
        for r in recs if r["tool_id"] == "playwright-mcp"
    )


def test_recommendations_follow_missing_coverage(ws):
    discovery = discover_workspace(str(ws))
    reconstruction = reconstruct_workspace(str(ws))
    report = _report(ws)
    recs = recommend_tools(discovery, reconstruction, report, project_category="Web application")
    ids = [r["tool_id"] for r in recs]
    assert "playwright-mcp" in ids
    by_id = next(r for r in recs if r["tool_id"] == "playwright-mcp")
    assert by_id["declared_source"]
    assert "unknown" in by_id["maintenance_status"]


def test_install_plan_has_all_ten_items(ws):
    report = _report(ws)
    plan = build_install_plan("chrome-devtools-mcp", report)
    for key in (
        "1_name", "2_intended_purpose", "3_source", "4_candidate_version",
        "5_compatibility", "6_required_permissions", "7_required_network_access",
        "8_install_command", "9_security_considerations", "10_removal",
    ):
        assert key in plan, f"missing {key}"
    assert "manual_fallback" in plan
    assert plan["4_candidate_version"].startswith("unspecified")


def test_unknown_candidate_refused(ws):
    report = _report(ws)
    with pytest.raises(ValidationError):
        build_install_plan("totally-unknown-tool", report)


def test_execution_requires_recorded_approval_and_flag(apdir, ws):
    create_session(apdir, workspace=str(ws), classification="existing_non_aperio")
    report = _report(ws)
    plan = build_install_plan("chrome-devtools-mcp", report)
    # Neither gate present:
    with pytest.raises(StateError, match="not approved"):
        execute_install(apdir, plan, authorize_install=False)
    # Approval recorded but flag missing:
    record_approval(apdir, action=INSTALL_ACTION, scope="chrome-devtools-mcp")
    with pytest.raises(StateError, match="--authorize-install"):
        execute_install(apdir, plan, authorize_install=False)
    # Flag present but approval missing:
    fresh = ws.parent / "other"
    fresh.mkdir()
    init_project(str(fresh))
    apdir2 = fresh / ".aperio"
    create_session(apdir2, workspace=str(fresh), classification="existing_non_aperio")
    with pytest.raises(StateError, match="not approved"):
        execute_install(apdir2, plan, authorize_install=True)


def test_execution_runs_fixed_argv_and_records_honestly(apdir, ws):
    """Execute a trivially safe fixed command to prove the machinery, not npm."""
    create_session(apdir, workspace=str(ws), classification="existing_non_aperio")
    record_approval(apdir, action=INSTALL_ACTION, scope="echo-probe")
    python3 = next(t.path for t in _report(ws).executables if t.tool_id == "python3")
    plan = {
        "tool_id": "echo-probe",
        "5_compatibility": "found",
        "8_install_command": [python3, "-c", "print('install simulation')"],
    }
    record = execute_install(apdir, plan, authorize_install=True)
    assert record["status"] == "install_command_completed"
    # Honesty: completion is not verification.
    assert "NOT verified" in record["detail"]
    session = read_session(apdir)
    assert session["tool_records"][-1]["name"] == "echo-probe"


def test_execution_refuses_missing_prerequisites(apdir, ws):
    create_session(apdir, workspace=str(ws), classification="existing_non_aperio")
    record_approval(apdir, action=INSTALL_ACTION, scope="chrome-devtools-mcp")
    plan = build_install_plan("chrome-devtools-mcp", _report(ws))
    plan["5_compatibility"] = "missing"
    with pytest.raises(StateError, match="prerequisites missing"):
        execute_install(apdir, plan, authorize_install=True)


def test_execution_refuses_manual_only_tools(apdir, ws):
    create_session(apdir, workspace=str(ws), classification="existing_non_aperio")
    record_approval(apdir, action=INSTALL_ACTION, scope="pytest")
    plan = build_install_plan("pytest", _report(ws))
    with pytest.raises(StateError, match="manual-only"):
        execute_install(apdir, plan, authorize_install=True)


def test_declined_install_changes_nothing(apdir, ws):
    """Scenario 6: operator declines; nothing is installed or executed."""
    create_session(apdir, workspace=str(ws), classification="existing_non_aperio")
    report = _report(ws)
    before = sorted(p.name for p in ws.rglob("*"))
    plan = build_install_plan("chrome-devtools-mcp", report)
    # Operator declines: no approval recorded, no execute call.
    with pytest.raises(StateError):
        execute_install(apdir, plan, authorize_install=False)
    after = sorted(p.name for p in ws.rglob("*"))
    assert before == after


def test_discovery_repeated_detects_changes(ws):
    first = _report(ws)
    assert not [t for t in first.project_local if t.tool_id.startswith("mcp:")]
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "nope"}}}))
    second = _report(ws)
    assert [t for t in second.project_local if t.tool_id.startswith("mcp:")]
    # No background monitoring is claimed: the change was found because
    # discovery was re-run explicitly.
    assert any("no network" in n for n in second.to_dict()["notes"])

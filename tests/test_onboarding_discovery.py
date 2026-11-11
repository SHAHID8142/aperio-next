"""Onboarding workspace discovery: classification and read-only guarantees."""

from __future__ import annotations

import json
import os

import pytest

from aperio.onboarding_discovery import (
    CLASS_APERIO_PARTIAL,
    CLASS_APERIO_PROJECT,
    CLASS_AMBIGUOUS,
    CLASS_EMPTY_NEW,
    CLASS_EXISTING_NON_APERIO,
    CLASS_LEGACY_ORCHESTRATED,
    DISCOVERY_RESULT_VERSION,
    discover_workspace,
)
from aperio.project import init_project


@pytest.fixture
def ws(project_dir):
    """A fresh, existing (empty) project directory."""
    project_dir.mkdir()
    return project_dir


def test_empty_folder_detected(ws):
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_EMPTY_NEW
    assert result.is_empty is True
    assert result.has_aperio_dir is False


def test_discovery_changes_no_files(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hi')\n")
    (workspace / "README.md").write_text("# demo\n")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in workspace.rglob("*"))
    discover_workspace(str(workspace))
    after = sorted((p.name, p.stat().st_mtime_ns) for p in workspace.rglob("*"))
    assert before == after


def test_files_but_no_aperio_is_not_empty(ws):
    (ws / "main.py").write_text("print(1)\n")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_EXISTING_NON_APERIO
    assert result.is_empty is False


def test_existing_aperio_state_recognized(project_dir):
    init_project(str(project_dir))
    result = discover_workspace(str(project_dir))
    assert result.classification == CLASS_APERIO_PROJECT
    assert result.aperio_state_status == "valid"
    assert result.aperio_state is not None
    assert result.aperio_state.name == project_dir.name


def test_partially_initialized_project(ws):
    (ws / ".aperio").mkdir()
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_APERIO_PARTIAL
    assert result.aperio_state_status == "missing"
    assert result.missing_scaffolding == ("brief.md", "decisions.md", "handoff.md")


def test_partial_with_some_scaffolding(ws):
    aperio_dir = ws / ".aperio"
    aperio_dir.mkdir()
    (aperio_dir / "brief.md").write_text("# Project Brief\n")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_APERIO_PARTIAL
    assert result.missing_scaffolding == ("decisions.md", "handoff.md")


def test_corrupt_state_is_ambiguous(ws):
    (ws / ".aperio").mkdir()
    (ws / ".aperio/state.json").write_text("{ definitely not json")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_AMBIGUOUS
    assert result.aperio_state_status == "corrupt"


def test_newer_schema_version_is_ambiguous(ws):
    (ws / ".aperio").mkdir()
    (ws / ".aperio/state.json").write_text(
        json.dumps({"schema_version": 999, "name": "x"})
    )
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_AMBIGUOUS
    assert result.aperio_state_status == "unsupported_version"


def test_symlinked_aperio_dir_is_ambiguous(ws):
    other = ws / "elsewhere"
    other.mkdir()
    os.symlink(other, ws / ".aperio")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_AMBIGUOUS
    assert result.aperio_dir_is_symlink is True


def test_symlinked_state_file_is_ambiguous(ws):
    aperio_dir = ws / ".aperio"
    aperio_dir.mkdir()
    os.symlink(ws / "outside.json", aperio_dir / "state.json")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_AMBIGUOUS
    assert result.aperio_state_status == "symlinked"


def test_legacy_orchestration_metadata_identified(ws):
    (ws / "AGENTS.md").write_text("# rules\n")
    (ws / "src").mkdir()
    (ws / "src/main.py").write_text("pass\n")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_LEGACY_ORCHESTRATED
    assert "AGENTS.md" in result.legacy_orchestrator_evidence
    assert "AGENTS.md" in result.ai_instructions


def test_legacy_markers_recorded_alongside_valid_aperio(project_dir):
    init_project(str(project_dir))
    (project_dir / "CLAUDE.md").write_text("# rules\n")
    result = discover_workspace(str(project_dir))
    assert result.classification == CLASS_APERIO_PROJECT
    assert "CLAUDE.md" in result.legacy_orchestrator_evidence


def test_noise_only_directory_is_not_empty(ws):
    (ws / ".DS_Store").write_text("")
    result = discover_workspace(str(ws))
    assert result.classification == CLASS_EXISTING_NON_APERIO
    assert result.is_empty is False


def test_documentation_and_task_documents_inventoried(ws):
    (ws / "README.md").write_text("# demo\n")
    (ws / "PRD-v2.md").write_text("prd\n")
    (ws / "TODO.md").write_text("- ship\n")
    result = discover_workspace(str(ws))
    assert "README.md" in result.documentation
    assert "PRD-v2.md" in result.documentation
    assert "TODO.md" in result.task_documents


def test_ignored_directories_are_not_descended(ws):
    node_modules = ws / "node_modules" / "pkg" / "deep"
    node_modules.mkdir(parents=True)
    for i in range(3000):
        (node_modules / f"f{i}.py").write_text("x" * 10)
    (ws / "main.py").write_text("pass\n")
    result = discover_workspace(str(ws))
    assert result.scan_truncated is False
    assert result.source_files_count == 1
    assert "node_modules" in result.top_level_names


def test_scan_bound_reports_truncation(ws):
    deep = ws
    for i in range(6):
        deep = deep / f"level{i}"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("pass\n")
    result = discover_workspace(str(ws))
    assert result.scan_truncated is True
    assert "scan_limits" in result.to_dict()


def test_escaping_symlink_flagged(ws):
    target = ws.parent
    (ws / "keep.txt").write_text("x")
    os.symlink(target, ws / "escape")
    result = discover_workspace(str(ws))
    matches = [s for s in result.symlinks if s.path == "escape"]
    assert len(matches) == 1
    assert matches[0].escapes_workspace is True


def test_internal_symlink_not_flagged_escaping(ws):
    (ws / "docs").mkdir()
    (ws / "docs/guide.md").write_text("# guide\n")
    os.symlink(ws / "docs/guide.md", ws / "alias.md")
    result = discover_workspace(str(ws))
    matches = [s for s in result.symlinks if s.path == "alias.md"]
    assert len(matches) == 1
    assert matches[0].escapes_workspace is False


def test_nonexistent_path_rejected(tmp_path):
    with pytest.raises(Exception):
        discover_workspace(str(tmp_path / "ghost"))


def test_file_as_target_rejected(ws):
    target = ws / "plain.txt"
    target.write_text("x")
    with pytest.raises(Exception):
        discover_workspace(str(target))


def test_classification_reasons_present(ws):
    (ws / "main.py").write_text("pass\n")
    result = discover_workspace(str(ws))
    assert len(result.classification_reasons) >= 1
    assert all(isinstance(r, str) and r for r in result.classification_reasons)


def test_to_dict_is_json_safe_and_versioned(project_dir):
    init_project(str(project_dir))
    payload = discover_workspace(str(project_dir)).to_dict()
    encoded = json.dumps(payload)  # must not raise
    decoded = json.loads(encoded)
    assert decoded["format"] == "aperio-onboarding-discovery"
    assert decoded["version"] == DISCOVERY_RESULT_VERSION
    assert decoded["classification"] == CLASS_APERIO_PROJECT
    assert decoded["aperio_state"]["schema_version"] >= 1


def test_counts_are_recorded(ws):
    (ws / "app.py").write_text("pass\n")
    (ws / "style.css").write_text("body{}\n")
    (ws / "config.json").write_text("{}\n")
    (ws / "Makefile").write_text("all:\n")
    result = discover_workspace(str(ws))
    assert result.source_files_count == 1
    assert result.config_files_count == 1
    assert result.other_files_count == 2  # .css and Makefile

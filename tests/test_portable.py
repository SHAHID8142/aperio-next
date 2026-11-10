"""Feature 05: portable export/import, integrity, schema-upgrade foundation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from aperio import portable
from aperio.cli import main
from aperio.errors import AperioError, PathError, StateError, ValidationError
from aperio.integrity import check_integrity
from aperio.locks import acquire_lock, release_lock
from aperio.planmodel import PLAN_SCHEMA_VERSION
from aperio.planstore import approve_plan, create_plan, read_plan, update_task
from aperio.portable import export_project, import_archive, read_portable_archive
from aperio.runtime import dispatch_task, read_executions
from aperio.upgrade import Migration, apply_upgrade, plan_upgrade

SECRET = "sk-live-A1B2C3D4-do-not-share"

PLAN = {
    "objective": "prove portability",
    "expected_outcome": "context arrives intact",
    "scope": ["portable files"],
    "exclusions": [],
    "assumptions": [],
    "risks": [],
    "strategy": "one export, one import",
    "acceptance_criteria": ["round trip works"],
    "verification_strategy": "pytest",
    "tasks": [
        {
            "id": "TASK-001",
            "title": "portable task",
            "objective": "travel",
            "deliverables": ["archive"],
            "acceptance_criteria": ["imported"],
            "depends_on": [],
        }
    ],
}


def _init_project(run_cli, project_dir: Path, *, plan: bool = True) -> Path:
    code, out, err = run_cli("init", str(project_dir))
    assert code == 0, err
    aperio_dir = project_dir / ".aperio"
    if plan:
        plan_file = project_dir / "plan.json"
        plan_file.write_text(json.dumps(PLAN))
        code, out, err = run_cli(
            "plan-create", str(project_dir), "--file", str(plan_file)
        )
        assert code == 0, err
        code, out, err = run_cli("plan-approve", str(project_dir))
        assert code == 0, err
    return aperio_dir


def _awaiting_project(run_cli, project_dir: Path) -> Path:
    """A project with a manual run awaiting an external agent + decision."""
    aperio_dir = _init_project(run_cli, project_dir)
    record = dispatch_task(
        aperio_dir, "TASK-001", adapter_id="manual", config=None,
        authorize_exec=False,
    )
    assert record["status"] == "awaiting-external"
    code, out, err = run_cli(
        "decision-add", str(project_dir),
        "--title", "Portability", "--context", "machines differ",
        "--decision", "use the archive format", "--consequences", "hashes only",
    )
    assert code == 0, err
    return aperio_dir


def _export(project_dir: Path, archive: Path, **kwargs) -> dict:
    return export_project(str(project_dir), output=str(archive), **kwargs)


def _zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


def _manifest_of(path: Path) -> dict:
    return json.loads(_zip_members(path)["manifest.json"])


def _build_archive(
    archive_path: Path,
    files: dict[str, bytes],
    *,
    schemas: dict | None = None,
    manifest: dict | None = None,
    extra_members: dict[str, bytes] | None = None,
    symlink_members: tuple[str, ...] = (),
) -> None:
    """Build a controlled archive (for adversarial import tests)."""
    import hashlib

    if manifest is None:
        entries = [
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for rel, data in sorted(files.items())
        ]
        manifest = {
            "format": portable.EXPORT_FORMAT,
            "format_version": portable.EXPORT_FORMAT_VERSION,
            "aperio_version": "0.4.0",
            "created_at": "2026-01-01T00:00:00Z",
            "schemas": schemas if schemas is not None else {"state": 1},
            "files": entries,
            "excluded": [],
            "notes": "test archive",
        }
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(info, json.dumps(manifest))
        for rel, data in sorted(files.items()):
            info = zipfile.ZipInfo(
                f"files/{rel}", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, data)
        for name, data in (extra_members or {}).items():
            zf.writestr(name, data)
        for name in symlink_members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, b"../../outside")


# --- export ------------------------------------------------------------------


def test_export_requires_initialized_project(run_cli, project_dir, tmp_path):
    with pytest.raises(AperioError):
        _export(project_dir, tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


def test_export_dry_run_writes_nothing(run_cli, project_dir, tmp_path):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "dry.zip"
    report = _export(project_dir, archive, dry_run=True)
    assert report["dry_run"] is True
    assert report["archive"] is None
    assert not archive.exists()
    assert sorted(e["path"] for e in report["files"]) == [
        "brief.md", "decisions.md", "executions.json", "handoff.md",
        "plan.json", "runs/RUN-0001.handoff.md", "state.json",
    ]


def test_export_never_overwrites_an_existing_archive(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "twice.zip"
    archive.write_bytes(b"PRESERVE")
    with pytest.raises(StateError, match="refusing to overwrite"):
        _export(project_dir, archive)
    assert archive.read_bytes() == b"PRESERVE"


def test_export_excludes_local_secrets(run_cli, project_dir, tmp_path):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    local = aperio_dir / "local"
    (local / "notes.txt").write_text(SECRET, encoding="utf-8")
    (local / "runs").mkdir(exist_ok=True)
    (local / "runs" / "RUN-0001.report.txt").write_text(SECRET, encoding="utf-8")
    archive = tmp_path / "portable.zip"
    _export(project_dir, archive)
    members = _zip_members(archive)
    assert not any("local/" in name for name in members)
    for data in members.values():
        assert SECRET.encode() not in data
    assert ".gitignore" not in members


def test_export_awaiting_external_run_is_portable_and_machine_neutral(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    # Plant machine-specific fields the exporter must not leak.
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["pid"] = 4242
    document["runs"][0]["machine"] = "host-specific-token"
    document["runs"][0]["local_log_ref"] = "local/runs/RUN-0001.log"
    exec_file.write_text(json.dumps(document))
    archive = tmp_path / "neutral.zip"
    report = _export(project_dir, archive)
    assert "executions.json" in {e["path"] for e in report["files"]}
    exported = json.loads(_zip_members(archive)["files/executions.json"])
    run = exported["runs"][0]
    assert run["status"] == "awaiting-external"
    assert run["handoff_ref"] == "runs/RUN-0001.handoff.md"
    assert run["pid"] is None and run["machine"] is None
    assert run["local_log_ref"] is None
    raw = archive.read_bytes()
    assert b"host-specific-token" not in raw


def test_export_refuses_active_local_run(run_cli, project_dir, tmp_path, monkeypatch):
    aperio_dir = _init_project(run_cli, project_dir)
    dispatch_task(aperio_dir, "TASK-001", adapter_id="manual",
                  config=None, authorize_exec=False)
    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["status"] = "running"
    exec_file.write_text(json.dumps(document))
    report = _export(project_dir, tmp_path / "x.zip")
    assert "executions.json" not in {e["path"] for e in report["files"]}
    assert any("active run" in w for w in report["warnings"])


def test_export_refuses_symlinked_shareable_file(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _init_project(run_cli, project_dir)
    outside = tmp_path / "private.txt"
    outside.write_text(SECRET)
    os.unlink(aperio_dir / "brief.md")
    os.symlink(outside, aperio_dir / "brief.md")
    with pytest.raises(PathError, match="symlink"):
        _export(project_dir, tmp_path / "x.zip")


def test_export_output_symlink_refused(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    target = tmp_path / "real.zip"
    target.write_bytes(b"UNTOUCHED")
    link = tmp_path / "link.zip"
    os.symlink(target, link)
    with pytest.raises(PathError, match="symlink"):
        _export(project_dir, link)
    assert target.read_bytes() == b"UNTOUCHED"


def test_export_refuses_unsupported_state_schema(run_cli, project_dir, tmp_path):
    aperio_dir = _init_project(run_cli, project_dir)
    state_file = aperio_dir / "state.json"
    document = json.loads(state_file.read_text())
    document["schema_version"] = 99
    state_file.write_text(json.dumps(document))
    with pytest.raises(StateError, match="supports at most"):
        _export(project_dir, tmp_path / "x.zip")


# --- import ------------------------------------------------------------------


def test_round_trip_across_two_projects(run_cli, project_dir, tmp_path):
    source = project_dir
    aperio_dir = _awaiting_project(run_cli, source)
    archive = tmp_path / "trip.zip"
    _export(source, archive)
    destination = tmp_path / "dest"
    report = import_archive(str(destination), str(archive))
    assert report["created"] and not report["replaced"]

    assert read_plan(destination / ".aperio")["objective"] == PLAN["objective"]
    runs = read_executions(destination / ".aperio")["runs"]
    assert runs[0]["status"] == "awaiting-external"
    assert (destination / ".aperio" / "runs" / "RUN-0001.handoff.md").is_file()
    text = (destination / ".aperio" / "decisions.md").read_text()
    assert "DEC-0001" in text and "Portability" in text
    state = json.loads((destination / ".aperio" / "state.json").read_text())
    assert state["schema_version"] == 1
    assert check_integrity(str(destination))["ok"] is True
    # Scaffolding restored locally, never imported from the archive.
    assert (destination / ".aperio" / "local" / "README.md").is_file()
    assert (destination / ".aperio" / ".gitignore").is_file()
    assert (destination / ".aperio" / "state.json.tmp").exists() is False


def test_import_dry_run_changes_nothing(run_cli, project_dir, tmp_path):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    report = import_archive(str(destination), str(archive), dry_run=True)
    assert report["created"]
    assert not destination.exists()


def test_import_conflict_preserves_existing_context(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)

    destination = tmp_path / "dest"
    _init_project(run_cli, destination)
    (destination / ".aperio" / "brief.md").write_text("LOCAL EDIT", encoding="utf-8")
    with pytest.raises(StateError, match="would overwrite"):
        import_archive(str(destination), str(archive))
    assert (destination / ".aperio" / "brief.md").read_text() == "LOCAL EDIT"
    # And nothing else was touched.
    local_state = json.loads((destination / ".aperio" / "state.json").read_text())
    assert local_state["schema_version"] == 1


def test_import_replace_after_explicit_approval(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    _init_project(run_cli, destination)
    (destination / ".aperio" / "brief.md").write_text("LOCAL EDIT", encoding="utf-8")
    report = import_archive(str(destination), str(archive), replace=True)
    assert "brief.md" in report["replaced"]
    assert (destination / ".aperio" / "brief.md").read_text() != "LOCAL EDIT"
    assert check_integrity(str(destination))["ok"] is True


def test_import_identical_bytes_need_no_replace(run_cli, project_dir, tmp_path):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    import_archive(str(destination), str(archive))
    again = import_archive(str(destination), str(archive))
    assert not again["created"] and not again["replaced"]
    assert sorted(again["unchanged"]) == sorted(again["unchanged"])


def test_import_rejects_traversal_manifest(run_cli, project_dir, tmp_path):
    state = (project_dir / "state.json")
    _init_project(run_cli, project_dir)
    state_bytes = (project_dir / ".aperio" / "state.json").read_bytes()
    archive = tmp_path / "evil.zip"
    _build_archive(
        archive,
        {"../../evil.txt": b"x"},
        manifest={
            "format": portable.EXPORT_FORMAT,
            "format_version": 1,
            "aperio_version": "0.4.0",
            "created_at": "2026-01-01T00:00:00Z",
            "schemas": {"state": 1},
            "files": [
                {"path": "../../evil.txt", "size": 1, "sha256": "00" * 32},
                {"path": "state.json", "size": len(state_bytes),
                 "sha256": __import__("hashlib").sha256(state_bytes).hexdigest()},
            ],
        },
        extra_members={},
    )
    with pytest.raises(AperioError, match="traversal|not an approved|whitelist"):
        import_archive(str(tmp_path / "dest"), str(archive))
    assert not (tmp_path / "dest").exists()
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "C:/windows/x", "files/local/secret.txt",
     "local/secret.txt", ".gitignore", "state.json/", "runs/../state.json",
     "runs/not-a-handoff.md"],
)
def test_import_rejects_non_portable_paths(run_cli, project_dir, tmp_path, bad_path):
    _init_project(run_cli, project_dir)
    state_bytes = (project_dir / ".aperio" / "state.json").read_bytes()
    archive = tmp_path / "bad.zip"
    _build_archive(
        archive,
        {"state.json": state_bytes, bad_path: b"x"},
        manifest={
            "format": portable.EXPORT_FORMAT,
            "format_version": 1,
            "aperio_version": "0.4.0",
            "created_at": "2026-01-01T00:00:00Z",
            "schemas": {"state": 1},
            "files": [
                {"path": rel, "size": len(data), "sha256":
                 __import__("hashlib").sha256(data).hexdigest()}
                for rel, data in (
                    ("state.json", state_bytes), (bad_path, b"x")
                )
            ],
        },
    )
    with pytest.raises(AperioError):
        import_archive(str(tmp_path / "dest"), str(archive))


def test_import_rejects_symlink_member(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    state_bytes = (project_dir / ".aperio" / "state.json").read_bytes()
    archive = tmp_path / "link.zip"
    _build_archive(
        archive,
        {"state.json": state_bytes},
        symlink_members=("files/evil",),
    )
    with pytest.raises(AperioError, match="symlink"):
        import_archive(str(tmp_path / "dest"), str(archive))


def test_import_rejects_tampered_hash(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    files = _zip_members(archive)
    manifest = json.loads(files["manifest.json"])
    for entry in manifest["files"]:
        if entry["path"] == "brief.md":
            entry["sha256"] = "f" * 64
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data if name != "manifest.json"
                        else json.dumps(manifest).encode())
    with pytest.raises(AperioError, match="SHA-256|corrupt|tampered"):
        import_archive(str(tmp_path / "dest"), str(archive))
    assert not (tmp_path / "dest").exists()


def test_import_rejects_corrupt_and_missing_manifest(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(AperioError, match="not a ZIP|invalid"):
        import_archive(str(tmp_path / "dest"), str(broken))
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("files/state.json", b"{}")
    with pytest.raises(AperioError, match="manifest"):
        import_archive(str(tmp_path / "dest"), str(empty))


def test_import_rejects_unsupported_newer_state_schema(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    state = json.loads((project_dir / ".aperio" / "state.json").read_text())
    state["schema_version"] = 2
    data = json.dumps(state).encode()
    archive = tmp_path / "future.zip"
    _build_archive(archive, {"state.json": data}, schemas={"state": 2})
    destination = tmp_path / "dest"
    with pytest.raises(StateError, match="schema version 2|supports at most"):
        import_archive(str(destination), str(archive))
    assert not destination.exists()


def test_import_rejects_broken_plan_run_relationship(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    files = {
        name[len("files/"):]: data
        for name, data in _zip_members(archive).items()
        if name.startswith("files/")
    }
    files.pop("plan.json")
    schemas = _manifest_of(archive)["schemas"]
    schemas.pop("plan", None)
    broken = tmp_path / "broken.zip"
    _build_archive(broken, files, schemas=schemas)
    with pytest.raises(StateError, match="no plan.json"):
        import_archive(str(tmp_path / "dest"), str(broken))


def test_import_rolls_back_on_write_failure(
    run_cli, project_dir, tmp_path, monkeypatch
):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    calls = {"n": 0}
    real = portable._create_new_bytes

    def flaky(path, data, *, rel):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StateError("simulated write failure")
        return real(path, data, rel=rel)

    monkeypatch.setattr(portable, "_create_new_bytes", flaky)
    with pytest.raises(StateError, match="simulated write failure"):
        import_archive(str(destination), str(archive))
    # Rollback removed this call's files; the recovery path is usable.
    assert not (destination / ".aperio" / "brief.md").exists()
    monkeypatch.undo()
    report = import_archive(str(destination), str(archive))
    assert report["created"]
    assert check_integrity(str(destination))["ok"] is True


def test_import_finishes_a_partial_previous_import(
    run_cli, project_dir, tmp_path
):
    """A crash before state.json is recoverable: matching bytes are kept."""
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    destination.mkdir()
    partial = destination / ".aperio"
    partial.mkdir()
    brief = _zip_members(archive)["files/brief.md"]
    (partial / "brief.md").write_bytes(brief)
    report = import_archive(str(destination), str(archive))
    assert "brief.md" in report["unchanged"]
    assert "state.json" in report["created"]
    assert check_integrity(str(destination))["ok"] is True


def test_import_lock_blocks_concurrent_import(
    run_cli, project_dir, tmp_path, monkeypatch
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / ".aperio").mkdir()
    lock = destination / ".aperio" / "import.lock"
    monkeypatch.setattr(portable, "IMPORT_LOCK_TIMEOUT", 0.2)
    token = acquire_lock(lock, timeout=1.0, artifact="test")
    try:
        with pytest.raises(StateError, match="lock"):
            import_archive(str(destination), str(archive))
    finally:
        from aperio.locks import release_lock

        release_lock(lock, token)
    assert not (destination / ".aperio" / "state.json").exists()


# --- integrity ----------------------------------------------------------------


def test_integrity_ok_report_is_machine_readable(run_cli, project_dir):
    _awaiting_project(run_cli, project_dir)
    report = check_integrity(str(project_dir))
    assert report["ok"] is True and report["errors"] == []
    assert report["schemas"]["state"] == 1
    assert report["schemas"]["handoff"] == "v1"
    assert set(report["supported"]) == {"state", "plan", "roadmap", "executions"}
    code, out, err = main(["integrity", str(project_dir), "--json"]) or (0, "", "")
    del out, err


def test_integrity_reports_dangling_handoff_and_unknown_task(
    run_cli, project_dir
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    (aperio_dir / "runs" / "RUN-0001.handoff.md").unlink()
    report = check_integrity(str(project_dir))
    assert report["ok"] is False
    codes = {finding["code"] for finding in report["errors"]}
    assert "dangling-handoff-ref" in codes

    exec_file = aperio_dir / "executions.json"
    document = json.loads(exec_file.read_text())
    document["runs"][0]["task_id"] = "TASK-999"
    exec_file.write_text(json.dumps(document))
    report = check_integrity(str(project_dir))
    codes = {finding["code"] for finding in report["errors"]}
    assert "orphan-run-task" in codes


def test_integrity_reports_unexpected_files_and_malformed_decisions(
    run_cli, project_dir
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    (aperio_dir / "stray.txt").write_text("unexpected")
    with (aperio_dir / "decisions.md").open("a", encoding="utf-8") as fh:
        fh.write("\n## DEC-9999 - not-a-timestamp - Broken entry\n")
    report = check_integrity(str(project_dir))
    codes = {finding["code"] for finding in report["warnings"]}
    assert "unexpected-file" in codes
    assert "malformed-decisions" in codes
    assert report["ok"] is True


def test_integrity_validates_an_archive(run_cli, project_dir, tmp_path):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    report = check_integrity(str(project_dir), archive_path=archive)
    assert report["archive"]["format_version"] == 1
    assert report["ok"] is True
    archive.write_bytes(b"garbage")
    report = check_integrity(str(project_dir), archive_path=archive)
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "archive-invalid"


# --- upgrade ------------------------------------------------------------------


def test_upgrade_preview_reports_versions_and_writes_nothing(
    run_cli, project_dir
):
    _awaiting_project(run_cli, project_dir)
    before = (project_dir / ".aperio" / "plan.json").read_bytes()
    report = plan_upgrade(str(project_dir))
    assert report["dry_run"] is True and report["applied"] is False
    assert report["ok"] is True
    by_name = {a["artifact"]: a for a in report["artifacts"]}
    assert by_name["state"]["action"] == "none"
    assert by_name["plan"]["current_version"] == PLAN_SCHEMA_VERSION
    assert by_name["handoff"]["current_version"] == "v1"
    assert (project_dir / ".aperio" / "plan.json").read_bytes() == before


def test_upgrade_refuses_future_schema_and_changes_nothing(
    run_cli, project_dir
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    plan_file = aperio_dir / "plan.json"
    document = json.loads(plan_file.read_text())
    document["schema_version"] = 4
    plan_file.write_text(json.dumps(document))
    preview = plan_upgrade(str(project_dir))
    assert preview["ok"] is False
    plan_entry = next(a for a in preview["artifacts"] if a["artifact"] == "plan")
    assert plan_entry["action"] == "blocked"
    with pytest.raises(StateError, match="not possible"):
        apply_upgrade(str(project_dir))
    assert plan_file.read_text() == json.dumps(document)


def test_upgrade_framework_applies_registered_migration_with_backup(
    run_cli, project_dir, monkeypatch
):
    """The framework is exercised with a synthetic, explicitly registered
    edge; production ships no migrations."""
    aperio_dir = _init_project(run_cli, project_dir)
    plan_file = aperio_dir / "plan.json"
    # rewrite the plan to v1 (drop priorities) so the synthetic
    # v1 -> v2 migration has something to migrate; created plans are
    # v2 now
    document = json.loads(plan_file.read_text())
    document["schema_version"] = 1
    for task in document.get("tasks", []):
        task.pop("priority", None)
    plan_file.write_text(json.dumps(document))
    original = plan_file.read_bytes()

    def add_marker(document: dict) -> dict:
        migrated = dict(document)
        migrated["schema_version"] = 2
        migrated["migration_marker"] = "from-v1"
        return migrated

    migration = Migration(
        artifact="plan",
        from_version=1,
        to_version=2,
        description="synthetic v1 -> v2",
        apply=add_marker,
    )
    monkeypatch.setattr(
        "aperio.upgrade.SUPPORTED_SCHEMAS", {"state": 1, "plan": 2,
                                            "executions": 1}
    )
    monkeypatch.setattr("aperio.upgrade.MIGRATIONS", (migration,))
    preview = plan_upgrade(str(project_dir))
    plan_entry = next(a for a in preview["artifacts"] if a["artifact"] == "plan")
    assert plan_entry["action"] == "migrate"
    assert plan_file.read_bytes() == original  # preview is read-only

    report = apply_upgrade(str(project_dir))
    assert report["applied"] is True
    backup = Path(report["backup"])
    assert (backup / "plan.json").read_bytes() == original
    migrated = json.loads(plan_file.read_text())
    assert migrated["schema_version"] == 2
    assert migrated["migration_marker"] == "from-v1"
    assert json.loads((backup / "backup.json").read_text())["artifacts"]["plan"]


# --- CLI smoke ----------------------------------------------------------------


def _run_cli_subprocess(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aperio", *args],
        capture_output=True, text=True, check=False,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def test_cli_smoke_export_import_integrity_upgrade(tmp_path):
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    archive = tmp_path / "smoke.zip"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))

    assert _run_cli_subprocess("init", str(source)).returncode == 0
    assert _run_cli_subprocess(
        "plan-create", str(source), "--file", str(plan_file)
    ).returncode == 0
    assert _run_cli_subprocess("plan-approve", str(source)).returncode == 0
    assert _run_cli_subprocess(
        "run-start", str(source), "TASK-001", "--agent", "manual"
    ).returncode == 0

    exported = _run_cli_subprocess(
        "export", str(source), "--output", str(archive), "--json"
    )
    assert exported.returncode == 0, exported.stderr
    assert json.loads(exported.stdout)["files"]
    assert "aperio: error:" not in exported.stderr

    preview = _run_cli_subprocess(
        "import", str(destination), str(archive), "--dry-run", "--json"
    )
    assert preview.returncode == 0
    assert json.loads(preview.stdout)["created"]
    assert not destination.exists()

    imported = _run_cli_subprocess(
        "import", str(destination), str(archive)
    )
    assert imported.returncode == 0, imported.stderr

    integrity = _run_cli_subprocess("integrity", str(destination), "--json")
    assert integrity.returncode == 0, integrity.stderr
    assert json.loads(integrity.stdout)["ok"] is True

    upgrade = _run_cli_subprocess("upgrade", str(destination), "--json")
    assert upgrade.returncode == 0
    assert json.loads(upgrade.stdout)["ok"] is True

    blocked = _run_cli_subprocess(
        "import", str(destination), str(tmp_path / "missing.zip")
    )
    assert blocked.returncode == 1
    assert "aperio: error:" in blocked.stderr
    assert "Traceback" not in blocked.stderr


def test_import_refuses_ambiguous_uninitialized_aperio_dir(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    (destination / ".aperio").mkdir(parents=True)
    (destination / ".aperio" / "notes.txt").write_text("private")
    with pytest.raises(AperioError, match="incomplete import"):
        import_archive(str(destination), str(archive))
    assert (destination / ".aperio" / "notes.txt").read_text() == "private"
    assert not (destination / ".aperio" / "state.json").exists()


def test_import_rejects_oversized_manifest_entry(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    state_bytes = (project_dir / ".aperio" / "state.json").read_bytes()
    archive = tmp_path / "big.zip"
    _build_archive(
        archive,
        {"state.json": state_bytes},
        manifest={
            "format": portable.EXPORT_FORMAT,
            "format_version": 1,
            "aperio_version": "0.4.0",
            "created_at": "2026-01-01T00:00:00Z",
            "schemas": {"state": 1},
            "files": [
                {"path": "state.json", "size": portable.MAX_ENTRY_BYTES + 1,
                 "sha256": "0" * 64}
            ],
        },
    )
    with pytest.raises(AperioError, match="per-file limit|over the"):
        import_archive(str(tmp_path / "dest"), str(archive))


def test_upgrade_previews_a_future_state_schema(run_cli, project_dir):
    aperio_dir = _init_project(run_cli, project_dir)
    state_file = aperio_dir / "state.json"
    document = json.loads(state_file.read_text())
    document["schema_version"] = 77
    state_file.write_text(json.dumps(document))
    preview = plan_upgrade(str(project_dir))
    assert preview["ok"] is False
    state_entry = next(a for a in preview["artifacts"] if a["artifact"] == "state")
    assert state_entry["action"] == "blocked"
    assert "newer than supported" in state_entry["description"]
    with pytest.raises(StateError, match="not possible"):
        apply_upgrade(str(project_dir))
    assert json.loads(state_file.read_text())["schema_version"] == 77


def test_import_refuses_replacing_a_hardlinked_leaf(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "x.zip"
    _export(project_dir, archive)
    destination = tmp_path / "dest"
    _init_project(run_cli, destination)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-CONTENT", encoding="utf-8")
    brief = destination / ".aperio" / "brief.md"
    brief.unlink()
    os.link(outside, brief)
    with pytest.raises(PathError, match="hard links"):
        import_archive(str(destination), str(archive), replace=True)
    assert outside.read_text() == "OUTSIDE-CONTENT"


# --- release gate: imported execution identity is never trusted ---------------


def _rewrite_archive(archive: Path, mutate) -> None:
    members = _zip_members(archive)
    files = {
        name[len("files/"):]: data
        for name, data in members.items()
        if name.startswith("files/")
    }
    schemas = _manifest_of(archive)["schemas"]
    mutate(files)
    _build_archive(archive, files, schemas=schemas)


@pytest.mark.parametrize("status", ["running", "prepared"])
def test_import_refuses_active_execution_records(
    run_cli, project_dir, tmp_path, status
):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "active.zip"
    _export(project_dir, archive)

    def mutate(files):
        document = json.loads(files["executions.json"])
        document["runs"][0]["status"] = status
        files["executions.json"] = json.dumps(document).encode()

    _rewrite_archive(archive, mutate)
    destination = tmp_path / "dst"
    with pytest.raises(StateError, match="active local execution record"):
        import_archive(str(destination), str(archive))
    assert not (destination / ".aperio" / "state.json").exists()


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("pid", 4242, "machine-local process identity"),
        ("machine", "some-other-host", "machine-local process identity"),
        ("local_log_ref", "local/runs/RUN-0001.log", "machine-local log"),
    ],
)
def test_import_refuses_machine_local_identity(
    run_cli, project_dir, tmp_path, field, value, pattern
):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "identity.zip"
    _export(project_dir, archive)

    def mutate(files):
        document = json.loads(files["executions.json"])
        document["runs"][0][field] = value
        files["executions.json"] = json.dumps(document).encode()

    _rewrite_archive(archive, mutate)
    destination = tmp_path / "dst"
    with pytest.raises(StateError, match=pattern):
        import_archive(str(destination), str(archive))
    assert not (destination / ".aperio" / "state.json").exists()


def test_export_refuses_mismatched_execution_relationship(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    records = aperio_dir / "executions.json"
    document = json.loads(records.read_text())
    document["runs"][0]["task_id"] = "TASK-999"
    records.write_text(json.dumps(document))
    with pytest.raises(StateError, match="cannot export"):
        _export(project_dir, tmp_path / "bad.zip")


def test_cancel_never_signals_a_forged_or_self_pid(
    run_cli, project_dir, monkeypatch
):
    from aperio import runtime

    aperio_dir = _awaiting_project(run_cli, project_dir)
    records = aperio_dir / "executions.json"

    def forge(pid):
        document = json.loads(records.read_text())
        document["runs"][0]["status"] = "running"
        document["runs"][0]["pid"] = pid
        document["runs"][0]["machine"] = runtime.machine_token()
        records.write_text(json.dumps(document))

    calls = []
    monkeypatch.setattr(
        runtime, "_terminate_process", lambda pid: calls.append(pid) or True
    )
    forge(os.getpid())
    with pytest.raises(StateError, match="does not identify a process"):
        runtime.cancel_run(aperio_dir, "RUN-0001")
    forge(1)
    with pytest.raises(StateError, match="does not identify a process"):
        runtime.cancel_run(aperio_dir, "RUN-0001")
    assert calls == []
    assert json.loads(records.read_text())["runs"][0]["status"] == "running"


# --- release gate: merged post-import context --------------------------------


PLAN_V2 = json.loads(json.dumps(PLAN))
PLAN_V2["tasks"][0]["title"] = "portable task (revised)"


def _archive_from(
    tmp_path, run_cli, plan, name="other.zip", revise_plan=None
):
    project = tmp_path / ("other-" + name)
    _init_project(run_cli, project, plan=False)
    plan_file = tmp_path / ("v1-" + name + ".json")
    plan_file.write_text(json.dumps(plan))
    code, _, err = run_cli(
        "plan-create", str(project), "--file", str(plan_file)
    )
    assert code == 0, err
    code, _, err = run_cli("plan-approve", str(project))
    assert code == 0, err
    if revise_plan is not None:
        revised_file = tmp_path / ("v2-" + name + ".json")
        revised_file.write_text(json.dumps(revise_plan))
        code, _, err = run_cli(
            "plan-revise", str(project), "--file", str(revised_file),
            "--revises", "1",
        )
        assert code == 0, err
    archive = tmp_path / name
    _export(project, archive)
    return archive


def _snapshot_destination(project_dir: Path) -> dict:
    return {
        str(path.relative_to(project_dir)): path.read_bytes()
        for path in sorted((project_dir / ".aperio").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def test_import_refuses_plan_that_orphans_retained_execution(
    run_cli, project_dir, tmp_path
):
    _awaiting_project(run_cli, project_dir)
    other = json.loads(json.dumps(PLAN))
    other["tasks"] = [dict(other["tasks"][0], id="TASK-002")]
    archive = _archive_from(tmp_path, run_cli, other)
    before = _snapshot_destination(project_dir)

    with pytest.raises(StateError, match="removed or renamed task"):
        import_archive(str(project_dir), str(archive), replace=True)

    assert _snapshot_destination(project_dir) == before


def test_import_refuses_stale_active_run_across_plan_replacement(
    run_cli, project_dir, tmp_path
):
    _awaiting_project(run_cli, project_dir)
    archive = _archive_from(
        tmp_path, run_cli, PLAN, name="v2.zip", revise_plan=PLAN_V2
    )
    before = _snapshot_destination(project_dir)
    with pytest.raises(StateError, match="replaces the plan at revision 2"):
        import_archive(str(project_dir), str(archive), replace=True)
    assert _snapshot_destination(project_dir) == before


def test_import_refuses_missing_retained_handoff(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    (aperio_dir / "runs" / "RUN-0001.handoff.md").unlink()
    archive = _archive_from(tmp_path, run_cli, PLAN, name="same.zip")
    with pytest.raises(StateError, match="missing handoff"):
        import_archive(str(project_dir), str(archive), replace=True)


def test_import_allows_retained_historical_run_with_compatible_plan(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    records = aperio_dir / "executions.json"
    document = json.loads(records.read_text())
    document["runs"][0]["status"] = "completed"
    document["runs"][0]["outcome"] = "done elsewhere"
    records.write_text(json.dumps(document))
    archive = _archive_from(
        tmp_path, run_cli, PLAN, name="same-v2.zip", revise_plan=PLAN_V2
    )
    report = import_archive(str(project_dir), str(archive), replace=True)
    assert "plan.json" in report["replaced"]
    plan = json.loads((aperio_dir / "plan.json").read_text())
    assert plan["plan_version"] == 2
    assert json.loads(records.read_text())["runs"][0]["status"] == "completed"


# --- release gate: coordination, locking and stale snapshots -----------------


REPO_ROOT = Path(__file__).resolve().parent.parent

LOCK_HOLDER = """
import sys, time
from pathlib import Path
from aperio.locks import acquire_lock, release_lock
project = Path(sys.argv[1])
name = sys.argv[2]
lock = project / ".aperio" / name
token = acquire_lock(lock, timeout=2.0, artifact=name)
print("held", flush=True)
time.sleep(float(sys.argv[3]))
release_lock(lock, token)
"""


def _start_lock_holder(project_dir: Path, lock_name: str, seconds: float):
    proc = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(project_dir), lock_name,
         str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _start_cli(*args: str):
    return subprocess.Popen(
        [sys.executable, "-m", "aperio", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT),
    )


def _assert_clean_finish(proc, label: str) -> subprocess.CompletedProcess:
    out, err = proc.communicate(timeout=60)
    assert "Traceback" not in err, f"{label} raised:\n{err}"
    if proc.returncode != 0:
        assert "aperio: error:" in err, f"{label} failed without a clean error:\n{err}"
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_import_waits_for_and_fails_on_a_held_writer_lock(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "self.zip"
    _export(project_dir, archive)
    before = _snapshot_destination(project_dir)

    holder = _start_lock_holder(project_dir, "plan.lock", 6.0)
    started = time.monotonic()
    result = _run_cli_subprocess("import", str(project_dir), str(archive))
    elapsed = time.monotonic() - started
    holder.communicate(timeout=30)

    assert result.returncode == 1
    assert "aperio: error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "consistent project snapshot" in result.stderr
    assert elapsed >= 4.0, "import must wait for the bounded lock timeout"
    assert _snapshot_destination(project_dir) == before

    # After the writer releases, the same import succeeds (nothing stale).
    retry = _run_cli_subprocess("import", str(project_dir), str(archive))
    assert retry.returncode == 0, retry.stderr


def test_concurrent_import_and_task_status_keep_the_project_valid(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "self.zip"
    _export(project_dir, archive)
    importer = _start_cli("import", str(project_dir), str(archive))
    writer = _start_cli(
        "task-status", str(project_dir), "TASK-001",
        "--status", "in-progress", "--owner", "agent-b",
    )
    _assert_clean_finish(importer, "import")
    _assert_clean_finish(writer, "task-status")
    report = _run_cli_subprocess("integrity", str(project_dir), "--json")
    assert report.returncode == 0, report.stderr


def test_concurrent_import_and_execution_update_never_lose_the_result(
    run_cli, project_dir, tmp_path
):
    aperio_dir = _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "self.zip"
    _export(project_dir, archive)
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps({
        "outcome_reported": "finished concurrently",
        "success_reported": True,
        "diagnostics": "none",
    }))
    importer = _start_cli("import", str(project_dir), str(archive))
    reporter = _start_cli(
        "run-report", str(project_dir), "RUN-0001", "--file", str(report_file)
    )
    _assert_clean_finish(importer, "import")
    reporter_result = _assert_clean_finish(reporter, "run-report")
    if reporter_result.returncode == 0:
        # A completed external result must never be reverted by an import
        # that carried the older awaiting-external snapshot.
        assert read_executions(aperio_dir)["runs"][0]["status"] == "completed"
    assert _run_cli_subprocess("integrity", str(project_dir)).returncode == 0


def test_concurrent_plan_revision_and_import_leave_a_consistent_plan(
    run_cli, project_dir, tmp_path
):
    _init_project(run_cli, project_dir)
    archive = tmp_path / "self.zip"
    _export(project_dir, archive)
    revised = tmp_path / "revised.json"
    revised.write_text(json.dumps(PLAN_V2))
    importer = _start_cli("import", str(project_dir), str(archive))
    reviser = _start_cli(
        "plan-revise", str(project_dir), "--file", str(revised),
        "--revises", "1",
    )
    _assert_clean_finish(importer, "import")
    reviser_result = _assert_clean_finish(reviser, "plan-revise")
    assert reviser_result.returncode in (0, 1)
    plan = read_plan(project_dir / ".aperio")
    assert plan["plan_version"] in (1, 2)
    assert _run_cli_subprocess("integrity", str(project_dir)).returncode == 0


def test_two_concurrent_imports_leave_a_valid_project(
    run_cli, project_dir, tmp_path
):
    _awaiting_project(run_cli, project_dir)
    archive = tmp_path / "self.zip"
    _export(project_dir, archive)
    destination = tmp_path / "newbox"
    first = _start_cli("import", str(destination), str(archive))
    second = _start_cli("import", str(destination), str(archive))
    _assert_clean_finish(first, "import #1")
    _assert_clean_finish(second, "import #2")
    assert (destination / ".aperio" / "state.json").is_file()
    assert _run_cli_subprocess("integrity", str(destination)).returncode == 0

"""Chunk 07 UX: consolidated work-status view and the read-only loopback UI.

The work-status document is the operator's standing answer to "where am
I"; the loopback UI exposes exactly that document over HTTP with a
per-launch token, loopback-only binding, GET-only endpoints, and no
write capability — verified with real HTTP requests against a live
server.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from aperio.cli import main
from aperio.localui import start_ui_server
from aperio.workstatus import (
    build_work_status,
    render_work_status_markdown,
)


HERO_PLAN = {
    "objective": "Implement the Hero Section", "expected_outcome": "Hero ships",
    "scope": ["hero"], "exclusions": [], "assumptions": [], "risks": [],
    "strategy": "small", "acceptance_criteria": ["Hero renders"],
    "verification_strategy": "visual check",
    "tasks": [{
        "id": "TASK-001", "title": "Build the Hero Section markup",
        "objective": "Hero Section", "deliverables": ["hero component"],
        "acceptance_criteria": ["renders"], "depends_on": [],
    }],
}


@pytest.fixture
def active_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    main(["init", str(root)])
    from aperio.planstore import approve_plan, create_plan, update_task

    aperio = root / ".aperio"
    create_plan(aperio, HERO_PLAN)
    approve_plan(aperio)
    update_task(aperio, "TASK-001", new_status="in-progress", owner="a1")
    main(["capture", str(root), "--message",
          "Dark mode might be nice someday.", "--actor", "operator"])
    return aperio


def run_cli(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# the consolidated document
# ---------------------------------------------------------------------------


def test_work_status_consolidates_every_view(active_project: Path) -> None:
    document = build_work_status(active_project)
    content = document["content"]
    assert content["current_work"]["active_task"]["task_id"] == "TASK-001"
    assert content["brain_dump"]["pending_items"], "captured idea pending"
    assert content["brain_dump"]["ideas"], "idea routed as idea-note"
    assert content["current_work"]["approval_status"] == "approved"
    assert content["recovery"]["status"] in ("current", "stale",
                                             "no-checkpoint")
    assert any(a["id"] == "manual" for a in content["agents"])


def test_work_status_markdown_is_human_readable(active_project: Path) -> None:
    markdown = render_work_status_markdown(build_work_status(active_project))
    assert markdown.startswith("# Work status")
    assert "Brain dump" in markdown
    assert "keyword triage, not language understanding" in markdown
    assert "Recovery" in markdown


def test_work_status_reports_verified_separately(active_project: Path) -> None:
    from aperio.planstore import update_task

    update_task(active_project, "TASK-001", new_status="completed",
                evidence="visual check", limitations="none", result="passed")
    content = build_work_status(active_project)["content"]
    assert len(content["execution"]["verification_evidence"]) == 1
    assert content["execution"]["verification_evidence"][0][
        "evidence"] == "visual check"


def test_urgent_capture_surfaces_in_work_status(active_project: Path) -> None:
    main(["capture", str(active_project.parent), "--urgent", "--message",
          "The site is down.", "--actor", "operator"])
    content = build_work_status(active_project)["content"]
    assert content["brain_dump"]["urgent"]
    assert content["brain_dump"]["urgent"][0]["authority"] == \
        "operator-requested"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_work_status_markdown_and_json(active_project: Path,
                                           capsys) -> None:
    root = active_project.parent
    code, out, err = run_cli(capsys, "work-status", str(root))
    assert code == 0, err
    assert out.startswith("# Work status")
    code, out, _ = run_cli(capsys, "work-status", str(root), "--json")
    assert code == 0
    document = json.loads(out)
    assert document["format"] == "aperio-work-status"


def test_cli_work_status_with_skills_store(active_project: Path,
                                           tmp_path: Path, capsys) -> None:
    from aperio.skills import SkillStore

    store = tmp_path / "skills"
    SkillStore(store).import_skill_md(
        "<!-- aperio:skill\nname: test-pack\nversion: 1.0.0\n"
        "description: A test pack.\nscope: general\n"
        "provenance: imported\n-->\nGuidance body.",
        source="test",
    )
    code, out, _ = run_cli(capsys, "work-status", str(active_project.parent),
                           "--skills-store", str(store))
    assert code == 0, "untrusted skills appear in status"
    assert "test-pack" in out
    assert "[untrusted]" in out


# ---------------------------------------------------------------------------
# the read-only loopback UI
# ---------------------------------------------------------------------------


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), response.headers.get(
                "Content-Type", ""
            )
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")


def test_ui_serves_token_protected_readonly_views(active_project: Path) -> None:
    handle = start_ui_server(active_project, port=0)
    try:
        base = f"http://127.0.0.1:{handle['port']}"
        # without the token: 403, on every route
        for path in ("/", "/api/work-status", "/anything"):
            code, _, _ = _get(base + path)
            assert code == 403, path
        # with the token: the views
        code, body, ctype = _get(handle["url"])
        assert code == 200
        assert "text/html" in ctype
        html = body.decode("utf-8")
        assert "Aperio — work status" in html
        assert "TASK-001" in html
        assert "no write endpoints" in html
        assert "<script" not in html

        code, body, ctype = _get(
            f"{base}/{handle['token']}/api/work-status"
        )
        assert code == 200
        assert "application/json" in ctype
        document = json.loads(body)
        assert document["format"] == "aperio-work-status"

        code, _, _ = _get(f"{base}/{handle['token']}/nope")
        assert code == 404
    finally:
        handle["server"].shutdown()


def test_ui_has_no_write_endpoints(active_project: Path) -> None:
    handle = start_ui_server(active_project, port=0)
    try:
        base = f"http://127.0.0.1:{handle['port']}"
        request = urllib.request.Request(
            f"{base}/{handle['token']}/api/work-status", data=b"{}", method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        assert code == 405
    finally:
        handle["server"].shutdown()


def test_ui_token_is_unique_per_launch(active_project: Path) -> None:
    first = start_ui_server(active_project, port=0)
    second = start_ui_server(active_project, port=0)
    try:
        assert first["token"] != second["token"]
        assert first["port"] != second["port"]
    finally:
        first["server"].shutdown()
        second["server"].shutdown()


def test_ui_binds_loopback_only(active_project: Path) -> None:
    handle = start_ui_server(active_project, port=0)
    try:
        host, port = handle["server"].server_address
        assert host == "127.0.0.1", "the UI must never bind a public interface"
    finally:
        handle["server"].shutdown()


def test_ui_refuses_invalid_port(active_project: Path) -> None:
    with pytest.raises(Exception):
        start_ui_server(active_project, port=99999)

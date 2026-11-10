"""Host compatibility doctor: honest levels, reserved-level invariant,
probe integration, and the CLI surface."""

from __future__ import annotations

import json

from aperio.hostcompat import (
    COMPATIBILITY_LEVELS,
    EMITTED_LEVELS,
    HOST_CATALOG,
    RESERVED_LEVELS,
    inspect_hosts,
)


def _probe(**overrides) -> dict:
    report = {
        "adapter_id": "codex-cli",
        "installed": True,
        "executable": "/usr/local/bin/codex",
        "version": "1.2.3",
        "version_error": None,
        "installation_error": None,
        "missing_flags": [],
        "missing_controls": [],
        "capability_error": None,
    }
    report.update(overrides)
    return report


# --- vocabulary and catalog -----------------------------------------------------------


def test_level_vocabulary_matches_directive_scheme():
    assert set(COMPATIBILITY_LEVELS) == {
        "native_verified",
        "cli_verified",
        "cooperative_verified",
        "experimental",
        "unsupported",
        "not_yet_tested",
    }
    assert set(RESERVED_LEVELS) == {"native_verified", "experimental"}
    assert set(EMITTED_LEVELS) == set(COMPATIBILITY_LEVELS) - set(RESERVED_LEVELS)


def test_catalog_covers_documented_hosts_without_invented_apis():
    hosts = {entry["host"] for entry in HOST_CATALOG}
    assert {
        "terminal-cli", "codex-cli", "claude-cli", "opencode", "cline",
        "kilo-code", "zcode", "pi-coding-agent", "goose",
        "google-antigravity", "cursor",
    } <= hosts
    # no entry claims a native slash-command registration mechanism
    for entry in HOST_CATALOG:
        method = entry["integration_method"].lower()
        assert "slash" not in method
        if entry["family"] == "cooperative":
            assert "none documented in aperio" in method


def test_reserved_levels_are_never_emitted():
    for probes in (None, {}, {"codex-cli": _probe(),
                              "claude-cli": _probe(adapter_id="claude-cli")}):
        report = inspect_hosts(probes=probes)
        levels = {row["level"] for row in report["hosts"]}
        assert levels <= set(EMITTED_LEVELS)
        assert "native_verified" not in levels
        assert "experimental" not in levels


# --- rows ------------------------------------------------------------------------------


def test_terminal_cli_is_cli_verified():
    report = inspect_hosts()
    row = next(r for r in report["hosts"] if r["host"] == "terminal-cli")
    assert row["level"] == "cli_verified"
    assert "runtime-enforced" in row["evidence"]


def test_provider_host_without_probe_is_not_yet_tested():
    report = inspect_hosts()
    row = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert row["level"] == "not_yet_tested"
    assert "--probe" in row["evidence"]
    assert row["executable"] is None


def test_provider_host_with_clean_probe_is_cli_verified():
    report = inspect_hosts(probes={
        "codex-cli": _probe(),
        "claude-cli": None,
    })
    codex = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert codex["level"] == "cli_verified"
    assert codex["version"] == "1.2.3"
    assert "no live model request" in codex["evidence"]
    claude = next(r for r in report["hosts"] if r["host"] == "claude-cli")
    assert claude["level"] == "not_yet_tested"


def test_installed_but_incompatible_provider_is_unsupported():
    report = inspect_hosts(probes={
        "codex-cli": _probe(missing_flags=["--json"]),
        "claude-cli": _probe(adapter_id="claude-cli"),
    })
    codex = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert codex["level"] == "unsupported"
    assert "--json" in codex["evidence"]


def test_missing_provider_executable_is_not_yet_tested():
    report = inspect_hosts(probes={
        "codex-cli": _probe(installed=False, executable=None,
                            installation_error="not found"),
        "claude-cli": None,
    })
    codex = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert codex["level"] == "not_yet_tested"
    assert "not found" in codex["evidence"]


def test_cooperative_hosts_are_not_yet_tested_with_fallback():
    report = inspect_hosts()
    for row in report["hosts"]:
        if row["family"] != "cooperative":
            continue
        assert row["level"] == "not_yet_tested"
        assert "per-host live verification" in row["evidence"]
        assert "run 'aperio' commands in a terminal" in row["fallback"]


def test_report_notes_distinguish_level_meanings():
    report = inspect_hosts()
    joined = " ".join(report["notes"])
    assert "not interchangeable" in joined
    assert "never" in joined


# --- CLI surface ------------------------------------------------------------------------


def test_cli_host_doctor_json_and_text(run_cli):
    code, out, err = run_cli("host-doctor", "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["command"] == "host-doctor"
    assert len(report["hosts"]) == len(HOST_CATALOG)
    assert report["probes_performed"] == []

    code, out, _ = run_cli("host-doctor")
    assert code == 0
    assert "compatibility matrix" in out
    assert "Terminal / canonical CLI" in out
    assert "OpenCode" in out
    assert "Reserved levels" in out


def test_cli_host_doctor_probe_wiresthrough_provider_doctor(
    run_cli, monkeypatch
):
    """The --probe path reuses agent-doctor's passive diagnostics and
    feeds the reports into the matrix (proven with a fake probe)."""
    import aperio.cli as cli_module

    calls = []

    def fake_doctor(adapter_id, *, config, aperio_dir):
        calls.append(adapter_id)
        return _probe(adapter_id=adapter_id)

    monkeypatch.setattr(cli_module, "provider_doctor", fake_doctor)
    code, out, err = run_cli("host-doctor", "--probe", "--json")
    assert code == 0, err
    report = json.loads(out)
    assert sorted(calls) == ["claude-cli", "codex-cli"]
    assert sorted(report["probes_performed"]) == ["claude-cli", "codex-cli"]
    codex = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert codex["level"] == "cli_verified"


def test_cli_host_doctor_probe_failure_degrades_honestly(run_cli, monkeypatch):
    import aperio.cli as cli_module

    def failing_doctor(adapter_id, *, config, aperio_dir):
        from aperio.errors import ValidationError
        raise ValidationError(f"{adapter_id} unavailable")

    monkeypatch.setattr(cli_module, "provider_doctor", failing_doctor)
    code, out, err = run_cli("host-doctor", "--probe", "--json")
    assert code == 0, err  # diagnostic degrades, never crashes
    report = json.loads(out)
    assert report["probes_performed"] == []
    codex = next(r for r in report["hosts"] if r["host"] == "codex-cli")
    assert codex["level"] == "not_yet_tested"

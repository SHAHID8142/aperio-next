"""Host compatibility doctor: honest per-host integration levels.

Aperio is agent-agnostic by architecture, but honesty comes first: a
host is only ever reported at a level that has actually been verified.
This module produces the compatibility matrix from two inputs — the
documented host catalog below and (optionally) passive probe reports
from :func:`aperio.adapters.provider_doctor` — and never invents an
integration interface it has not verified.

The directive-level vocabulary is defined in full; two levels are
**reserved** and never emitted by this version:

- ``native_verified`` — reserved: no verified native integration
  mechanism (slash-command registration, embedded extension) exists for
  any host, and aperio does not invent one.
- ``experimental`` — reserved: nothing experimental ships.

What the doctor CAN verify today:

- ``cli_verified`` — the canonical terminal CLI (aperio is that CLI;
  the runtime-enforced path), and a provider CLI whose executable was
  found and whose documented flags/controls all probe cleanly.
- ``not_yet_tested`` — a host with no aperio adapter (cooperative
  fallback available: the host runs the canonical CLI; the JSON
  protocols are host-agnostic) or a provider CLI that is not installed
  (install it, then re-run with --probe).
- ``unsupported`` — a provider CLI that is installed but does not
  advertise the documented controls aperio relies on.

Pure function over the catalog + probe results: filesystem checks live
in the CLI layer (:func:`aperio.adapters.provider_doctor`), which keeps
this module deterministic and testable without any host installed.
"""

from __future__ import annotations

COMPATIBILITY_LEVELS: tuple[str, ...] = (
    "native_verified",
    "cli_verified",
    "cooperative_verified",
    "experimental",
    "unsupported",
    "not_yet_tested",
)

#: Levels this version never emits, with the reason. Honest reserves:
#: claiming them without a verified mechanism would be fabrication.
RESERVED_LEVELS: dict[str, str] = {
    "native_verified": (
        "no verified native integration mechanism exists for any host "
        "(slash-command registration, embedded extension); aperio does "
        "not invent undocumented host APIs"
    ),
    "experimental": "nothing experimental ships in this version",
}

#: Levels the doctor may emit (the complement of the reserved set).
EMITTED_LEVELS: tuple[str, ...] = (
    "cli_verified",
    "cooperative_verified",
    "not_yet_tested",
    "unsupported",
)

#: The documented host catalog. ``adapter_id`` names the aperio adapter
#: that can drive the host's documented CLI; hosts without one are
#: cooperative-fallback rows. No entry claims an undocumented interface.
HOST_CATALOG: tuple[dict, ...] = (
    {
        "host": "terminal-cli",
        "display": "Terminal / canonical CLI (any OS)",
        "family": "canonical",
        "adapter_id": None,
        "integration_method": (
            "aperio IS this CLI — the runtime-enforced path every other "
            "row falls back to"
        ),
        "fallback": None,
    },
    {
        "host": "codex-cli",
        "display": "OpenAI Codex CLI",
        "family": "provider-cli",
        "adapter_id": "codex-cli",
        "integration_method": (
            "driven through the documented 'codex exec --json' CLI via "
            "the codex-cli adapter"
        ),
        "fallback": "manual handoff or run the canonical CLI yourself",
    },
    {
        "host": "claude-cli",
        "display": "Claude Code (CLI mode)",
        "family": "provider-cli",
        "adapter_id": "claude-cli",
        "integration_method": (
            "driven through the documented 'claude -p' CLI via the "
            "claude-cli adapter"
        ),
        "fallback": "manual handoff or run the canonical CLI yourself",
    },
    {
        "host": "opencode",
        "display": "OpenCode",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "cline",
        "display": "Cline",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "kilo-code",
        "display": "Kilo Code",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "zcode",
        "display": "ZCode",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "pi-coding-agent",
        "display": "Pi coding agent",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "goose",
        "display": "Goose",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "google-antigravity",
        "display": "Google Antigravity",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
    {
        "host": "cursor",
        "display": "Cursor",
        "family": "cooperative",
        "adapter_id": None,
        "integration_method": (
            "none documented in aperio; cooperative fallback — the host "
            "runs the canonical CLI commands and reads the host-agnostic "
            "JSON protocols"
        ),
        "fallback": "run 'aperio' commands in a terminal; exchange via "
                    "'aperio exchange-export/import', 'aperio agent-brief', "
                    "and the onboarding --json protocol",
    },
)

_COOPERATIVE_FALLBACK = (
    "run 'aperio' commands in a terminal; exchange via 'aperio "
    "exchange-export/import', 'aperio agent-brief', and the onboarding "
    "--json protocol"
)


def _verification_dimensions(row: dict) -> dict:
    """Break a compatibility row into independently true/false facts.

    Finding A10: the single ``level`` label cannot distinguish "the binary
    exists" from "a live, authenticated model call succeeded". These
    dimensions are separate so no reader has to infer one from the other.
    Today no host has a live functional handoff recorded, so every dimension
    past ``executable_present`` is honestly false or unknown.
    """
    family = row.get("family")
    present = family == "canonical" or bool(row.get("executable"))
    return {
        "executable_present": present,
        "supported_flags_detected": family == "canonical"
        or row.get("level") == "cli_verified",
        "cli_launch_tested": False,
        "authenticated_model_call_tested": False,
        "functional_handoff_tested": False,
        "native_command_registered_and_tested": False,
    }


def inspect_hosts(*, probes: dict[str, dict | None] | None = None) -> dict:
    """Build the compatibility matrix (pure; no filesystem access).

    ``probes`` maps an adapter id to a provider_doctor-style report, or
    to None when probing was requested but produced no report. Without
    probes, adapter-backed hosts are reported ``not_yet_tested`` with
    the instruction to re-run with a probe — the doctor never guesses
    installation from a stale assumption.

    Invariant (tested): no row ever carries a reserved level.
    """
    probes = probes or {}
    rows: list[dict] = []
    for entry in HOST_CATALOG:
        adapter_id = entry["adapter_id"]
        if entry["family"] == "canonical":
            rows.append(
                {
                    "host": entry["host"],
                    "display": entry["display"],
                    "family": entry["family"],
                    "level": "cli_verified",
                    "integration_method": entry["integration_method"],
                    "evidence": (
                        "the doctor itself runs inside the canonical CLI; "
                        "this is the runtime-enforced path"
                    ),
                    "executable": None,
                    "version": None,
                    "fallback": None,
                }
            )
            continue
        if entry["family"] == "provider-cli":
            probe = probes.get(adapter_id)
            row = {
                "host": entry["host"],
                "display": entry["display"],
                "family": entry["family"],
                "integration_method": entry["integration_method"],
                "executable": None,
                "version": None,
                "fallback": entry["fallback"],
            }
            if probe is None:
                row.update(
                    {
                        "level": "not_yet_tested",
                        "evidence": (
                            "no probe was performed; re-run 'aperio "
                            "host-doctor --probe' for a passive "
                            "executable/flags check (never a login or "
                            "model request)"
                        ),
                    }
                )
            elif probe.get("installed"):
                blocking = []
                if probe.get("missing_flags"):
                    blocking.append(
                        "missing documented flags: "
                        + ", ".join(probe["missing_flags"])
                    )
                if probe.get("missing_controls"):
                    blocking.append(
                        "missing documented controls: "
                        + ", ".join(probe["missing_controls"])
                    )
                if probe.get("capability_error"):
                    blocking.append(str(probe["capability_error"]))
                if blocking:
                    row.update(
                        {
                            "level": "unsupported",
                            "evidence": "; ".join(blocking)
                            + " — the installed CLI does not advertise "
                            "the documented interface aperio relies on",
                        }
                    )
                else:
                    row.update(
                        {
                            "level": "cli_verified",
                            "evidence": (
                                "executable found and its documented "
                                "flags/controls probe cleanly "
                                "(passive check; no live model request)"
                            ),
                        }
                    )
            else:
                installation = (
                    probe.get("installation_error") or "none reported"
                )
                row.update(
                    {
                        "level": "not_yet_tested",
                        "evidence": (
                            "executable not found on this machine "
                            "(installation_error: "
                            f"{installation}); install the CLI and "
                            "re-run with --probe"
                        ),
                    }
                )
            row["executable"] = probe.get("executable") if probe else None
            row["version"] = probe.get("version") if probe else None
            rows.append(row)
            continue
        # cooperative family: aperio-side protocol is tested; per-host
        # verification would require running inside that host — not
        # claimed.
        rows.append(
            {
                "host": entry["host"],
                "display": entry["display"],
                "family": entry["family"],
                "level": "not_yet_tested",
                "integration_method": entry["integration_method"],
                "evidence": (
                    "aperio's side of the cooperative protocol is tested "
                    "(host-agnostic JSON); per-host live verification "
                    "has not been performed"
                ),
                "executable": None,
                "version": None,
                "fallback": entry["fallback"] or _COOPERATIVE_FALLBACK,
            }
        )
    emitted = {row["level"] for row in rows}
    unexpected = emitted - set(EMITTED_LEVELS)
    if unexpected:  # pragma: no cover - guarded by construction + tests
        raise AssertionError(
            f"compatibility report emitted reserved level(s): {unexpected}"
        )
    for row in rows:
        row["verification"] = _verification_dimensions(row)
    return {
        "command": "host-doctor",
        "levels": list(COMPATIBILITY_LEVELS),
        "reserved_levels": RESERVED_LEVELS,
        "hosts": rows,
        "probes_performed": sorted(
            adapter for adapter, report in probes.items() if report is not None
        ),
        "notes": [
            "Levels are not interchangeable: 'not_yet_tested' means no "
            "evidence was gathered, 'unsupported' means evidence showed "
            "the documented interface is unavailable, and a probe proves "
            "only that a binary runs — never that a session works.",
            "No live integration test was performed by this diagnostic; "
            "native/CLI/cooperative distinctions come from documented "
            "interfaces only.",
            "Per-host 'verification' records six separate facts. Only "
            "'executable_present' and 'supported_flags_detected' can be "
            "true from passive probing; 'cli_launch_tested', "
            "'authenticated_model_call_tested', 'functional_handoff_tested' "
            "and 'native_command_registered_and_tested' are false until a "
            "live run is actually recorded.",
        ],
    }


__all__ = [
    "COMPATIBILITY_LEVELS",
    "EMITTED_LEVELS",
    "HOST_CATALOG",
    "RESERVED_LEVELS",
    "inspect_hosts",
]

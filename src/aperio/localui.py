"""Optional read-only loopback UI (serve-ui).

A minimal local viewer for the consolidated work-status document:
stdlib ``http.server`` bound to **127.0.0.1 only**, a per-launch random
token in the URL (the sole credential — without it every request is
403), GET-only endpoints, no write endpoints, no filesystem operations
beyond reading the project's own state, and no remote assets. The
security posture is deliberate: a server that cannot mutate anything
cannot need CSRF tokens, and loopback-only binding plus the URL token
keeps it invisible to other machines and other local users' browsers
are the documented residual risk (same-machine processes can read the
token from the command line — documented, not hidden).

Endpoints:

* ``GET /<token>/``            — the HTML view (semantic, no JS);
* ``GET /<token>/api/work-status`` — the JSON document;
* anything else                — 404; without the token — 403.

The UI is explicitly optional: the CLI (`aperio work-status`) renders
the same document without any server. No accessibility claim beyond
semantic HTML with real headings/lists/tables is made; the JSON feed is
the interface of record.
"""

from __future__ import annotations

import functools
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aperio.errors import ValidationError
from aperio.workstatus import (
    build_work_status,
    render_work_status_markdown,
)


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


def _html_escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def _render_html(document: dict) -> str:
    """Semantic HTML from the work-status document. No scripts, no
    remote assets; every value is escaped."""

    content = document["content"]
    work = content["current_work"]
    e = _html_escape

    def section(title: str, body: str) -> str:
        if not body.strip():
            return ""
        return f"<section><h2>{e(title)}</h2>{body}</section>"

    def ul(items: list[str]) -> str:
        if not items:
            return ""
        return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"

    current_items: list[str] = []
    if work["active_task"]:
        current_items.append(
            f"Current task: <strong>{e(work['active_task']['task_id'])}</strong>"
            f" — {e(work['active_task'].get('title') or '')}"
        )
    else:
        current_items.append("Current task: none")
    for run in work["active_runs"]:
        current_items.append(
            f"Active run <code>{e(run['run_id'])}</code> "
            f"({e(run['status'])}) on {e(run['task_id'])}"
        )
    if work["approval_status"]:
        current_items.append(f"Plan approval: {e(work['approval_status'])}")
    current_items.append(f"Next: {e(work['next_action'])}")

    brain_items: list[str] = []
    for item in content["brain_dump"]["urgent"]:
        brain_items.append(
            f"<strong>URGENT</strong> {e(item['text'][:120])} "
            f"(<code>{e(item['capture_id'])}[{item['item_index']}]</code>)"
        )
    for item in content["brain_dump"]["pending_items"]:
        brain_items.append(
            f"{e(item['category'])} → {e(item['routing'])}: "
            f"{e(item['text'][:120])} "
            f"(<code>{e(item['capture_id'])}[{item['item_index']}]</code>)"
        )

    approval_items = [
        f"<code>{e(r['review_id'])}</code> on {e(r['task_id'])} "
        f"(by {e(r.get('submitted_by') or '?')})"
        for r in content["approvals"]["pending_reviews"]
    ]
    evidence_items = [
        f"<code>{e(t['task_id'])}</code> — evidence: {e(t.get('evidence') or '')}"
        for t in content["execution"]["verification_evidence"]
    ]
    unverified_items = [e(s) for s in content["execution"]["unverified_signals"]]
    outcome_items = [
        f"<code>{e(r['run_id'])}</code> task {e(r['task_id'])} — "
        f"{e(r['status'])}"
        for r in content["execution"]["last_outcomes"]
    ]
    recovery = content["recovery"]
    recovery_items = [
        f"Checkpoint: <strong>{e(recovery['status'])}</strong>",
        f"Resume: <strong>"
        f"{'eligible' if recovery['resume_eligible'] else 'gated'}</strong>",
        *[e(reason) for reason in recovery["reasons"]],
    ]
    agent_items = [
        f"<code>{e(a['id'])}</code> — "
        f"{'available' if a['available'] else 'unavailable'}"
        + (f" ({e(a['note'])})" if a["note"] else "")
        for a in content["agents"]
    ]
    agent_items += [
        f"skill <code>{e(s['name'])}</code> v{e(s['version'])} "
        f"[{e(s['state'])}]"
        for s in content["skills"]
    ]

    parts = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\">",
        "<title>Aperio — work status</title>",
        "<style>body{font-family:sans-serif;max-width:60rem;margin:0 auto;"
        "padding:1rem}code{background:#eee;padding:0 .2em}"
        "section{margin:1.5rem 0}</style></head><body>",
        "<h1>Aperio — work status</h1>",
        "<p><em>Read-only view; the CLI (<code>aperio work-status</code>) "
        "renders the same document. This server has no write "
        "endpoints.</em></p>",
        section("Current work", ul(current_items)),
        section("Brain dump (pending review)", ul(brain_items)),
        section("Awaiting independent review", ul(approval_items)),
        section("Verified completions", ul(evidence_items)),
        section("Unverified signals (not evidence)", ul(unverified_items)),
        section("Recent run outcomes", ul(outcome_items)),
        section("Recovery", ul(recovery_items)),
        section("Agents and skills", ul(agent_items)),
        "<footer><p>Bound to 127.0.0.1 only; token-protected; GET-only."
        "</p></footer></body></html>",
    ]
    return "\n".join(parts)


def make_handler(aperio_dir: Path, token: str,
                 skill_store: Path | None = None):
    """Build a request-handler class bound to one project and token."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "aperio-ui/1"

        def log_message(self, format, *args) -> None:  # noqa: A002
            # No request logging: the token would land in the output.
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            prefix = "/" + token
            if not self.path.startswith(prefix):
                self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
                return
            route = self.path[len(prefix):] or "/"
            try:
                document = build_work_status(aperio_dir,
                                             skill_store=skill_store)
            except Exception as exc:  # noqa: BLE001 - report, never leak
                body = json.dumps({"error": str(exc)[:200]}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            if route in ("/", "/index.html"):
                body = _render_html(document).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif route == "/api/work-status":
                body = json.dumps(document, ensure_ascii=False,
                                  indent=2).encode("utf-8")
                self._send(200, body, "application/json")
            elif route == "/api/work-status/markdown":
                body = render_work_status_markdown(document).encode("utf-8")
                self._send(200, body, "text/markdown; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            # Deliberate: no write endpoints exist at all.
            self._send(405, b"method not allowed\n",
                       "text/plain; charset=utf-8")

    return Handler


def serve_ui(aperio_dir: Path, *, port: int = 0,
             skill_store: Path | None = None) -> dict:
    """Start the read-only loopback UI. Blocks the calling thread until
    interrupted; returns the bound address/token for the caller to
    print BEFORE serving (via ``start_ui_server``)."""

    raise NotImplementedError(
        "use start_ui_server, which returns the bound address and token "
        "without blocking the caller's error reporting"
    )


def start_ui_server(aperio_dir: Path, *, port: int = 0,
                    skill_store: Path | None = None) -> dict:
    """Create and start the server; returns handle info without blocking
    (the caller keeps the returned server object referenced and calls
    ``server.shutdown()`` to stop it)."""

    if not isinstance(port, int) or isinstance(port, bool) or not (
        0 <= port <= 65535
    ):
        raise ValidationError("serve-ui: port must be 0-65535")
    token = _generate_token()
    handler = make_handler(aperio_dir, token, skill_store=skill_store)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    # ThreadingHTTPServer.daemon_threads defaults True on 3.7+; keep it so
    # a Ctrl-C in the CLI shuts down cleanly.
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return {
        "server": server,
        "thread": thread,
        "token": token,
        "host": "127.0.0.1",
        "port": actual_port,
        "url": f"http://127.0.0.1:{actual_port}/{token}/",
    }

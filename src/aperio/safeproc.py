"""Bounded, cross-platform subprocess helpers and output redaction.

This module holds the small set of primitives that every external-process
call site in Aperio shares so that limits are enforced *while* output is
produced, not after it has already been buffered into memory.

Three problems this module exists to prevent:

1. **Unbounded capture.** ``subprocess.run(capture_output=True)`` followed
   by ``out[:LIMIT]`` buffers the whole stream first; a flooding child can
   exhaust memory before the slice happens. ``run_captured`` drains both
   pipes incrementally (reader threads) and stops appending at the limit
   while still draining, so the child never blocks on a full pipe.
2. **Platform-specific termination.** ``os.killpg`` does not exist on
   Windows. ``stop_process_tree`` only signals the group on POSIX and falls
   back to ``terminate``/``kill`` elsewhere.
3. **Secret persistence.** External tools print tokens, keys, and local
   paths. ``redact_output`` removes credential-shaped substrings and the
   caller decides where (if anywhere) the raw text is stored.

Nothing here opens a shell, and no argument list is ever re-parsed as a
command string.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time

# Credential-shaped substrings that must never reach a shareable record.
# Each replacement keeps a short, non-reversible hint so a human can tell
# *that* something was removed without learning *what* it was.
_SECRET_RES = (
    # Common provider key prefixes.
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # Bearer / basic auth material.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    # key=value style assignments with a long opaque value.
    re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|password|passwd|token|credential)"
        r"[A-Za-z0-9_.-]*)\s*[:=]\s*['\"]?[A-Za-z0-9+/_.~-]{12,}['\"]?"
    ),
    # URL-embedded credentials: scheme://user:pass@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
)

REDACTED = "<redacted>"


def redact_output(text: str, *, home: str | None = None) -> str:
    """Remove credential-shaped substrings and the user's home path.

    ``home`` (when supplied) is replaced with ``~`` so absolute paths in
    machine-local diagnostics do not leak usernames into shareable state.
    Redaction is best-effort and pattern-based; it is a data-minimisation
    control, not a formal proof of secrecy.
    """
    if not text:
        return ""
    out = text
    for pattern in _SECRET_RES:
        # For patterns with a leading key group, keep the key name.
        if pattern.groups and "://" in pattern.pattern:
            out = pattern.sub(lambda m: m.group(1) + REDACTED + "@", out)
        elif "key|secret|password" in pattern.pattern:
            out = pattern.sub(lambda m: m.group(1) + "=" + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    if home:
        trimmed = home.rstrip(os.sep)
        if trimmed and trimmed != os.sep:
            out = out.replace(trimmed, "~")
    return out


def tail_bytes(data: bytes, limit: int, *, home: str | None = None) -> str:
    """Return the redacted tail of ``data`` as text, at most ``limit`` chars."""
    if not data or limit <= 0:
        return ""
    text = data[-limit:].decode("utf-8", errors="replace")
    return redact_output(text, home=home)


def stop_process_tree(process: subprocess.Popen, *, force: bool) -> None:
    """Stop a child and, on POSIX, its own process group.

    Children started with ``start_new_session=True`` have a process-group
    id equal to their pid. ``os.killpg``/``os.getpgid`` are POSIX-only, so
    the group signal is guarded; Windows and any failure fall back to the
    per-process ``terminate``/``kill`` calls.
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


def run_captured(
    argv: list[str] | tuple[str, ...],
    *,
    timeout_seconds: float,
    capture_limit: int,
    cwd: str | None = None,
    env: dict | None = None,
    home: str | None = None,
) -> dict:
    """Run one child process with genuinely bounded, redacted output capture.

    Both stdout and stderr are drained by reader threads so a chatty child
    cannot deadlock on a full pipe; bytes beyond ``capture_limit`` (per
    stream) are counted but discarded. On timeout the whole process tree is
    terminated and ``timed_out`` is set.

    Returns a dict with: ``returncode``, ``timed_out``, ``stdout``,
    ``stderr``, ``stdout_bytes``, ``stderr_bytes``, ``truncated``,
    ``duration``. Text fields are already redacted.
    """
    limit = max(0, int(capture_limit))
    counts = {"stdout": 0, "stderr": 0}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    lock = threading.Lock()

    def drain(stream, key: str) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with lock:
                    counts[key] += len(chunk)
                    room = limit - len(buffers[key])
                    if room > 0:
                        buffers[key].extend(chunk[:room])
                    if len(chunk) > room:
                        truncated[key] = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=(os.name == "posix"),
        shell=False,
    )
    threads = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process_tree(process, force=True)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    join_deadline = time.monotonic() + 5.0
    for thread in threads:
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    if any(t.is_alive() for t in threads):
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
        for thread in threads:
            thread.join(timeout=1.0)

    duration = round(time.monotonic() - started, 2)
    with lock:
        out_text = redact_output(
            buffers["stdout"].decode("utf-8", errors="replace"), home=home
        )
        err_text = redact_output(
            buffers["stderr"].decode("utf-8", errors="replace"), home=home
        )
        result = {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout": out_text,
            "stderr": err_text,
            "stdout_bytes": counts["stdout"],
            "stderr_bytes": counts["stderr"],
            "truncated": truncated["stdout"] or truncated["stderr"],
            "duration": duration,
        }
    return result


def read_text_bounded(path, limit: int) -> tuple[str, bool]:
    """Read at most ``limit`` bytes from ``path`` without loading the rest.

    Returns ``(text, truncated)``. Raises ``OSError`` on read failure so the
    caller can decide how to report it. Never reads more than ``limit + 1``
    bytes, so an enormous file cannot exhaust memory.
    """
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")
    return text, truncated

"""Bounded retry and problem-signature recovery (anti-loop protection).

Agents must never remain trapped in repeated blind-fix loops. This module
enforces the bounded problem-solving policy at the execution boundary:

- **Signature keying.** A failed run is keyed by a deterministic
  ``failure_signature`` digest computed from what the shareable record
  preserves — task id, adapter id, exit code, and the recorded outcome
  text (whitespace-normalized, bounded). The same failure re-run yields
  the same signature, so re-running the same failing command, changing
  wording, or making an equivalent edit can never reset the counter.
  Older records without a stored signature are re-derived from their
  recorded fields, so the budget is continuous across versions.

- **Budget.** At most :data:`MAX_ATTEMPTS_PER_SIGNATURE` (3) attempts
  per signature. The dispatch gate refuses a 4th attempt with escalation
  guidance: mark the task blocked, preserve the debugging history
  (``aperio recovery-report``), change the approach materially, switch
  adapter after ``agent-doctor``, or escalate to the operator. The gate
  is enforced in the Python boundary (:mod:`aperio.runtime`), not by
  prompt instructions. An exhausted task therefore *surfaces* —
  including during ``runs-dispatch`` auto-scheduling, which stops with
  the escalation message instead of silently looping.

- **Meaningful retries only.** A retry (attempt 2+) must declare a
  remediation record (``--remediation``): the changed hypothesis or the
  new evidence justifying another attempt. Repeating an identical
  remediation note for the same signature is refused as not materially
  distinct.

- **No new state store.** The ledger is derived from the append-only
  execution records (failed runs are never overwritten), so there is no
  second source of truth and no migration.

Honest limitation: signatures key on what records preserve. Two
*different* root causes that surface as the same recorded outcome text
and exit code share a budget; two presentations of the *same* root
cause (different exit codes or outcome text) do not. The signature is a
coordination boundary, not a root-cause analyzer — the
``recovery-report`` history is the evidence a human reads.
"""

from __future__ import annotations

import hashlib

from aperio.errors import StateError, ValidationError

MAX_ATTEMPTS_PER_SIGNATURE = 3

#: Hard ceiling on *all* failed attempts for one task, regardless of how the
#: error text, exit code or adapter varies between attempts. Signatures can
#: be reset by genuinely different-looking failures (or an adapter switch);
#: this task-level budget prevents that from becoming an unbounded loop.
MAX_ATTEMPTS_PER_TASK = 8

#: Bounded excerpt of a recorded outcome used for signature text.
_SIGNATURE_TEXT_LIMIT = 280

#: Bounded excerpt rendered per run in recovery reports.
_REPORT_EXCERPT_LIMIT = 200


def normalize_signature_text(text: str) -> str:
    """Collapse whitespace and bound the text used for a signature."""
    return " ".join(str(text).split())[:_SIGNATURE_TEXT_LIMIT]


def failure_signature(
    task_id: str, adapter_id: str, *, outcome: str, exit_code: int | None
) -> str:
    """Deterministic 16-hex problem signature for one recorded failure.

    Keys only on facts the shareable record already preserves (never raw
    argv, config, or environment): task, adapter, exit code, and the
    normalized outcome text.
    """
    material = "|".join(
        (
            task_id,
            adapter_id,
            "none" if exit_code is None else str(exit_code),
            normalize_signature_text(outcome or ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def signature_of_run(run: dict) -> str:
    """Signature for one stored run: the stored digest when present, else
    re-derived from the record's own fields (pre-signature records)."""
    stored = run.get("failure_signature")
    if isinstance(stored, str) and stored:
        return stored
    return failure_signature(
        str(run.get("task_id", "")),
        str(run.get("adapter_id", "")),
        outcome=str(run.get("outcome") or ""),
        exit_code=run.get("exit_code"),
    )


def failed_runs_for_task(runs: list[dict], task_id: str) -> list[dict]:
    """Failed runs for one task in record (append) order."""
    return [
        run
        for run in runs
        if run.get("task_id") == task_id and run.get("status") == "failed"
    ]


def failure_counts(runs: list[dict], task_id: str) -> dict[str, int]:
    """Failed-run count per problem signature for one task."""
    counts: dict[str, int] = {}
    for run in failed_runs_for_task(runs, task_id):
        counts[signature_of_run(run)] = counts.get(signature_of_run(run), 0) + 1
    return counts


def retry_gate(
    runs: list[dict],
    task_id: str,
    *,
    remediation: str | None,
) -> dict:
    """Evaluate the bounded-retry gate for dispatching ``task_id``.

    Returns ``{"signature", "attempt_no", "prior_failures"}`` for the
    problem being retried (``signature`` None on a fresh task). Raises:

    - :class:`StateError` when the signature's budget is exhausted (the
      escalation message carries the run ids, the recorded outcome, and
      the safe alternatives);
    - :class:`ValidationError` when a required remediation record is
      missing or repeats a previous attempt's note verbatim.
    """
    failed = failed_runs_for_task(runs, task_id)
    if not failed:
        return {"signature": None, "attempt_no": 1, "prior_failures": 0}

    # Task-level ceiling: bounded even when error text, exit code, or the
    # adapter changes between attempts and resets the per-signature counter.
    if len(failed) >= MAX_ATTEMPTS_PER_TASK:
        raise StateError(
            f"task {task_id}: the task-level retry ceiling is exhausted — "
            f"{len(failed)} failed attempts across all problem signatures "
            f"(limit {MAX_ATTEMPTS_PER_TASK}). Varying error text, exit codes "
            "or adapters does not earn more attempts. Safe next steps: mark "
            f"the task blocked ('aperio task-status <dir> {task_id} "
            "--status blocked --reason \"exhausted task retry ceiling\"'), "
            f"preserve the history ('aperio recovery-report <dir> {task_id}'), "
            "then change the approach materially, revise the plan, or "
            "escalate to the operator. Nothing was modified."
        )

    latest = failed[-1]
    signature = signature_of_run(latest)
    same = [run for run in failed if signature_of_run(run) == signature]
    prior = len(same)

    if prior >= MAX_ATTEMPTS_PER_SIGNATURE:
        run_ids = ", ".join(str(run.get("run_id")) for run in same)
        outcome = normalize_signature_text(str(latest.get("outcome") or ""))[
            :_REPORT_EXCERPT_LIMIT
        ]
        raise StateError(
            f"task {task_id}: the bounded-retry budget is exhausted — "
            f"{prior} failed attempts share problem signature {signature} "
            f"({run_ids}; latest outcome: {outcome!r}). Stop retrying this "
            f"problem. Safe next steps: mark the task blocked "
            f"('aperio task-status <dir> {task_id} --status blocked "
            f"--reason \"exhausted retry budget for signature {signature}\"'), "
            "preserve the debugging history ('aperio recovery-report "
            f"<dir> {task_id}', 'aperio decision-add'), then change the "
            "approach materially (a new plan revision), switch adapter "
            "after 'aperio agent-doctor', or escalate to the operator. "
            "Nothing was modified."
        )

    prior_notes = [
        str(run.get("remediation_note"))
        for run in same
        if isinstance(run.get("remediation_note"), str)
        and run.get("remediation_note").strip()
    ]
    if remediation is None or not remediation.strip():
        raise ValidationError(
            f"task {task_id} already has {prior} failed attempt(s) with "
            f"problem signature {signature}; a retry must declare what "
            "changed: pass --remediation with the new evidence or changed "
            "root-cause hypothesis (re-running the same fix is not a "
            "meaningful retry). Nothing was modified."
        )
    note = remediation.strip()
    if note in prior_notes:
        raise ValidationError(
            f"task {task_id}: the declared remediation repeats an earlier "
            f"attempt's note for signature {signature}; a retry must be "
            "materially distinct (new evidence or a changed hypothesis), "
            "not re-run with equivalent wording. Nothing was modified."
        )
    return {
        "signature": signature,
        "attempt_no": prior + 1,
        "prior_failures": prior,
    }


def attach_failure_signature(
    prior_runs: list[dict],
    *,
    task_id: str,
    adapter_id: str,
    outcome: str,
    exit_code: int | None,
) -> dict:
    """Signature fields for a just-failed run's record.

    ``prior_runs`` is the runs list read before publishing the finalized
    record; the returned ``failure_attempt`` counts prior failed runs
    sharing this signature (best-effort under concurrency: the byte-CAS
    protects the write, and a rare stale count only mislabels a number,
    never enables an extra attempt — the dispatch gate recounts).
    """
    signature = failure_signature(
        task_id, adapter_id, outcome=outcome or "", exit_code=exit_code
    )
    prior = sum(
        1
        for run in prior_runs
        if run.get("task_id") == task_id
        and run.get("status") == "failed"
        and signature_of_run(run) == signature
    )
    return {"failure_signature": signature, "failure_attempt": prior + 1}


def recovery_report(runs: list[dict], task_id: str) -> dict:
    """Read-only debugging history for one task's failures, grouped by
    problem signature, with the honest verdict and escalation guidance."""
    failed = failed_runs_for_task(runs, task_id)
    groups: dict[str, list[dict]] = {}
    for run in failed:
        groups.setdefault(signature_of_run(run), []).append(run)
    signatures: list[dict] = []
    for signature, same in groups.items():
        signatures.append(
            {
                "signature": signature,
                "attempts": len(same),
                "exhausted": len(same) >= MAX_ATTEMPTS_PER_SIGNATURE,
                "runs": [
                    {
                        "run_id": run.get("run_id"),
                        "adapter_id": run.get("adapter_id"),
                        "exit_code": run.get("exit_code"),
                        "attempt": run.get("failure_attempt"),
                        "finished_at": run.get("finished_at"),
                        "outcome": str(run.get("outcome") or "")[
                            :_REPORT_EXCERPT_LIMIT
                        ],
                        "remediation_note": (
                            None
                            if not isinstance(
                                run.get("remediation_note"), str
                            )
                            or not run.get("remediation_note").strip()
                            else str(run.get("remediation_note"))[
                                :_REPORT_EXCERPT_LIMIT
                            ]
                        ),
                    }
                    for run in same
                ],
            }
        )
    exhausted = [entry for entry in signatures if entry["exhausted"]]
    return {
        "task_id": task_id,
        "max_attempts_per_signature": MAX_ATTEMPTS_PER_SIGNATURE,
        "signature_count": len(signatures),
        "failed_run_count": len(failed),
        "signatures": signatures,
        "verdict": "exhausted" if exhausted else (
            "failed_runs_present" if failed else "no_failed_runs"
        ),
        "guidance": (
            [
                "At least one problem signature has exhausted its retry "
                "budget: stop retrying it. Mark the task blocked with "
                f"'aperio task-status <dir> {task_id} --status blocked "
                "--reason ...', record the debugging history "
                "('aperio decision-add'), change the approach materially, "
                "or switch adapter after 'aperio agent-doctor'."
            ]
            if exhausted
            else (
                [
                    "Failures are recorded; retries require a declared "
                    "--remediation (new evidence or a changed hypothesis) "
                    "and are capped at "
                    f"{MAX_ATTEMPTS_PER_SIGNATURE} per signature."
                ]
                if failed
                else [
                    "No failed runs are recorded for this task; nothing "
                    "to recover."
                ]
            )
        ),
    }


__all__ = [
    "MAX_ATTEMPTS_PER_SIGNATURE",
    "attach_failure_signature",
    "failed_runs_for_task",
    "failure_counts",
    "failure_signature",
    "normalize_signature_text",
    "recovery_report",
    "retry_gate",
    "signature_of_run",
]

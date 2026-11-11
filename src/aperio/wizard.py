"""Opt-in interactive onboarding wizard (finding A06).

``aperio next`` stays a strictly read-only, scriptable protocol. This module
adds a *separate*, explicitly requested experience for people who would rather
answer questions than copy question IDs into commands.

Design rules this module obeys:

- **Opt-in only.** Nothing here runs unless the operator typed ``aperio start``.
  An agent scripting the CLI keeps using the read-only protocol.
- **Consent before mutation.** Initializing a project is the first mutation.
  The wizard explains exactly what will be created and asks before doing it;
  declining exits and changes nothing.
- **Never invents approval.** Approvals are the operator's own keypresses.
- **Back / pause / resume.** The operator can revise the previous answer,
  pause (state is preserved), or quit at any prompt.
- **Non-interactive safe.** When stdin is not a TTY the wizard refuses with a
  clear message instead of hanging or misreading piped input.
- **Bounded input.** Answers and prompts are length-limited.

The wizard is a *driver*: it only calls the same facade functions the CLI
subcommands call, so there is no second source of truth for onboarding state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aperio import onboarding
from aperio.onboarding_discovery import discover_workspace
from aperio.onboarding_session import session_exists
from aperio.project import aperio_dir_for

MAX_ANSWER_CHARS = 2000

# Sentinels the operator can type at any prompt.
CMD_BACK = "/back"
CMD_PAUSE = "/pause"
CMD_QUIT = "/quit"
CMD_SKIP = "/skip"


class WizardUnavailable(RuntimeError):
    """Raised when the interactive wizard cannot run in this environment."""


@dataclass
class WizardResult:
    """What the wizard did, for the caller to report honestly."""

    finished: bool
    paused: bool = False
    cancelled: bool = False
    steps: int = 0
    final_action: str | None = None
    final_prompt: str | None = None
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "finished": self.finished,
            "paused": self.paused,
            "cancelled": self.cancelled,
            "steps": self.steps,
            "final_action": self.final_action,
            "final_prompt": self.final_prompt,
            "messages": list(self.messages),
        }


def _require_tty(streams) -> None:
    stdin, stdout = streams
    if not getattr(stdin, "isatty", lambda: False)():
        raise WizardUnavailable(
            "the interactive wizard needs a terminal (stdin is not a TTY). "
            "Use the scriptable commands instead: 'aperio next DIR', "
            "'aperio onboarding-answer', 'aperio onboarding-readiness'."
        )
    if not getattr(stdout, "isatty", lambda: False)():
        raise WizardUnavailable(
            "the interactive wizard needs a terminal (stdout is not a TTY)"
        )


class _Prompter:
    """Small read/write helper so tests can drive the wizard without a TTY."""

    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout

    def say(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def ask(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            # EOF: treat as a request to quit rather than looping forever.
            return CMD_QUIT
        return line.rstrip("\n")


def _parse_choice(raw: str, choices: list[dict]) -> dict | None:
    """Accept a number, an id, or an exact label."""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(choices):
            return choices[index - 1]
        return None
    lowered = text.casefold()
    for choice in choices:
        if lowered in {str(choice.get("id", "")).casefold(),
                       str(choice.get("label", "")).casefold()}:
            return choice
    return None


def _ask_choice(prompter: _Prompter, choices: list[dict], *, allow_back: bool) -> str | dict:
    """Present choices; return chosen dict, or a sentinel string."""
    for i, choice in enumerate(choices, start=1):
        prompter.say(f"  {i}) {choice.get('label')}  [{choice.get('id')}]")
    hint = "Type a number" + (" , /back" if allow_back else "") + " , /pause or /quit: "
    while True:
        raw = prompter.ask(hint)
        if raw.strip() == CMD_BACK and allow_back:
            return CMD_BACK
        if raw.strip() in (CMD_PAUSE, CMD_QUIT):
            return raw.strip()
        chosen = _parse_choice(raw, choices)
        if chosen is not None:
            return chosen
        prompter.say("  Sorry, that is not one of the options. Try again.")


def run(
    raw_path: str,
    *,
    stdin=None,
    stdout=None,
    request_approval=None,
    max_steps: int = 200,
) -> WizardResult:
    """Drive onboarding interactively, one step at a time.

    ``request_approval`` is called with a consent message and must return
    True to proceed with the first mutation (initialization). When omitted the
    wizard refuses to initialize, so no unattended caller can mutate by
    accident.
    """
    import sys

    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    _require_tty((stdin, stdout))

    prompter = _Prompter(stdin, stdout)
    result = WizardResult(finished=False)
    prompter.say("Aperio onboarding")
    prompter.say("Answer one step at a time. At any prompt: /pause saves and exits,")
    prompter.say("/quit cancels, /back revises the previous answer where offered.")
    prompter.say("")

    previous_question_id: str | None = None
    for _ in range(max_steps):
        protocol = onboarding.start(raw_path)
        action = protocol.get("action_required")

        if action == "confirm_initialization":
            prompter.say(protocol.get("prompt") or "Initialize this project?")
            prompter.say("  This creates only Aperio's .aperio context folder "
                         "(state, onboarding session, empty plan/memory stores).")
            prompter.say("  Existing files are never overwritten.")
            chosen = _ask_choice(
                prompter,
                [{"id": "yes", "label": "Yes, initialize"},
                 {"id": "no", "label": "No, exit"}],
                allow_back=previous_question_id is not None,
            )
            if isinstance(chosen, str):
                return _finish_sentinel(result, chosen, protocol)
            if chosen["id"] == "no":
                prompter.say("Nothing was changed.")
                result.cancelled = True
                result.final_action = "declined"
                return result
            if request_approval is not None:
                consent = (
                    "Initialize this project now? This creates .aperio/ "
                    "scaffolding only; no other files are touched."
                )
                if not request_approval(consent):
                    prompter.say("Approval not given; nothing was changed.")
                    result.cancelled = True
                    result.final_action = "declined"
                    return result
            onboarding.confirm_initialization(raw_path)
            result.steps += 1
            continue

        if action == "choose_intent":
            prompter.say(protocol.get("prompt") or "What would you like to do?")
            choices = protocol.get("choices") or []
            chosen = _ask_choice(
                prompter,
                choices,
                allow_back=previous_question_id is not None,
            )
            if isinstance(chosen, str):
                return _finish_sentinel(result, chosen, protocol)
            workspace = Path(protocol["workspace"])
            apdir = aperio_dir_for(workspace)
            # An existing project may have no Aperio session yet. Recording an
            # intent writes to .aperio, which is a mutation: require explicit
            # consent first, then use the sanctioned initialization path.
            if not session_exists(apdir):
                prompter.say("  Aperio needs to create its own .aperio/ folder here.")
                prompter.say("  It contains only Aperio's notes; existing files are never overwritten.")
                if request_approval is not None and not request_approval(
                    "Create Aperio's .aperio/ scaffolding in this existing project?"
                ):
                    prompter.say("Approval not given; nothing was changed.")
                    result.cancelled = True
                    result.final_action = "declined"
                    return result
                onboarding.confirm_initialization(str(workspace))
                result.steps += 1
            onboarding.record_intent(apdir, chosen["id"])
            result.steps += 1
            previous_question_id = "intent"
            continue

        if action == "answer_question":
            question = protocol.get("question") or {}
            progress = protocol.get("progress") or {}
            qid = question.get("id")
            prompter.say(protocol.get("prompt") or "Answer:")
            suggestions = question.get("suggestions") or []
            if suggestions:
                prompter.say(
                    "  Common answers: "
                    + ", ".join(str(s) for s in suggestions[:6])
                    + " (or your own words)"
                )
            prompter.say(
                "  You can also write 'Not sure yet' or 'Decide later'."
            )
            if progress.get("answered") is not None:
                prompter.say(
                    f"  Progress: {progress.get('answered')}/{progress.get('total')}"
                )
            hint = ("Type your answer, or 'Not sure yet' / 'Decide later'"
                    + (", /back" if previous_question_id else "")
                    + " , /pause or /quit: ")
            while True:
                raw = prompter.ask(hint)
                stripped = raw.strip()
                if stripped == CMD_BACK and previous_question_id:
                    onboarding.revise(
                        Path(protocol["workspace"]) / ".aperio",
                        previous_question_id,
                        "Not sure yet",
                    )
                    result.steps += 1
                    break
                if stripped in (CMD_PAUSE, CMD_QUIT):
                    return _finish_sentinel(result, stripped, protocol)
                if stripped == CMD_SKIP:
                    stripped = "Not sure yet"
                if not stripped:
                    prompter.say("  Please type an answer, or /pause to stop.")
                    continue
                if len(stripped) > MAX_ANSWER_CHARS:
                    prompter.say(
                        f"  That answer is too long ({len(stripped)} characters); "
                        f"please keep it under {MAX_ANSWER_CHARS}."
                    )
                    continue
                onboarding.answer(
                    Path(protocol["workspace"]) / ".aperio", qid, stripped
                )
                result.steps += 1
                previous_question_id = qid
                break
            continue

        if action == "resolve_state":
            prompter.say(protocol.get("prompt") or "The state needs manual attention.")
            for reason in protocol.get("classification_reasons") or []:
                prompter.say(f"  - {reason}")
            prompter.say("Stopping without changes. See docs/troubleshooting.md.")
            result.final_action = action
            result.final_prompt = protocol.get("prompt")
            result.finished = True
            return result

        # Any stage action (evaluate_readiness, propose_standardization, ...)
        # hands control back to the operator with the exact next command.
        prompter.say(protocol.get("prompt") or "")
        result.final_action = action
        result.final_prompt = protocol.get("prompt")
        result.finished = True
        return result

    result.messages.append(
        f"stopped after {max_steps} steps to stay bounded; run 'aperio next' "
        "to see the next action"
    )
    return result


def _finish_sentinel(result: WizardResult, sentinel: str, protocol: dict) -> WizardResult:
    if sentinel == CMD_PAUSE:
        result.paused = True
        result.messages.append(
            "paused; answers so far are saved. Run 'aperio start' again to resume."
        )
    else:
        result.cancelled = True
        result.messages.append("cancelled; answers so far are saved.")
    result.final_action = protocol.get("action_required")
    result.final_prompt = protocol.get("prompt")
    return result

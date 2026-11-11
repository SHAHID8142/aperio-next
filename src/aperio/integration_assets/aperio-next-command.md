---
description: Run the Aperio Next engine for this project: read-only next-action, then follow its onboarding or resume protocol.
argument-hint: "[project directory] (defaults to the current workspace)"
---

Aperio Next slash-command entry point (thin wrapper; the full protocol lives
in the `aperio-next` skill).

1. Let `PROJECT` be the argument the user passed, else the current workspace
   root. Never guess a parent directory.
2. Run the canonical read-only entry point (adjust the path if the engine is
   not on PATH — the installer prints it, typically
   `~/.aperio-next/bin/aperio`):

   ```
   aperio next "$$PROJECT" --json
   ```

3. The JSON document names the project state and exactly one next action.
   Follow that action. It never initializes or modifies anything by itself:
   - New/unmanaged project → walk the user through onboarding; nothing is
     written until the user explicitly confirms initialization.
   - Aperio-managed project → load `aperio memory "$$PROJECT"` and
     `aperio work-status "$$PROJECT"`, run `aperio recover "$$PROJECT"` if
     the last session was interrupted, and resume inside the approved plan.
4. If the engine is not installed, tell the user to run
   `npx aperio-next install` and stop.

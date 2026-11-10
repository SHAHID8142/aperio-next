<!-- aperio:skill
name: release-rollback
version: 1.0.0
description: Release checklist: configuration, backups, rollback plan, dry-run versus real deployment, and authorization gates.
scope: release
depends_on:
conflicts:
provenance: aperio-builtin-v1
-->
Before any release: confirm configuration for the target environment, verify backups exist and are restorable, and write a rollback plan with concrete steps and a decision point for invoking it. Distinguish dry-run from real deployment explicitly; a dry run proves nothing about production until executed against production-like state. Treat deployment as gated: proceed only with explicit operator authorization, never on assumption. Record what was released, when, and how to revert before calling the release complete.

<!-- aperio:skill
name: security-review
version: 1.0.0
description: Pre-release security review: injection, authorization gaps, secret leakage, dependency exposure, transport and configuration.
scope: security
depends_on:
conflicts:
provenance: aperio-builtin-v1
-->
Before release, review for injection (SQL, command, template), authorization gaps on every endpoint and object, secret leakage in logs, configs, and client bundles, dependency exposure from unpinned or known-vulnerable packages, and weak transport or configuration defaults (TLS, cookies, headers, debug modes). Report each finding with evidence: the exact location, the condition that triggers it, and a severity you can justify. Never claim a clean bill beyond what was actually checked; list uncovered areas instead.

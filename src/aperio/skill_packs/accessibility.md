<!-- aperio:skill
name: accessibility
version: 1.0.0
description: WCAG-oriented accessibility checks: contrast, keyboard paths, labels, focus order, alt text, and honest limits of automated checks.
scope: accessibility
depends_on:
conflicts:
provenance: aperio-builtin-v1
-->
Check WCAG-oriented basics: contrast ratios, complete keyboard paths, visible focus indicators, programmatic labels on every control, logical focus order, and meaningful alt text (empty alt for decorative content). Verify nothing requires hover-only or pointer-only input. State limits honestly: automated checks miss judgment calls such as alt-text quality, sensible reading order, and cognitive load; route those to human review. Do not claim conformance beyond the checks actually performed.

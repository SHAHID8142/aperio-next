<!-- aperio:skill
name: frontend-engineering
version: 1.0.0
description: Frontend implementation guidance for component architecture, routing, and responsive UI work.
scope: frontend
depends_on: design-systems, accessibility
conflicts:
provenance: aperio-builtin-v1
-->
Structure the UI as components with one responsibility each; keep routing declarative and state local by default, lifting it only when shared. Implement responsive behavior from the agreed breakpoints; do not invent new ones. Consume design tokens instead of hard-coded values, and carry accessibility decisions (labels, focus order, keyboard paths) into the implementation rather than deferring them. Before handoff, run regression checks: routes render, states switch, layout holds at each breakpoint. Do not patch design gaps with ad-hoc styles; raise them.

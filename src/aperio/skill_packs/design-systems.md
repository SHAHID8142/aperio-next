<!-- aperio:skill
name: design-systems
version: 1.0.0
description: Design system discipline: tokens, typography, color and contrast, component states, and spacing rules.
scope: design-systems
depends_on: ux-ia
conflicts:
provenance: aperio-builtin-v1
-->
Define tokens first: typography scale, color palette, spacing rhythm, and radii; reference tokens instead of raw values everywhere. Check every text and background pair for contrast, not only the primary ones. Specify component states (default, hover, focus, active, disabled, error) before styling variants. Keep spacing on the established scale; do not introduce one-off gaps. Do not add fonts or colors outside the agreed palette; propose them as token changes instead.

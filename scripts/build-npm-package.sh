#!/usr/bin/env bash
# Build the npm launcher package: wheel into packaging/npm/engine/, then npm pack.
# Dev-machine build step (not part of the runtime); requires uv (or any wheel
# builder) and npm.
set -euo pipefail
cd "$(dirname "$0")/.."

ENGINE_DIR="packaging/npm/engine"
mkdir -p "$ENGINE_DIR"
rm -f "$ENGINE_DIR"/*.whl

if command -v uv >/dev/null 2>&1; then
  uv build --wheel --out-dir "$ENGINE_DIR" . >/dev/null
else
  .venv/bin/python -m pip wheel --no-deps --no-build-isolation -w "$ENGINE_DIR" . >/dev/null
fi

WHEEL=$(ls "$ENGINE_DIR"/*.whl | head -1)
echo "bundled engine wheel: $WHEEL"

# Stage outside the git worktree: npm pack consults git when packing inside
# a repository, which fails on machines without push access configured.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/bin"
cp packaging/npm/package.json packaging/npm/README.md "$STAGE/"
cp packaging/npm/bin/aperio-next.js "$STAGE/bin/"
cp "$ENGINE_DIR"/*.whl "$STAGE/engine-wheel-copy.tmp"
mkdir -p "$STAGE/engine"
mv "$STAGE/engine-wheel-copy.tmp" "$STAGE/engine/$(basename "$WHEEL")"

mkdir -p dist
( cd "$STAGE" && npm pack --pack-destination "$OLDPWD/dist" )
echo "packed: $(ls dist/aperio-next-*.tgz)"

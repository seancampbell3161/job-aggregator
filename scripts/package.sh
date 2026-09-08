#!/usr/bin/env bash
#
# Build a Lambda deployment zip into build/lambda.zip.
# Vendors dependencies into build/pkg/ and adds src/ + config.yaml.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
PKG="$BUILD/pkg"
ZIP="$BUILD/lambda.zip"

rm -rf "$BUILD"
mkdir -p "$PKG"

# Install runtime deps only (PEP 621 main dependencies).
python3.12 -m pip install \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --target "$PKG" \
  --upgrade \
  --no-compile \
  -r <(python3.12 -c "import tomllib,sys;d=tomllib.load(open('$ROOT/pyproject.toml','rb'));print('\n'.join(d['project']['dependencies']))")

# Copy source + config.
cp -R "$ROOT/src" "$PKG/"
cp "$ROOT/config.yaml" "$PKG/"
cp "$ROOT/profile.md" "$PKG/"
# resume.md is gitignored (PII) and optional; only present when gap_analysis is used.
[ -f "$ROOT/resume.md" ] && cp "$ROOT/resume.md" "$PKG/" || true

# Strip caches.
find "$PKG" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$PKG" -type d -name '*.dist-info' -prune -exec rm -rf {} + || true

(cd "$PKG" && zip -qr "$ZIP" .)
echo "Built $ZIP ($(du -h "$ZIP" | cut -f1))"

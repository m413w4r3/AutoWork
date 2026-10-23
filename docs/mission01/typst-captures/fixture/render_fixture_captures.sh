#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
CAPTURE_ROOT="$REPO_ROOT/docs/mission01/typst-captures"
REFERENCE_ROOT="/home/nill/perso/work/AutoWork/docs/references"
TYPST_BINARY="${TYPST_BINARY:-/snap/typst/current/bin/typst}"
PYTHON="$REPO_ROOT/backend/.venv/bin/python"

export TMPDIR="$HOME/tmp"
mkdir -p "$TMPDIR"
mkdir -p "$CAPTURE_ROOT/current" "$CAPTURE_ROOT/historical" \
  "$CAPTURE_ROOT/historical-pipeline"

PYTHONPATH="$REPO_ROOT/backend/src" "$PYTHON" -B \
  "$CAPTURE_ROOT/fixture/render_fixture_inputs.py"

WORK="$(mktemp -d /tmp/m01t-parity.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

cp -a "$REPO_ROOT/chpTypst" "$WORK/chpTypst"
cp "$CAPTURE_ROOT/fixture/current/render-data.json" \
  "$WORK/chpTypst/RENDERER/render-data.json"
cp "$CAPTURE_ROOT/fixture/current/fixture.svg" \
  "$WORK/chpTypst/RENDERER/fixture.svg"

"$TYPST_BINARY" compile \
  --root "$WORK/chpTypst" \
  --font-path "$REFERENCE_ROOT/chpTypst/HankenGrotesk" \
  "$WORK/chpTypst/RENDERER/publication.typ" \
  "$WORK/current.pdf"

"$TYPST_BINARY" compile \
  --root / \
  --font-path "$REFERENCE_ROOT/chpTypst/HankenGrotesk" \
  "$CAPTURE_ROOT/fixture/historical/article.typ" \
  "$WORK/historical.pdf"

"$TYPST_BINARY" compile \
  --root / \
  --font-path "$REFERENCE_ROOT/chpTypst/HankenGrotesk" \
  "$CAPTURE_ROOT/fixture/historical-pipeline/article.typ" \
  "$WORK/historical-pipeline.pdf"

for variant in current historical historical-pipeline; do
  find "$CAPTURE_ROOT/$variant" -maxdepth 1 -name 'page-*.png' -delete
  pdftoppm -png -r 140 "$WORK/$variant.pdf" \
    "$CAPTURE_ROOT/$variant/page" >/dev/null 2>&1
done

M01T_PDF_ROOT="$WORK" PYTHONPATH="$REPO_ROOT/backend/src" "$PYTHON" -B \
  "$CAPTURE_ROOT/fixture/inspect_fixture_outputs.py"

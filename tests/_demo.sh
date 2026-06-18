#!/usr/bin/env bash
# Smoke demo: build fixtures, run the CLI, dump key fields of two sidecars. Not part of the test suite.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$HOME/repos/sv-kb/pipeline/shared/.venv/bin/python}"
OUT=/tmp/tfe_demo
rm -rf "$OUT"
export PYTHONPATH="$HERE"
"$PY" "$HERE/tests/make_fixtures.py" "$OUT" >/dev/null
"$PY" -m src.stage1 "$OUT" --generated-at 2026-06-15T00:00:00Z -j 2
echo
"$PY" "$HERE/tests/_inspect.py" "$OUT"

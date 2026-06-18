#!/usr/bin/env bash
# Convenience runner: executes Stage 1 with the sv-kb venv Python (which already has PyMuPDF 1.27.2).
# Usage:  ./run.sh <ROOT> [flags]          e.g.  ./run.sh ~/data/epstein/DataSet-12 -j 6
#         PYTHON=/path/to/python ./run.sh ...   to override the interpreter
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$HOME/repos/sv-kb/pipeline/shared/.venv/bin/python}"
export PYTHONPATH="$HERE:${PYTHONPATH:-}"
exec "$PYTHON" -m src.stage1 "$@"

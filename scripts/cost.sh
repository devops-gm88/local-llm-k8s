#!/usr/bin/env bash
# Build the break-even table and chart from the committed benchmark results.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r bench/requirements.txt
exec .venv/bin/python cost/model.py "$@"

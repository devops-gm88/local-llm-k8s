#!/usr/bin/env bash
# Unit tests for the gateway. No cluster or model required.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r gateway/requirements-dev.txt
cd gateway
exec ../.venv/bin/python -m pytest -q tests "$@"

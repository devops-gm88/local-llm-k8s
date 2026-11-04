#!/usr/bin/env bash
# Reproduce the benchmark table. Requires a running cluster (make up).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_kubeconfig

cd "$REPO_ROOT"
if [ ! -x .venv/bin/python ]; then
  echo "==> creating venv at .venv"
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r bench/requirements.txt

BASE_URL="$(gateway_url)"
if [ "$BASE_URL" = "http://127.0.0.1:18080" ]; then
  ensure_port_forward 18080
fi

export BASE_URL GATEWAY_TOKEN="$(gateway_token)" KUBECONFIG
exec .venv/bin/python bench/run_bench.py "$@"

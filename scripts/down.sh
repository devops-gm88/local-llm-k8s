#!/usr/bin/env bash
# Remove the workloads. The VM keeps running so `make up` is fast again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
if [ -s "$KUBECONFIG" ]; then
  "$KUBECTL" delete -k "$REPO_ROOT/manifests/local" --ignore-not-found --wait=true || true
  "$KUBECTL" delete pvc ollama-models -n "$NAMESPACE" --ignore-not-found || true
else
  echo "==> no kubeconfig; nothing to delete"
fi
if [ -f "$REPO_ROOT/.kube/port-forward.pid" ]; then
  kill "$(cat "$REPO_ROOT/.kube/port-forward.pid")" 2>/dev/null || true
  rm -f "$REPO_ROOT/.kube/port-forward.pid"
fi
echo "==> workloads deleted (VM left running; 'make stop' shuts it down)"

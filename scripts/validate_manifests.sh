#!/usr/bin/env bash
# Render every overlay and validate it with kubeconform.
# Deliberately standalone: CI runs this on a runner with kubectl and
# kubeconform but no Lima, so it must not source scripts/lib.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${KUBECTL:-}" ]; then
  if [ -x "$REPO_ROOT/.tools/bin/kubectl" ]; then
    KUBECTL="$REPO_ROOT/.tools/bin/kubectl"   # local checkout, no PATH needed
  else
    KUBECTL="kubectl"                          # CI installs it on PATH
  fi
fi
KUBECONFORM="${KUBECONFORM:-${REPO_ROOT}/.tools/bin/kubeconform}"

command -v "$KUBECTL" >/dev/null 2>&1 || { echo "error: kubectl not found" >&2; exit 1; }
if [ ! -x "$KUBECONFORM" ] && ! command -v "$KUBECONFORM" >/dev/null 2>&1; then
  echo "error: kubeconform not found. Run 'make tools' or install it." >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
status=0
for overlay in local kind; do
  rendered="$tmpdir/$overlay.yaml"
  echo "==> rendering manifests/$overlay"
  "$KUBECTL" kustomize "$REPO_ROOT/manifests/$overlay" > "$rendered"
  echo "==> validating manifests/$overlay"
  if ! "$KUBECONFORM" -strict -summary "$rendered"; then
    status=1
  fi
done
exit "$status"

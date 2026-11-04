#!/usr/bin/env bash
# Render every overlay and validate it with kubeconform. CI runs this too.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

KUBECONFORM="${KUBECONFORM:-}"
if [ -z "$KUBECONFORM" ]; then
  KUBECONFORM="$(pick_bin kubeconform "$REPO_ROOT/.tools/bin/kubeconform")"
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

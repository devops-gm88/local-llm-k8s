#!/usr/bin/env bash
# Delete the VM. Downloaded base images stay in Lima's cache.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_lima
read -r -p "Delete VM '$VM_NAME' and its model cache? [y/N] " answer
case "$answer" in
  y|Y) "$LIMACTL" stop "$VM_NAME" 2>/dev/null || true
       "$LIMACTL" delete "$VM_NAME";;
  *) echo "==> aborted";;
esac

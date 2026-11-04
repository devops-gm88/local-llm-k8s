#!/usr/bin/env bash
# Shut the VM down but keep it (and the model cache) for next time.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
if vm_running; then
  "$LIMACTL" stop "$VM_NAME"
else
  echo "==> VM '$VM_NAME' is not running"
fi

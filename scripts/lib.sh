#!/usr/bin/env bash
# Shared helpers for the local-llm-k8s scripts. Source this file; do not run it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

VM_NAME="${VM_NAME:-llmk8s}"
NAMESPACE="${NAMESPACE:-llm}"
export VM_NAME NAMESPACE

# Keep VM state inside the repo so the build is self-contained.
export LIMA_HOME="${LIMA_HOME:-$REPO_ROOT/.lima}"

# Lima caches images under ~/Library/Caches. In a sandboxed session that path
# is not writable, so fall back to a repo-local HOME. Nothing else changes.
if ! ( : > "${HOME}/.local-llm-k8s-write-test" ) 2>/dev/null; then
  export HOME="$REPO_ROOT/.home"
  mkdir -p "$HOME"
else
  rm -f "${HOME}/.local-llm-k8s-write-test"
fi

pick_bin() {
  local name="$1"; shift
  for candidate in "$@"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return 0; fi
  echo "error: '$name' not found. Run 'make tools' or install it." >&2
  return 1
}

LIMACTL="$(pick_bin limactl "$REPO_ROOT/.tools/bin/limactl" /opt/homebrew/bin/limactl /usr/local/bin/limactl)"
KUBECTL="$(pick_bin kubectl "$REPO_ROOT/.tools/bin/kubectl" /opt/homebrew/bin/kubectl /usr/local/bin/kubectl)"
export LIMACTL KUBECTL

mkdir -p "$REPO_ROOT/.kube"
export KUBECONFIG="${KUBECONFIG:-$REPO_ROOT/.kube/config}"

VM_IP_FILE="$REPO_ROOT/.kube/vm-ip"
GATEWAY_TOKEN_FILE="$REPO_ROOT/.kube/gateway-token"

in_vm() { "$LIMACTL" shell "$VM_NAME" -- bash -lc "$*"; }

vm_exists() {
  "$LIMACTL" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$VM_NAME"
}

vm_running() {
  "$LIMACTL" list 2>/dev/null | awk -v n="$VM_NAME" 'NR>1 && $1==n {print $2}' | grep -qx "Running"
}

vm_ip() {
  in_vm "hostname -I" 2>/dev/null | awk '{print $1}'
}

need_kubeconfig() {
  if [ ! -s "$KUBECONFIG" ]; then
    echo "error: no kubeconfig at $KUBECONFIG. Run 'make up' first." >&2
    exit 1
  fi
}

k() { "$KUBECTL" -n "$NAMESPACE" "$@"; }

gateway_token() {
  if [ -f "$GATEWAY_TOKEN_FILE" ]; then cat "$GATEWAY_TOKEN_FILE"; fi
}

gateway_url() {
  local vm_ip
  vm_ip="$(cat "$VM_IP_FILE" 2>/dev/null || true)"
  if [ -n "$vm_ip" ] && curl -fsS --max-time 2 "http://${vm_ip}:30080/healthz" >/dev/null 2>&1; then
    echo "http://${vm_ip}:30080"
  elif [ -n "${GATEWAY_BASE_URL:-}" ]; then
    echo "$GATEWAY_BASE_URL"
  else
    echo "http://127.0.0.1:18080"   # kubectl port-forward fallback
  fi
}

# Port-forward used only when the host cannot reach the VM NodePort directly.
ensure_port_forward() {
  local port="${1:-18080}"
  if curl -fsS --max-time 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    return 0
  fi
  echo "==> starting kubectl port-forward on 127.0.0.1:${port}" >&2
  "$KUBECTL" -n "$NAMESPACE" port-forward "svc/gateway" "${port}:80" \
    >"$REPO_ROOT/.kube/port-forward.log" 2>&1 &
  echo $! > "$REPO_ROOT/.kube/port-forward.pid"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 1 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "error: port-forward did not become ready" >&2
  return 1
}

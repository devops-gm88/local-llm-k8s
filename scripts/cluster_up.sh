#!/usr/bin/env bash
# Create (if needed) and start the Lima VM, install k3s, copy the kubeconfig out.
# Idempotent: safe to re-run.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_lima

# Render the committed VM definition with this clone's path so the VM mounts
# the same repo the host is working in.
LIMA_TEMPLATE="$REPO_ROOT/scripts/lima/llmk8s.yaml.in"
LIMA_CONFIG="$LIMA_HOME/llmk8s.generated.yaml"
mkdir -p "$LIMA_HOME"
sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$LIMA_TEMPLATE" > "$LIMA_CONFIG"

if ! vm_exists; then
  echo "==> creating Lima VM '$VM_NAME' (4 vCPU, 6 GiB RAM, 20 GiB disk, arm64)"
  "$LIMACTL" start --name="$VM_NAME" -y "$LIMA_CONFIG"
elif ! vm_running; then
  echo "==> starting existing VM '$VM_NAME'"
  "$LIMACTL" start "$VM_NAME"
else
  echo "==> VM '$VM_NAME' already running"
fi

echo "==> waiting for the VM to accept commands"
for _ in $(seq 1 150); do
  if in_vm "test -f /etc/os-release" >/dev/null 2>&1; then break; fi
  sleep 2
done

VM_IP="$(vm_ip)"
if [ -z "$VM_IP" ]; then
  echo "error: could not determine the VM IP" >&2
  exit 1
fi
echo "$VM_IP" > "$VM_IP_FILE"
echo "==> VM IP: $VM_IP (host reaches it only through Lima port forwards)"

if ! in_vm "test -x /usr/local/bin/k3s"; then
  echo "==> installing k3s (traefik disabled, metrics-server kept for the HPA)"
  in_vm "curl -sfL https://get.k3s.io | sudo INSTALL_K3S_EXEC='server --tls-san ${VM_IP} --write-kubeconfig-mode 644 --disable traefik' sh -"
else
  echo "==> k3s already installed"
fi

echo "==> waiting for the node to be Ready"
in_vm 'for i in $(seq 1 150); do sudo k3s kubectl get nodes 2>/dev/null | grep -q " Ready" && exit 0; sleep 2; done; exit 1'

# k3s writes 127.0.0.1:6443, and Lima forwards host 6443 -> guest 6443, so the
# kubeconfig works from the host unchanged.
in_vm "sudo cat /etc/rancher/k3s/k3s.yaml" > "$KUBECONFIG.tmp"
grep -v '^\s*#' "$KUBECONFIG.tmp" | sed '/^[[:space:]]*$/d' > "$KUBECONFIG"
rm -f "$KUBECONFIG.tmp"
chmod 600 "$KUBECONFIG"

echo "==> checking the API through the Lima port forward"
for _ in $(seq 1 30); do
  if "$KUBECTL" get nodes >/dev/null 2>&1; then break; fi
  sleep 1
done
"$KUBECTL" get nodes -o wide
"$KUBECTL" version 2>/dev/null | head -3
echo "==> kubeconfig written to $KUBECONFIG"

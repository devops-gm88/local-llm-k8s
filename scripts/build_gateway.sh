#!/usr/bin/env bash
# Build the gateway image inside the VM and import it into k3s' containerd.
# No Docker on the Mac: the VM's nerdctl + buildkit does the build.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
require_lima

IMAGE="${GATEWAY_IMAGE:-local-llm-k8s/gateway:dev}"
CTX="$REPO_ROOT/gateway"
REMOTE_CTX="/tmp/local-llm-k8s-gateway"
NERDCTL_FULL_VERSION="${NERDCTL_FULL_VERSION:-2.4.1}"

# 1. Build tooling. nerdctl-full ships nerdctl, containerd and buildkit, so the
#    VM builds an image without Docker Desktop being installed anywhere.
if ! in_vm "command -v nerdctl" >/dev/null 2>&1; then
  echo "==> installing nerdctl-full ${NERDCTL_FULL_VERSION} in the VM"
  in_vm "curl -fsSL https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_FULL_VERSION}/nerdctl-full-${NERDCTL_FULL_VERSION}-linux-arm64.tar.gz | sudo tar xzf - -C /usr/local"
  in_vm "sudo systemctl enable --now containerd buildkit"
fi
if ! in_vm "systemctl is-active --quiet buildkit"; then
  echo "error: buildkit is not running in the VM" >&2
  exit 1
fi

# 2. Copy the build context in. The VM has no shared folders on purpose.
echo "==> copying the gateway build context into the VM"
in_vm "rm -rf ${REMOTE_CTX} && mkdir -p ${REMOTE_CTX}"
tar czf - -C "$CTX" . |
  "$LIMACTL" shell "$VM_NAME" -- bash -c "tar xzf - -C '${REMOTE_CTX}'"

# 3. Build, then import straight into the containerd instance k3s uses.
echo "==> building ${IMAGE}"
in_vm "sudo nerdctl build --progress plain -t ${IMAGE} ${REMOTE_CTX}"
echo "==> importing ${IMAGE} into k3s containerd"
in_vm "sudo nerdctl save ${IMAGE} | sudo k3s ctr images import -"
in_vm "sudo k3s ctr images ls | grep -F '${IMAGE%%:*}' | head -3"

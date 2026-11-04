#!/usr/bin/env bash
# Download limactl, kubectl and kubeconform into .tools/. No Homebrew, no sudo, no spend.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="$REPO_ROOT/.tools"
BIN="$TOOLS/bin"
KUBECONFORM_VERSION="${KUBECONFORM_VERSION:-v0.7.0}"
mkdir -p "$BIN"

if [ ! -x "$BIN/limactl" ]; then
  ver="$(curl -sL https://api.github.com/repos/lima-vm/lima/releases/latest \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["tag_name"])')"
  echo "==> lima $ver"
  tmp="$(mktemp -d)"
  curl -sL -o "$tmp/lima.tgz" \
    "https://github.com/lima-vm/lima/releases/download/${ver}/lima-${ver#v}-Darwin-arm64.tar.gz"
  # The whole tree is needed: limactl locates the guest agents in share/lima.
  tar xzf "$tmp/lima.tgz" -C "$TOOLS"
  chmod +x "$BIN/limactl"
  rm -rf "$tmp"
else
  echo "==> limactl already installed"
fi

if [ ! -x "$BIN/kubectl" ]; then
  ver="$(curl -sL https://dl.k8s.io/release/stable.txt)"
  echo "==> kubectl $ver"
  curl -sL -o "$BIN/kubectl" "https://dl.k8s.io/release/${ver}/bin/darwin/arm64/kubectl"
  chmod +x "$BIN/kubectl"
else
  echo "==> kubectl already installed"
fi

if [ ! -x "$BIN/kubeconform" ]; then
  echo "==> kubeconform $KUBECONFORM_VERSION"
  tmp="$(mktemp -d)"
  curl -sL -o "$tmp/kubeconform.tar.gz" \
    "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-darwin-arm64.tar.gz"
  tar xzf "$tmp/kubeconform.tar.gz" -C "$tmp" kubeconform
  install -m 0755 "$tmp/kubeconform" "$BIN/kubeconform"
  rm -rf "$tmp"
else
  echo "==> kubeconform already installed"
fi

"$BIN/limactl" --version
"$BIN/kubectl" version --client
"$BIN/kubeconform" -v

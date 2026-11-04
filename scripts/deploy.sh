#!/usr/bin/env bash
# Apply the local overlay, then wait for Ollama and the gateway to be Ready.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_kubeconfig

# The namespace has to exist before the Secret, and the gateway image has to
# exist before the Deployment, so order matters here.
"$KUBECTL" apply -f "$REPO_ROOT/manifests/base/namespace.yaml"

# Optional shared token: created once, kept out of git.
if [ ! -s "$GATEWAY_TOKEN_FILE" ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$GATEWAY_TOKEN_FILE"
  chmod 600 "$GATEWAY_TOKEN_FILE"
fi
"$KUBECTL" create secret generic gateway-secret -n "$NAMESPACE" \
  --from-literal=token="$(cat "$GATEWAY_TOKEN_FILE")" \
  --dry-run=client -o yaml | "$KUBECTL" apply -f - >/dev/null

echo "==> building the gateway image"
"$REPO_ROOT/scripts/build_gateway.sh"

# The image tag does not change between builds, so restart to pick up the new one.
if k get deployment gateway >/dev/null 2>&1; then
  k rollout restart deployment/gateway >/dev/null
fi

echo "==> applying manifests/local"
"$KUBECTL" apply -k "$REPO_ROOT/manifests/local"

echo "==> waiting for Ollama (pulls the model on first run; this is the slow step)"
k rollout status deployment/ollama --timeout=900s
k wait --for=condition=Ready pod -l app.kubernetes.io/name=ollama --timeout=300s

echo "==> waiting for the gateway"
k rollout status deployment/gateway --timeout=300s

echo "==> workloads:"
k get pods -o wide
k get svc,hpa

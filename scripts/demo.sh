#!/usr/bin/env bash
# Ask one question through the gateway. This is the acceptance proof.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_kubeconfig

BASE_URL="$(gateway_url)"
if [ "$BASE_URL" = "http://127.0.0.1:18080" ]; then
  ensure_port_forward 18080
fi
TOKEN="$(gateway_token)"
QUESTION="${1:-What is the capital of Canada? Answer in one sentence.}"

BODY="$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1], "max_tokens": 96}))' "$QUESTION")"
echo "==> POST ${BASE_URL}/ask"
RESPONSE="$(curl -sS --max-time 180 -X POST "${BASE_URL}/ask" \
  -H 'content-type: application/json' \
  -w '\n%{http_code}' \
  ${TOKEN:+-H "Authorization: Bearer ${TOKEN}"} \
  -d "$BODY")"
HTTP_CODE="${RESPONSE##*$'\n'}"
PAYLOAD="${RESPONSE%$'\n'*}"

echo "$PAYLOAD" | python3 -m json.tool
if [ "$HTTP_CODE" != "200" ]; then
  echo "error: gateway returned HTTP $HTTP_CODE" >&2
  exit 1
fi
echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["answer"].strip(), "empty answer"; print("==> OK:", d["timings"]["tokens_per_second"], "tok/s,", d["timings"]["total_ms"], "ms")'

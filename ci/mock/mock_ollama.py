"""A stand-in for Ollama, used only by the kind smoke test in CI.

The Mac runs the real model; CI proves the manifests, probes, Services,
selectors and the gateway container wiring without downloading 400 MB of
weights onto a shared runner. Responses are deterministic and marked MOCK.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b-instruct")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": MODEL, "model": MODEL}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/api/generate":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad json"})
            return
        prompt = str(request.get("prompt", ""))[:40]
        self._send(
            200,
            {
                "model": MODEL,
                "response": f"MOCK[{MODEL}]: {prompt}",
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 8,
                "eval_duration": 800_000_000,
                "load_duration": 1_000_000,
            },
        )

    def log_message(self, *args: object) -> None:  # keep CI output readable
        return


if __name__ == "__main__":
    port = int(os.getenv("PORT", "11434"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

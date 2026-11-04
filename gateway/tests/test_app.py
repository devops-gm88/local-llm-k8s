"""Unit tests for the gateway. No cluster, no model, no network."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as gateway


TAGS_OK = {
    "models": [
        {"name": "qwen2.5:0.5b-instruct", "model": "qwen2.5:0.5b-instruct", "size": 397821472}
    ]
}

GENERATE_OK = {
    "model": "qwen2.5:0.5b-instruct",
    "response": "4",
    "done": True,
    "prompt_eval_count": 12,
    "eval_count": 8,
    "eval_duration": 800_000_000,   # 0.8 s for 8 tokens = 10 tok/s
    "load_duration": 120_000_000,   # 0.12 s model load
    "total_duration": 1_000_000_000,
}


def client_for(handler):
    app = gateway.create_app(transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_healthz_is_independent_of_ollama():
    def handler(request):
        raise httpx.ConnectError("no ollama", request=request)

    with client_for(handler) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok_when_model_present():
    with client_for(lambda r: httpx.Response(200, json=TAGS_OK)) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["model"] == gateway.OLLAMA_MODEL


def test_readyz_not_ready_when_model_missing():
    with client_for(lambda r: httpx.Response(200, json={"models": []})) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "model_missing"


def test_readyz_not_ready_when_ollama_down():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with client_for(handler) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "ollama_unreachable"


def test_ask_returns_answer_and_token_accounting():
    with client_for(lambda r: httpx.Response(200, json=GENERATE_OK)) as client:
        resp = client.post("/ask", json={"question": "2+2?", "max_tokens": 32})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "4"
        assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
        assert body["timings"]["tokens_per_second"] == pytest.approx(10.0)
        assert body["timings"]["model_load_ms"] == pytest.approx(120.0)
        assert resp.headers["x-request-id"] == body["request_id"]


def test_ask_forwards_max_tokens_to_ollama():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=GENERATE_OK)

    with client_for(handler) as client:
        client.post("/ask", json={"question": "hi", "max_tokens": 7})
    assert seen["options"]["num_predict"] == 7
    assert seen["stream"] is False


def test_ask_timeout_maps_to_504():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with client_for(handler) as client:
        resp = client.post("/ask", json={"question": "hi"})
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"


def test_ask_upstream_error_maps_to_502():
    with client_for(lambda r: httpx.Response(500, text="boom")) as client:
        resp = client.post("/ask", json={"question": "hi"})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "upstream_error"


def test_ask_missing_model_maps_to_503():
    with client_for(lambda r: httpx.Response(404, json={"error": "model not found"})) as client:
        resp = client.post("/ask", json={"question": "hi"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "model_not_found"


def test_ask_rejects_empty_question():
    with client_for(lambda r: httpx.Response(200, json=GENERATE_OK)) as client:
        assert client.post("/ask", json={"question": ""}).status_code == 422
        assert client.post("/ask", json={}).status_code == 422


def test_ask_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(gateway, "API_TOKEN", "s3cret")
    with client_for(lambda r: httpx.Response(200, json=GENERATE_OK)) as client:
        assert client.post("/ask", json={"question": "hi"}).status_code == 401
        ok = client.post(
            "/ask", json={"question": "hi"}, headers={"Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200
        # Probes stay open so Kubernetes can check the pod.
        assert client.get("/healthz").status_code == 200


def test_metrics_counts_requests_and_errors():
    responses = iter([httpx.Response(200, json=GENERATE_OK), httpx.Response(500, text="boom")])

    def handler(request):
        return next(responses)

    with client_for(handler) as client:
        client.post("/ask", json={"question": "hi"})
        client.post("/ask", json={"question": "hi"})
        text = client.get("/metrics").text
    assert "llm_gateway_requests_total 2" in text
    assert "llm_gateway_errors_total 1" in text

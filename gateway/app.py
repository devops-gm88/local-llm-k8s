"""FastAPI gateway in front of Ollama.

One job: accept ``POST /ask``, call the model through Ollama's HTTP API, and
return the answer together with token accounting and one structured JSON log
line per request. The gateway holds no state about the model; it is the piece
that can scale horizontally while Ollama stays a single workload.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b-instruct")
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "120"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "128"))
API_TOKEN = os.getenv("GATEWAY_API_TOKEN", "")

log = logging.getLogger("gateway")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_handler)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO"))


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1, le=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


def _model_names(tags: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in tags.get("models", []) or []:
        for key in ("name", "model"):
            if isinstance(item.get(key), str):
                names.add(item[key])
    return names


def _has_model(tags: dict[str, Any], model: str) -> bool:
    names = _model_names(tags)
    return bool({model, f"{model}:latest"} & names)


def _log_line(**fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    log.info(json.dumps(record, separators=(",", ":")))


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build the app. ``transport`` is injected by the tests only."""
    app = FastAPI(title="llm-gateway", version="1.0.0")
    stats = {"requests": 0, "errors": 0, "latency_ms_sum": 0.0}
    app.state.stats = stats

    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(REQUEST_TIMEOUT_S)}
    if transport is not None:
        client_kwargs["transport"] = transport
    app.state.client = httpx.AsyncClient(**client_kwargs)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.client.aclose()

    @app.middleware("http")
    async def _request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    def _authorized(request: Request) -> bool:
        if not API_TOKEN:
            return True
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        return header.startswith(prefix) and secrets.compare_digest(
            header[len(prefix) :], API_TOKEN
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process can serve HTTP. Never depends on Ollama."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness: Ollama answers AND the configured model is present."""
        try:
            resp = await app.state.client.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
            resp.raise_for_status()
            tags = resp.json()
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "ollama_unreachable", "detail": str(exc)},
            )
        if not _has_model(tags, OLLAMA_MODEL):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "model_missing", "model": OLLAMA_MODEL},
            )
        return JSONResponse(status_code=200, content={"status": "ready", "model": OLLAMA_MODEL})

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """Per-pod counters. Sum across pods for a service-wide total."""
        count = stats["requests"]
        avg = stats["latency_ms_sum"] / count if count else 0.0
        body = (
            "# HELP llm_gateway_requests_total Requests handled by this pod.\n"
            "# TYPE llm_gateway_requests_total counter\n"
            f"llm_gateway_requests_total {count}\n"
            "# HELP llm_gateway_errors_total Failed requests handled by this pod.\n"
            "# TYPE llm_gateway_errors_total counter\n"
            f"llm_gateway_errors_total {stats['errors']}\n"
            "# HELP llm_gateway_latency_ms Mean end-to-end latency, this pod.\n"
            "# TYPE llm_gateway_latency_ms gauge\n"
            f"llm_gateway_latency_ms {avg:.3f}\n"
        )
        return PlainTextResponse(body)

    @app.post("/ask")
    async def ask(payload: AskRequest, request: Request) -> JSONResponse:
        request_id: str = getattr(request.state, "request_id", uuid.uuid4().hex)
        started = time.perf_counter()

        if not _authorized(request):
            stats["requests"] += 1
            stats["errors"] += 1
            _log_line(
                event="ask", request_id=request_id, status="unauthorized",
                http_status=401, latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})

        upstream = {
            "model": OLLAMA_MODEL,
            "prompt": payload.question,
            "stream": False,
            "options": {
                "num_predict": payload.max_tokens,
                "temperature": payload.temperature,
            },
        }

        http_status = 200
        error_body: dict[str, Any] | None = None
        data: dict[str, Any] = {}
        try:
            resp = await app.state.client.post(f"{OLLAMA_URL}/api/generate", json=upstream)
            if resp.status_code == 404:
                http_status, error_body = 503, {"code": "model_not_found", "model": OLLAMA_MODEL}
            else:
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            http_status, error_body = 504, {"code": "upstream_timeout", "timeout_s": REQUEST_TIMEOUT_S}
        except httpx.HTTPStatusError as exc:
            http_status, error_body = 502, {
                "code": "upstream_error",
                "upstream_status": exc.response.status_code,
                "detail": exc.response.text[:300],
            }
        except httpx.HTTPError as exc:
            http_status, error_body = 503, {"code": "upstream_unavailable", "detail": str(exc)}

        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        stats["requests"] += 1
        stats["latency_ms_sum"] += elapsed_ms

        if error_body is not None:
            stats["errors"] += 1
            _log_line(
                event="ask", request_id=request_id, status="error", http_status=http_status,
                error=error_body["code"], latency_ms=elapsed_ms,
                model=OLLAMA_MODEL, max_tokens=payload.max_tokens,
                question_chars=len(payload.question),
            )
            return JSONResponse(status_code=http_status, content={"error": error_body, "request_id": request_id})

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)
        eval_ns = int(data.get("eval_duration") or 0)
        load_ns = int(data.get("load_duration") or 0)
        tokens_per_second = round(completion_tokens / (eval_ns / 1e9), 2) if eval_ns > 0 else 0.0

        _log_line(
            event="ask", request_id=request_id, status="ok", http_status=200,
            latency_ms=elapsed_ms, model=OLLAMA_MODEL, max_tokens=payload.max_tokens,
            question_chars=len(payload.question), prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, tokens_per_second=tokens_per_second,
            model_load_ms=round(load_ns / 1e6, 2),
        )

        return JSONResponse(
            status_code=200,
            content={
                "request_id": request_id,
                "model": OLLAMA_MODEL,
                "answer": data.get("response", ""),
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "timings": {
                    "total_ms": elapsed_ms,
                    "model_load_ms": round(load_ns / 1e6, 2),
                    "generation_ms": round(eval_ns / 1e6, 2),
                    "tokens_per_second": tokens_per_second,
                },
            },
        )

    return app


app = create_app()

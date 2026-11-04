"""Async load generator for the gateway.

Kept separate from the orchestration so it can be pointed at any deployment:
``run_load`` needs only a base URL, a token and a request shape.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class RequestResult:
    status: int | None
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_second: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile on an already sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = int(round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


async def _one(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict,
    semaphore: asyncio.Semaphore,
    out: list[RequestResult],
) -> None:
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.post(f"{url}/ask", json=payload, headers=headers)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status_code != 200:
                out.append(
                    RequestResult(
                        status=response.status_code,
                        latency_ms=elapsed_ms,
                        error=response.text[:300],
                    )
                )
                return
            body = response.json()
            usage = body.get("usage", {})
            timings = body.get("timings", {})
            out.append(
                RequestResult(
                    status=200,
                    latency_ms=elapsed_ms,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    tokens_per_second=float(timings.get("tokens_per_second", 0.0)),
                )
            )
        except Exception as exc:  # noqa: BLE001 - errors are data here
            out.append(
                RequestResult(
                    status=None,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=repr(exc)[:300],
                )
            )


def summarize(results: list[RequestResult], wall_seconds: float) -> dict:
    """Turn raw request results into the row the report shows."""
    ok = [r for r in results if r.ok]
    latencies = sorted(r.latency_ms for r in ok)
    total_completion = sum(r.completion_tokens for r in ok)
    total_prompt = sum(r.prompt_tokens for r in ok)
    per_request_tps = [r.tokens_per_second for r in ok if r.tokens_per_second > 0]
    return {
        "requests": len(results),
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "error_rate": round((len(results) - len(ok)) / len(results), 4) if results else 0.0,
        "wall_seconds": round(wall_seconds, 3),
        "throughput_rps": round(len(ok) / wall_seconds, 3) if wall_seconds > 0 else 0.0,
        "latency_ms_p50": round(_percentile(latencies, 0.50), 1),
        "latency_ms_p95": round(_percentile(latencies, 0.95), 1),
        "latency_ms_p99": round(_percentile(latencies, 0.99), 1),
        "latency_ms_mean": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "latency_ms_min": round(latencies[0], 1) if latencies else 0.0,
        "latency_ms_max": round(latencies[-1], 1) if latencies else 0.0,
        "completion_tokens": total_completion,
        "prompt_tokens": total_prompt,
        # Tokens actually produced per wall-clock second: the number the cost
        # model uses, because it is what the hardware delivers.
        "tokens_per_second_wall": round(total_completion / wall_seconds, 2) if wall_seconds > 0 else 0.0,
        "tokens_per_second_per_request_mean": (
            round(sum(per_request_tps) / len(per_request_tps), 2) if per_request_tps else 0.0
        ),
    }


async def run_load(
    base_url: str,
    token: str,
    prompt: str,
    max_tokens: int,
    concurrency: int,
    count: int,
    timeout_s: float = 600.0,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"question": prompt, "max_tokens": max_tokens, "temperature": 0.0}
    limits = httpx.Limits(
        max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10
    )
    semaphore = asyncio.Semaphore(concurrency)
    results: list[RequestResult] = []
    started = time.perf_counter()
    async with httpx.AsyncClient(
        limits=limits, timeout=httpx.Timeout(timeout_s, connect=10.0)
    ) as client:
        await asyncio.gather(
            *[
                _one(client, base_url, headers, payload, semaphore, results)
                for _ in range(count)
            ]
        )
    return summarize(results, time.perf_counter() - started)


def run_load_sync(**kwargs) -> dict:
    """Blocking wrapper, so the orchestrator can run it in a thread."""
    return asyncio.run(run_load(**kwargs))


def single_ask(
    base_url: str, token: str, prompt: str, max_tokens: int = 8, retries: int = 5
) -> dict:
    """One request, no concurrency: used for cold-start measurement.

    Retries because a pod that just became Ready can still have its connection
    dropped by the kube-proxy endpoint update; that is a measurement artefact,
    not a result, and it must not abort the whole benchmark.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last_error: Exception | None = None
    for attempt in range(retries):
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
                response = client.post(
                    f"{base_url}/ask",
                    json={"question": prompt, "max_tokens": max_tokens, "temperature": 0.0},
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
            body["_wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
            body["_attempts"] = attempt + 1
            return body
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"single_ask failed after {retries} attempts: {last_error!r}")

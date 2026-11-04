"""Benchmark driver: gateway replicas x client concurrency on the local k3s node.

Writes ``bench/results.json``, ``bench/results.csv`` and charts. Everything it
reports is measured here; nothing is typed in by hand.

Usage:
    python bench/run_bench.py            # full grid + cold start + HPA
    python bench/run_bench.py --quick    # tiny smoke run, validates the harness
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import loadgen

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent

KUBECTL = os.environ.get("KUBECTL", "kubectl")
NAMESPACE = os.environ.get("NAMESPACE", "llm")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:30080")
TOKEN = os.environ.get("GATEWAY_TOKEN", "")

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b-instruct")
PROMPT = (
    "Explain in two sentences why measuring latency matters when you self-host "
    "a language model. Keep it under 60 words."
)

DEFAULT_CONFIG = {
    "replicas": [1, 2, 4],
    "concurrency": [10, 50],
    "requests": 120,
    "warmup_requests": 10,
    "warmup_concurrency": 4,
    "max_tokens": 64,
    "hpa_concurrency": 50,
    "hpa_max_seconds": 150,
}


def sh(args: list[str], check: bool = True, timeout: int = 600) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr[-600:]}")
    return proc.stdout


def kubectl(*args: str, check: bool = True, timeout: int = 600) -> str:
    return sh([KUBECTL, "-n", NAMESPACE, *args], check=check, timeout=timeout)


def parse_cpu(value: str) -> float:
    text = value.strip()
    if text.endswith("m"):
        return float(text[:-1]) / 1000.0
    if text.endswith("n"):
        return float(text[:-1]) / 1e9
    return float(text)


_MEM_UNITS = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0**2}


def parse_mem(value: str) -> float:
    """Return MiB."""
    text = value.strip()
    for unit, factor in _MEM_UNITS.items():
        if text.endswith(unit):
            return float(text[: -len(unit)]) * factor
    if text.endswith("K"):
        return float(text[:-1]) * 1000 / 1024**2
    if text.endswith("M"):
        return float(text[:-1]) * 1e6 / 1024**2
    if text.endswith("G"):
        return float(text[:-1]) * 1e9 / 1024**2
    return float(text) / 1024**2


class ResourceSampler(threading.Thread):
    """Scrape ``kubectl top pod`` while a run is in flight."""

    def __init__(self, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.samples: list[dict[str, tuple[float, float]]] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            out = kubectl("top", "pod", "--no-headers", check=False, timeout=30)
            rows: dict[str, tuple[float, float]] = {}
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        rows[parts[0]] = (parse_cpu(parts[1]), parse_mem(parts[2]))
                    except ValueError:
                        continue
            self.samples.append(rows)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=10)

    def aggregate(self) -> dict:
        if not self.samples:
            return {"available": False, "reason": "kubectl top returned no samples"}
        gateway = {"cpu": [], "mem": []}
        ollama = {"cpu": [], "mem": []}
        for sample in self.samples:
            g_cpu = sum(v[0] for k, v in sample.items() if k.startswith("gateway-"))
            g_mem = sum(v[1] for k, v in sample.items() if k.startswith("gateway-"))
            o_cpu = sum(v[0] for k, v in sample.items() if k.startswith("ollama-"))
            o_mem = sum(v[1] for k, v in sample.items() if k.startswith("ollama-"))
            if any(k.startswith("gateway-") for k in sample):
                gateway["cpu"].append(g_cpu)
                gateway["mem"].append(g_mem)
            if any(k.startswith("ollama-") for k in sample):
                ollama["cpu"].append(o_cpu)
                ollama["mem"].append(o_mem)

        def stats(values: list[float]) -> dict:
            if not values:
                return {"mean": None, "peak": None}
            return {"mean": round(sum(values) / len(values), 3), "peak": round(max(values), 3)}

        return {
            "available": True,
            "samples": len(self.samples),
            "gateway_cpu_cores": stats(gateway["cpu"]),
            "gateway_mem_mib": stats(gateway["mem"]),
            "ollama_cpu_cores": stats(ollama["cpu"]),
            "ollama_mem_mib": stats(ollama["mem"]),
        }


def wait_deployment_ready(name: str, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = kubectl(
            "get",
            "deployment",
            name,
            "-o",
            "jsonpath={.status.readyReplicas}/{.status.replicas}",
            check=False,
            timeout=30,
        )
        if "/" in out:
            ready, total = out.split("/", 1)
            try:
                if int(ready) == int(total) and int(total) > 0:
                    return
            except ValueError:
                pass
        time.sleep(2)
    raise TimeoutError(f"deployment/{name} did not become ready")


def wait_pod_count(selector: str, expected: int, timeout_s: int = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = kubectl(
            "get",
            "pods",
            "-l",
            selector,
            "--field-selector=status.phase=Running",
            "--no-headers",
            check=False,
            timeout=30,
        )
        count = len([line for line in out.splitlines() if line.strip()])
        if count == expected:
            return
        time.sleep(1)
    raise TimeoutError(f"expected {expected} running pods for {selector}")


def pin_hpa(minimum: int, maximum: int) -> None:
    kubectl(
        "patch",
        "hpa",
        "gateway",
        "--type=merge",
        "-p",
        json.dumps({"spec": {"minReplicas": minimum, "maxReplicas": maximum}}),
        check=False,
    )


def set_replicas(replicas: int) -> None:
    # Pin the HPA to the target so it cannot fight the fixed-replica runs.
    pin_hpa(replicas, replicas)
    kubectl("scale", "deployment/gateway", f"--replicas={replicas}")
    wait_deployment_ready("gateway")
    time.sleep(3)  # let the new pods be picked up by metrics-server


def warmup(concurrency: int, count: int) -> dict:
    print(f"    warm-up: {count} requests at concurrency {concurrency} (excluded)", flush=True)
    return loadgen.run_load_sync(
        base_url=BASE_URL,
        token=TOKEN,
        prompt=PROMPT,
        max_tokens=32,
        concurrency=concurrency,
        count=count,
    )


def run_grid(config: dict, payload: dict) -> None:
    runs: list[dict] = payload["runs"]
    for replicas in config["replicas"]:
        print(f"==> replicas={replicas}: pinning HPA and scaling", flush=True)
        set_replicas(replicas)
        for concurrency in config["concurrency"]:
            warmup(config["warmup_concurrency"], config["warmup_requests"])
            sampler = ResourceSampler()
            sampler.start()
            print(
                f"    measuring: {config['requests']} requests, concurrency {concurrency}, "
                f"max_tokens {config['max_tokens']}",
                flush=True,
            )
            result = loadgen.run_load_sync(
                base_url=BASE_URL,
                token=TOKEN,
                prompt=PROMPT,
                max_tokens=config["max_tokens"],
                concurrency=concurrency,
                count=config["requests"],
            )
            sampler.stop()
            resources = sampler.aggregate()
            row = {
                "replicas": replicas,
                "concurrency": concurrency,
                **result,
                "resources": resources,
            }
            runs.append(row)
            print(
                f"    p50={row['latency_ms_p50']}ms p95={row['latency_ms_p95']}ms "
                f"tok/s={row['tokens_per_second_wall']} errors={row['error_rate']}",
                flush=True,
            )
            # Write after every cell: a crash later must not lose measured data.
            write_outputs(payload)


def wait_readyz(timeout_s: int = 180) -> None:
    """The Deployment can report Ready a moment before the Service routes to it."""
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = httpx.get(f"{BASE_URL}/readyz", timeout=3.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError("gateway /readyz never returned 200")


def measure_cold_start(config: dict) -> dict:
    print("==> cold start: gateway from 0 replicas, then model reload", flush=True)
    kubectl("delete", "hpa", "gateway", "--ignore-not-found", check=False)
    kubectl("scale", "deployment/gateway", "--replicas=0")
    wait_pod_count("app.kubernetes.io/name=gateway", 0)
    time.sleep(5)

    started = time.perf_counter()
    kubectl("scale", "deployment/gateway", "--replicas=1")
    wait_deployment_ready("gateway")
    gateway_ready_s = round(time.perf_counter() - started, 2)

    wait_readyz()

    # A pod is Ready when /readyz passes, which needs Ollama to list the model.
    first = loadgen.single_ask(BASE_URL, TOKEN, PROMPT, max_tokens=8)
    first_ask_ms = first.get("_wall_ms")

    # Now evict Ollama: the init container re-verifies the cached weights and
    # the server reloads the model from disk.
    print("    restarting the Ollama pod to measure model load", flush=True)
    restart_started = time.perf_counter()
    kubectl("delete", "pod", "-l", "app.kubernetes.io/name=ollama", "--wait=false")
    wait_pod_count("app.kubernetes.io/name=ollama", 0, timeout_s=120)
    wait_deployment_ready("ollama", timeout_s=600)
    ollama_ready_s = round(time.perf_counter() - restart_started, 2)
    wait_readyz()
    after = loadgen.single_ask(BASE_URL, TOKEN, PROMPT, max_tokens=8)

    return {
        "gateway_cold_start_s": gateway_ready_s,
        "gateway_first_ask_ms": first_ask_ms,
        "ollama_restart_s": ollama_ready_s,
        "first_ask_after_model_reload_ms": after.get("_wall_ms"),
        "model_load_ms_after_reload": after.get("timings", {}).get("model_load_ms"),
    }


def measure_hpa(config: dict) -> dict:
    print(f"==> HPA: load at concurrency {config['hpa_concurrency']} and watch the scale-up", flush=True)
    kubectl("apply", "-n", NAMESPACE, "-f", str(REPO_ROOT / "manifests/base/gateway-hpa.yaml"), check=False)
    pin_hpa(1, 4)
    kubectl("scale", "deployment/gateway", "--replicas=1")
    wait_deployment_ready("gateway")
    time.sleep(3)

    timeline: list[dict] = []
    started = time.perf_counter()
    load_result: dict = {}
    done = threading.Event()

    def _load() -> None:
        nonlocal load_result
        try:
            load_result = loadgen.run_load_sync(
                base_url=BASE_URL,
                token=TOKEN,
                prompt=PROMPT,
                max_tokens=config["max_tokens"],
                concurrency=config["hpa_concurrency"],
                count=400,
            )
        finally:
            done.set()

    worker = threading.Thread(target=_load, daemon=True)
    worker.start()

    peak = 1
    first_scale_s: float | None = None
    max_scale_s: float | None = None
    while True:
        elapsed = time.perf_counter() - started
        out = kubectl("get", "hpa", "gateway", "-o", "json", check=False, timeout=30)
        current = desired = None
        cpu_pct = None
        try:
            state = json.loads(out)
            current = state["status"].get("currentReplicas")
            desired = state["status"].get("desiredReplicas")
            for metric in state["status"].get("currentMetrics") or []:
                if metric.get("resource", {}).get("name") == "cpu":
                    cpu_pct = metric["resource"].get("current", {}).get("averageUtilization")
        except (json.JSONDecodeError, KeyError):
            pass
        if current is not None:
            peak = max(peak, current)
            if current > 1 and first_scale_s is None:
                first_scale_s = round(elapsed, 1)
            if current >= 4 and max_scale_s is None:
                max_scale_s = round(elapsed, 1)
        timeline.append(
            {"t_s": round(elapsed, 1), "replicas": current, "desired": desired, "cpu_pct": cpu_pct}
        )
        if (max_scale_s is not None and elapsed > (max_scale_s or 0) + 15) or done.is_set() or elapsed > config["hpa_max_seconds"]:
            break
        time.sleep(1.5)

    done.wait(timeout=600)
    pin_hpa(1, 4)
    return {
        "target_cpu_utilization": 50,
        "max_replicas": 4,
        "time_to_first_scale_s": first_scale_s,
        "time_to_max_replicas_s": max_scale_s,
        "peak_replicas": peak,
        "reached_max": peak >= 4,
        "load": load_result,
        "timeline": timeline,
    }


def cluster_meta() -> dict:
    out = kubectl("get", "node", "-o", "json", check=False)
    node: dict = {}
    try:
        data = json.loads(out)
        if data["items"]:
            item = data["items"][0]
            node = {
                "name": item["metadata"]["name"],
                "kubelet_version": item["status"]["nodeInfo"].get("kubeletVersion"),
                "os_image": item["status"]["nodeInfo"].get("osImage"),
                "capacity": item["status"]["capacity"],
            }
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    model = kubectl(
        "get", "configmap", "llm-config", "-o", "jsonpath={.data.OLLAMA_MODEL}", check=False
    ).strip()
    return {
        "host_platform": platform.platform(),
        "host_machine": platform.machine(),
        "node": node,
        "model": model or MODEL,
        "gateway_base_url": BASE_URL,
        "namespace": NAMESPACE,
    }


def write_outputs(payload: dict) -> None:
    json_path = BENCH_DIR / "results.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n")

    rows = payload["runs"]
    csv_path = BENCH_DIR / "results.csv"
    fieldnames = [
        "replicas",
        "concurrency",
        "requests",
        "ok",
        "errors",
        "error_rate",
        "throughput_rps",
        "tokens_per_second_wall",
        "tokens_per_second_per_request_mean",
        "latency_ms_p50",
        "latency_ms_p95",
        "latency_ms_p99",
        "latency_ms_mean",
        "gateway_cpu_cores_mean",
        "gateway_cpu_cores_peak",
        "ollama_cpu_cores_mean",
        "gateway_mem_mib_mean",
        "ollama_mem_mib_mean",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            resources = row.get("resources", {})
            gateway_cpu = resources.get("gateway_cpu_cores", {}) or {}
            ollama_cpu = resources.get("ollama_cpu_cores", {}) or {}
            gateway_mem = resources.get("gateway_mem_mib", {}) or {}
            ollama_mem = resources.get("ollama_mem_mib", {}) or {}
            writer.writerow(
                {
                    "replicas": row["replicas"],
                    "concurrency": row["concurrency"],
                    "requests": row["requests"],
                    "ok": row["ok"],
                    "errors": row["errors"],
                    "error_rate": row["error_rate"],
                    "throughput_rps": row["throughput_rps"],
                    "tokens_per_second_wall": row["tokens_per_second_wall"],
                    "tokens_per_second_per_request_mean": row["tokens_per_second_per_request_mean"],
                    "latency_ms_p50": row["latency_ms_p50"],
                    "latency_ms_p95": row["latency_ms_p95"],
                    "latency_ms_p99": row["latency_ms_p99"],
                    "latency_ms_mean": row["latency_ms_mean"],
                    "gateway_cpu_cores_mean": gateway_cpu.get("mean"),
                    "gateway_cpu_cores_peak": gateway_cpu.get("peak"),
                    "ollama_cpu_cores_mean": ollama_cpu.get("mean"),
                    "gateway_mem_mib_mean": gateway_mem.get("mean"),
                    "ollama_mem_mib_mean": ollama_mem.get("mean"),
                }
            )
    print(f"==> wrote {json_path} and {csv_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="tiny smoke run, no cold start/HPA")
    parser.add_argument("--requests", type=int, default=None)
    parser.add_argument("--replicas", type=int, nargs="+", default=None)
    parser.add_argument("--concurrency", type=int, nargs="+", default=None)
    parser.add_argument("--skip-cold-start", action="store_true")
    parser.add_argument("--skip-hpa", action="store_true")
    args = parser.parse_args()

    config = dict(DEFAULT_CONFIG)
    if args.quick:
        config.update({"replicas": [1], "concurrency": [2], "requests": 8, "warmup_requests": 2})
    if args.requests:
        config["requests"] = args.requests
    if args.replicas:
        config["replicas"] = args.replicas
    if args.concurrency:
        config["concurrency"] = args.concurrency

    print(f"==> target {BASE_URL}, model {MODEL}", flush=True)
    payload: dict = {
        "meta": {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "command": " ".join([Path(sys.executable).name, *sys.argv]),
            "prompt": PROMPT,
            **cluster_meta(),
        },
        "config": config,
        "runs": [],
    }
    started = time.time()
    run_grid(config, payload)
    if not args.quick and not args.skip_cold_start:
        try:
            payload["cold_start"] = measure_cold_start(config)
        except Exception as exc:  # noqa: BLE001 - the grid matters more
            payload["cold_start"] = {"error": repr(exc)[:500]}
            print(f"warning: cold-start measurement failed: {exc}", file=sys.stderr)
        write_outputs(payload)
    if not args.quick and not args.skip_hpa:
        try:
            payload["hpa"] = measure_hpa(config)
        except Exception as exc:  # noqa: BLE001
            payload["hpa"] = {"error": repr(exc)[:500]}
            print(f"warning: HPA measurement failed: {exc}", file=sys.stderr)
        write_outputs(payload)
    payload["wall_seconds"] = round(time.time() - started, 1)
    write_outputs(payload)

    try:
        import charts

        charts.build_all(payload)
    except Exception as exc:  # charts are a convenience, not the evidence
        print(f"warning: chart generation failed: {exc}", file=sys.stderr)

    print("==> done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

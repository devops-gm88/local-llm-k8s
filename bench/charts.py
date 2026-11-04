"""Charts generated from bench/results.json. No hand-drawn numbers."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
OUT = BENCH_DIR / "charts"
OUT.mkdir(exist_ok=True)

REPLICA_COLOURS = {1: "#1f4e79", 2: "#2e75b6", 4: "#9dc3e6"}


def _series(payload: dict, field: str, replicas: int, concurrency: int) -> float | None:
    for row in payload["runs"]:
        if row["replicas"] == replicas and row["concurrency"] == concurrency:
            return row[field]
    return None


def latency_chart(payload: dict) -> Path:
    replicas = payload["config"]["replicas"]
    concurrency = payload["config"]["concurrency"]
    fig, axes = plt.subplots(1, len(replicas), figsize=(4.2 * len(replicas), 3.8), sharey=True)
    if len(replicas) == 1:
        axes = [axes]
    for axis, replica_count in zip(axes, replicas):
        p50 = [_series(payload, "latency_ms_p50", replica_count, c) for c in concurrency]
        p95 = [_series(payload, "latency_ms_p95", replica_count, c) for c in concurrency]
        positions = range(len(concurrency))
        axis.bar([p - 0.18 for p in positions], p50, width=0.36, label="p50", color="#2e75b6")
        axis.bar([p + 0.18 for p in positions], p95, width=0.36, label="p95", color="#c55a11")
        axis.set_xticks(list(positions))
        axis.set_xticklabels([str(c) for c in concurrency])
        axis.set_title(f"{replica_count} gateway replica{'s' if replica_count > 1 else ''}")
        axis.set_xlabel("concurrent clients")
        axis.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("end-to-end latency (ms)")
    axes[0].legend(frameon=False)
    fig.suptitle("Gateway latency by replica count and concurrency (CPU-only k3s node)")
    fig.tight_layout()
    path = OUT / "latency.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def throughput_chart(payload: dict) -> Path:
    fig, axis = plt.subplots(figsize=(6.4, 4.0))
    concurrency = payload["config"]["concurrency"]
    for replica_count in payload["config"]["replicas"]:
        values = [
            _series(payload, "tokens_per_second_wall", replica_count, c) or 0 for c in concurrency
        ]
        axis.plot(
            [str(c) for c in concurrency],
            values,
            marker="o",
            label=f"{replica_count} replica{'s' if replica_count > 1 else ''}",
            color=REPLICA_COLOURS.get(replica_count),
        )
    axis.set_xlabel("concurrent clients")
    axis.set_ylabel("output tokens / second (wall clock)")
    axis.set_title("Sustained throughput: Ollama is the bottleneck, not the gateway")
    axis.grid(alpha=0.3)
    axis.legend(frameon=False)
    fig.tight_layout()
    path = OUT / "throughput.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def hpa_chart(payload: dict) -> Path | None:
    hpa = payload.get("hpa")
    if not hpa or not hpa.get("timeline"):
        return None
    timeline = hpa["timeline"]
    fig, axis = plt.subplots(figsize=(6.4, 3.8))
    times = [point["t_s"] for point in timeline]
    replicas = [point["replicas"] or 0 for point in timeline]
    axis.step(times, replicas, where="post", color="#1f4e79")
    axis.set_xlabel("seconds after load started")
    axis.set_ylabel("gateway replicas")
    axis.set_title(
        f"HPA scale-up under 50 concurrent clients (reached {hpa['peak_replicas']} replicas)"
    )
    axis.set_ylim(0, 5)
    axis.grid(alpha=0.3)
    fig.tight_layout()
    path = OUT / "hpa_scaleup.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def build_all(payload: dict) -> list[Path]:
    paths = [latency_chart(payload), throughput_chart(payload)]
    hpa_path = hpa_chart(payload)
    if hpa_path:
        paths.append(hpa_path)
    for path in paths:
        print(f"==> wrote {path}")
    return paths


if __name__ == "__main__":
    data = json.loads((BENCH_DIR / "results.json").read_text())
    build_all(data)

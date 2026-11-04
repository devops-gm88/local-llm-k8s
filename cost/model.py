"""Break-even model: self-hosted inference vs API list prices.

Reads the measured throughput from ``bench/results.json`` and the priced inputs
from ``cost/assumptions.json``. Writes ``cost/break_even.csv``, a machine
readable ``cost/model_output.json`` and ``cost/break_even.png``.

The one thing this model will not do is pretend a GPU number was measured. The
CPU throughput is measured; GPU throughput is an explicit assumption, and the
sensitivity table shows how the answer moves with it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COST_DIR = Path(__file__).resolve().parent
REPO_ROOT = COST_DIR.parent
RESULTS = REPO_ROOT / "bench" / "results.json"
ASSUMPTIONS = COST_DIR / "assumptions.json"

HOURS_PER_MONTH = 730
SECONDS_PER_MONTH = 2_592_000


def load_inputs() -> dict:
    assumptions = json.loads(ASSUMPTIONS.read_text())
    if not RESULTS.exists():
        raise SystemExit(
            f"missing {RESULTS}; run `make bench` first so the throughput is measured, not guessed"
        )
    results = json.loads(RESULTS.read_text())
    runs = results["runs"]
    if not runs:
        raise SystemExit("bench/results.json has no runs")

    ok_runs = [r for r in runs if r["ok"] > 0]
    prompt_tokens = sum(r["prompt_tokens"] for r in ok_runs)
    completion_tokens = sum(r["completion_tokens"] for r in ok_runs)
    total = prompt_tokens + completion_tokens
    best = max(runs, key=lambda r: r["tokens_per_second_wall"])

    measured = {
        "best_tokens_per_second_wall": best["tokens_per_second_wall"],
        "best_run": {"replicas": best["replicas"], "concurrency": best["concurrency"]},
        "input_token_share": round(prompt_tokens / total, 4) if total else 0.5,
        "output_token_share": round(completion_tokens / total, 4) if total else 0.5,
        "worst_tokens_per_second_wall": min(r["tokens_per_second_wall"] for r in runs),
        "generated_at": results["meta"]["generated_at"],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    return {"assumptions": assumptions, "measured": measured, "results": results}


def blended_usd_per_million(model: dict, input_share: float) -> float:
    """USD per 1M mixed tokens at the measured input/output ratio."""
    return model["input_per_1m"] * input_share + model["output_per_1m"] * (
        1.0 - input_share
    )


def monthly_cost(assumptions: dict, measured: dict) -> list[dict]:
    hours = assumptions["hours_per_month"]
    overhead = assumptions["rented_node_overhead_pct"] / 100.0

    rented = assumptions["gpu_rental"]["offers"][0]
    rented_base = rented["usd_per_hour"] * hours
    rented_total = rented_base * (1 + overhead)

    owned = assumptions["owned_hardware"]
    owned_power = owned["power_watts"] * hours * owned["electricity_usd_per_kwh"] / 1000.0
    owned_base = owned["capex_usd"] / owned["amortization_months"] + owned_power
    owned_total = owned_base * (1 + assumptions["owned_node_overhead_pct"] / 100.0)

    cpu_power = (
        assumptions["measured_power_watts"] * hours * owned["electricity_usd_per_kwh"] / 1000.0
    )
    # The laptop is not free: if it is charged as hardware, its capex amortises
    # like everything else. The owned case keeps both views.
    cpu_capex = 2000.0 / owned["amortization_months"]
    cpu_ownership_total = (cpu_capex + cpu_power) * (1 + overhead)

    gpu_tps = assumptions["headline_gpu_throughput_tokens_per_second"]
    cpu_capacity = measured["best_tokens_per_second_wall"] * SECONDS_PER_MONTH
    gpu_capacity = gpu_tps * SECONDS_PER_MONTH

    return [
        {
            "scenario": "measured CPU-only (this Mac, via Lima VM)",
            "basis": "measured",
            "monthly_cost_usd": round(cpu_ownership_total, 2),
            "monthly_cost_marginal_usd": round(cpu_power, 2),
            "capacity_tokens_per_month": round(cpu_capacity),
            "tokens_per_second": measured["best_tokens_per_second_wall"],
            "notes": "throughput measured; 35 W and $2,000 hardware amortised 36 months are assumptions",
        },
        {
            "scenario": "rented L4 24GB (RunPod, 730 h/month)",
            "basis": "modelled throughput",
            "monthly_cost_usd": round(rented_total, 2),
            "monthly_cost_marginal_usd": round(rented_total, 2),
            "capacity_tokens_per_month": round(gpu_capacity),
            "tokens_per_second": gpu_tps,
            "notes": "$0.44/h verified 2026-11-04; +20% ops overhead; throughput assumed",
        },
        {
            "scenario": "owned GPU box ($2,500 capex, 36 months)",
            "basis": "modelled throughput",
            "monthly_cost_usd": round(owned_total, 2),
            "monthly_cost_marginal_usd": round(owned_total, 2),
            "capacity_tokens_per_month": round(gpu_capacity),
            "tokens_per_second": gpu_tps,
            "notes": "capex, power (400 W) and $0.13/kWh are assumptions; +20% ops overhead",
        },
    ]


def build_table(inputs: dict) -> tuple[list[dict], list[dict], dict]:
    assumptions = inputs["assumptions"]
    measured = inputs["measured"]
    models = assumptions["api"]["models"]
    scenarios = monthly_cost(assumptions, measured)
    input_share = measured["input_token_share"]

    rows: list[dict] = []
    for scenario in scenarios:
        for model in models:
            price = blended_usd_per_million(model, input_share)
            break_even_tokens = scenario["monthly_cost_usd"] / (price / 1e6)
            cap = scenario["capacity_tokens_per_month"]
            util = break_even_tokens / cap * 100 if cap else None
            verdict = (
                f"breaks even at {util:.0f}% utilisation"
                if util is not None and util <= 100
                else "cannot break even: capacity below break-even volume"
            )
            rows.append(
                {
                    "scenario": scenario["scenario"],
                    "basis": scenario["basis"],
                    "api_model": f"{model['vendor']} {model['model']}",
                    "blended_api_usd_per_1m_tokens": round(price, 4),
                    "self_host_monthly_usd": scenario["monthly_cost_usd"],
                    "self_host_monthly_marginal_usd": scenario["monthly_cost_marginal_usd"],
                    "capacity_tokens_per_month": cap,
                    "break_even_tokens_per_month": round(break_even_tokens),
                    "required_utilization_pct": None if util is None else round(util, 1),
                    "verdict": verdict,
                    "notes": scenario["notes"],
                }
            )

    # Sensitivity: the GPU throughput is the one input nobody measured.
    sensitivity: list[dict] = []
    rented = next(s for s in scenarios if s["scenario"].startswith("rented"))
    owned = next(s for s in scenarios if s["scenario"].startswith("owned"))
    headline_model = models[0]  # cheapest complete small-model pair
    price = blended_usd_per_million(headline_model, input_share)
    for tps in assumptions["gpu_throughput_assumptions_tokens_per_second"]:
        capacity = tps * SECONDS_PER_MONTH
        for label, cost in (("rented L4", rented["monthly_cost_usd"]), ("owned GPU", owned["monthly_cost_usd"])):
            break_even = cost / (price / 1e6)
            sensitivity.append(
                {
                    "scenario": label,
                    "assumed_tokens_per_second": tps,
                    "monthly_cost_usd": cost,
                    "capacity_tokens_per_month": round(capacity),
                    "break_even_tokens_per_month": round(break_even),
                    "required_utilization_pct": round(break_even / capacity * 100, 1),
                }
            )
    return rows, sensitivity, measured


def write_csv(rows: list[dict]) -> Path:
    path = COST_DIR / "break_even.csv"
    fields = [
        "scenario",
        "basis",
        "api_model",
        "blended_api_usd_per_1m_tokens",
        "self_host_monthly_usd",
        "self_host_monthly_marginal_usd",
        "capacity_tokens_per_month",
        "break_even_tokens_per_month",
        "required_utilization_pct",
        "verdict",
        "notes",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def chart(inputs: dict, rows: list[dict], sensitivity: list[dict]) -> Path:
    assumptions = inputs["assumptions"]
    measured = inputs["measured"]
    headline = assumptions["api"]["models"][0]
    price = blended_usd_per_million(headline, measured["input_token_share"])

    volumes = [1e6, 1e7, 1e8, 1e9, 1e10]
    api_cost = [v / 1e6 * price for v in volumes]

    scenario_lines = {}
    for row in rows:
        if row["api_model"].endswith(headline["model"]):
            scenario_lines[row["scenario"]] = row["self_host_monthly_usd"]

    fig, (left, right) = plt.subplots(1, 2, figsize=(12.5, 4.6))

    left.plot(volumes, api_cost, marker="o", color="#c55a11", label=f"API: {headline['model']} (USD)")
    colours = ["#1f4e79", "#2e75b6", "#9dc3e6"]
    for (label, cost), colour in zip(scenario_lines.items(), colours):
        left.axhline(cost, linestyle="--", color=colour, label=f"{label} (USD/month)")
        if "CPU" in label:
            cpu_capacity = next(
                row["capacity_tokens_per_month"] for row in rows if row["scenario"] == label
            )
            left.axvline(cpu_capacity, color=colour, linestyle=":", linewidth=1)
            left.annotate(
                "measured CPU capacity",
                xy=(cpu_capacity, cost),
                xytext=(6, 6),
                textcoords="offset points",
                fontsize=7,
                color=colour,
            )
    left.set_xscale("log")
    left.set_xlabel("tokens per month (mixed input/output, log scale)")
    left.set_ylabel("USD per month")
    left.set_title("Monthly cost: API vs self-hosted")
    left.grid(alpha=0.3)
    left.legend(frameon=False, fontsize=8)

    for label, colour in (("rented L4", "#c55a11"), ("owned GPU", "#2a9d8f")):
        series = [row for row in sensitivity if row["scenario"] == label]
        right.plot(
            [row["assumed_tokens_per_second"] for row in series],
            [row["required_utilization_pct"] for row in series],
            marker="o",
            color=colour,
            label=label,
        )
    right.axhline(100, color="#7f7f7f", linestyle="--", linewidth=1, label="100% utilisation")
    right.set_xscale("log")
    right.set_xticks(assumptions["gpu_throughput_assumptions_tokens_per_second"])
    right.set_xticklabels(
        [str(v) for v in assumptions["gpu_throughput_assumptions_tokens_per_second"]]
    )
    right.minorticks_off()
    right.set_xlabel("assumed GPU throughput (output tokens/second, log scale)")
    right.set_ylabel("utilisation needed to break even (%)")
    right.set_title(
        "Sensitivity: the GPU throughput was never measured\n"
        f"(API comparator: {headline['model']})"
    )
    right.grid(alpha=0.3)
    right.legend(frameon=False, fontsize=8)

    fig.suptitle("Break-even model - CPU throughput measured, GPU throughput assumed")
    fig.tight_layout()
    path = COST_DIR / "break_even.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def main() -> int:
    inputs = load_inputs()
    rows, sensitivity, measured = build_table(inputs)
    csv_path = write_csv(rows)
    png_path = chart(inputs, rows, sensitivity)

    payload = {
        "generated_from": {
            "bench_results": str(RESULTS.relative_to(REPO_ROOT)),
            "bench_generated_at": measured["generated_at"],
        },
        "measured": measured,
        "assumptions": inputs["assumptions"],
        "rows": rows,
        "sensitivity": sensitivity,
        "headline": {
            "api_model": f"{inputs['assumptions']['api']['models'][0]['vendor']} "
            f"{inputs['assumptions']['api']['models'][0]['model']}",
            "note": "The savings case is a GPU you already own or a rented node kept busy. "
            "CPU-only inference on a laptop is not cheaper than an API once the hardware is counted.",
        },
    }
    out = COST_DIR / "model_output.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")

    header = f"{'scenario':44s} {'api':24s} {'$/mo':>8s} {'break-even tok/mo':>17s} {'util':>7s}"
    print(header)
    print("-" * len(header))
    for row in rows:
        util = row["required_utilization_pct"]
        print(
            f"{row['scenario'][:44]:44s} {row['api_model'][:24]:24s} "
            f"{row['self_host_monthly_usd']:8.2f} {row['break_even_tokens_per_month']:>17,} "
            f"{(f'{util:.0f}%' if util is not None else 'n/a'):>7s}"
        )
    print(f"\nwrote {csv_path.relative_to(REPO_ROOT)}, {out.relative_to(REPO_ROOT)}, "
          f"{png_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Render the measured numbers into README.md and fail if the README drifts.

The README is the page people read; the JSON and CSV are the evidence. This
script keeps them identical: it rewrites the marked blocks from the committed
data, and ``--check`` exits non-zero when the README no longer matches.

Usage:
    python bench/render_readme.py          # rewrite README.md
    python bench/render_readme.py --check  # CI: fail on drift
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
RESULTS = REPO_ROOT / "bench" / "results.json"
BREAK_EVEN = REPO_ROOT / "cost" / "break_even.csv"
MODEL_OUTPUT = REPO_ROOT / "cost" / "model_output.json"

BENCH_START = "<!-- BENCH:START -->"
BENCH_END = "<!-- BENCH:END -->"
COST_START = "<!-- COST:START -->"
COST_END = "<!-- COST:END -->"


def _fmt_int(value: float) -> str:
    return f"{value:,.0f}"


def bench_block() -> str:
    if not RESULTS.exists():
        return "_No benchmark results committed yet. Run `make bench`._"
    data = json.loads(RESULTS.read_text())
    meta = data["meta"]
    runs = data["runs"]
    lines = [
        f"Measured {meta['generated_at']} on `{meta['host_platform']}` "
        f"(`{meta['host_machine']}`), k3s `{meta['node'].get('kubelet_version', '?')}`, "
        f"model `{meta['model']}`, prompt capped at {data['config']['max_tokens']} output tokens. "
        f"Warm-up excluded. {data['config']['requests']} requests per cell.",
        "",
        "| gateway replicas | concurrent clients | p50 (ms) | p95 (ms) | p99 (ms) | output tok/s | error rate |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        lines.append(
            f"| {run['replicas']} | {run['concurrency']} | {run['latency_ms_p50']} | "
            f"{run['latency_ms_p95']} | {run['latency_ms_p99']} | "
            f"{run['tokens_per_second_wall']} | {run['error_rate']:.1%} |"
        )

    hpa = data.get("hpa")
    cold = data.get("cold_start")
    facts = []
    if hpa and hpa.get("time_to_first_scale_s") is not None:
        peak = hpa["peak_replicas"]
        if hpa.get("reached_max"):
            tail = f"and reached the 4-replica maximum after {hpa['time_to_max_replicas_s']} s"
        else:
            tail = (
                "then stopped: per-pod CPU fell back below the target once the second "
                "pod was added, because the gateway is not the bottleneck"
            )
        facts.append(
            f"HPA: under {hpa['load'].get('requests', '?')} requests at "
            f"{data['config'].get('hpa_concurrency', '?')} concurrent clients it scaled "
            f"1 -> **{peak} replicas** after **{hpa['time_to_first_scale_s']} s**, {tail}."
        )
    elif hpa:
        facts.append(f"HPA did not scale: {hpa.get('error', 'no scale-up observed')}")
    if cold:
        facts.append(
            f"Cold start: gateway pod Ready in **{cold['gateway_cold_start_s']} s**; "
            f"Ollama pod restart (model re-verified from the PVC) in **{cold['ollama_restart_s']} s**; "
            f"first answer after the model reload in **{cold['first_ask_after_model_reload_ms']} ms**."
        )
    best = max(runs, key=lambda r: r["tokens_per_second_wall"])
    best = max(runs, key=lambda r: r["tokens_per_second_wall"])
    tok_rates = sorted(r["tokens_per_second_wall"] for r in runs)
    facts.append(
        f"Best sustained throughput in the grid: **{best['tokens_per_second_wall']} output tokens/second** "
        f"(spread across all six cells: {tok_rates[0]}-{tok_rates[-1]} tok/s). "
        f"**Adding gateway replicas does not add throughput** - Ollama is the bottleneck "
        f"and it is not horizontally scalable here."
    )
    lines.extend(["", *[f"- {fact}" for fact in facts]])
    return "\n".join(lines)


def cost_block() -> str:
    if not (BREAK_EVEN.exists() and MODEL_OUTPUT.exists()):
        return "_No cost results committed yet. Run `make cost`._"
    output = json.loads(MODEL_OUTPUT.read_text())
    measured = output["measured"]
    rows = output["rows"]
    headline = output["headline"]

    lines = [
        f"Measured CPU throughput: **{measured['best_tokens_per_second_wall']} output tokens/second** "
        f"({measured['best_run']['replicas']} replica, {measured['best_run']['concurrency']} clients). "
        f"Measured input/output token mix: {measured['input_token_share']:.0%} input / "
        f"{measured['output_token_share']:.0%} output.",
        "",
        f"Comparator: **{headline['api_model']}** at its list price "
        f"(`cost/assumptions.json`, fetched {output['assumptions']['api']['fetched']}).",
        "",
        "| self-host scenario | monthly cost (USD) | break-even tokens/month | utilisation needed |",
        "|---|---:|---:|---:|",
    ]
    for row in rows:
        if not row["api_model"].endswith(headline["api_model"].split()[-1]):
            continue
        util = row["required_utilization_pct"]
        util_text = f"{util:.0f}%" if util is not None else "impossible"
        lines.append(
            f"| {row['scenario']} | {row['self_host_monthly_usd']:,.0f} | "
            f"{_fmt_int(row['break_even_tokens_per_month'])} | {util_text} |"
        )
    cpu_rows = [r for r in rows if r["scenario"].startswith("measured CPU")]
    if cpu_rows:
        cpu = cpu_rows[0]
        marginal = cpu["self_host_monthly_marginal_usd"]
        marginal_break_even = marginal / (
            cpu["blended_api_usd_per_1m_tokens"] / 1e6
        )
        lines.extend(
            [
                "",
                f"Sunk-cost view: with the Mac already owned, the marginal cost of "
                f"serving is electricity only - about **${marginal:.2f}/month**. That "
                f"displaces an API bill for roughly {_fmt_int(marginal_break_even)} tokens/month "
                f"at {cpu['api_model']} prices, so the whole saving is capped at a few "
                f"dollars a month and it ties up the machine you work on.",
            ]
        )
    lines.extend(
        [
            "",
            f"> {headline['note']}",
            "",
            "Sensitivity: the GPU throughput is **assumed, not measured**. "
            "`cost/break_even.png` (right panel) and `cost/model_output.json` show the "
            "break-even for 200-8,000 tokens/second; it scales linearly.",
        ]
    )
    return "\n".join(lines)


def replace_block(text: str, start: str, end: str, body: str) -> str:
    if start not in text or end not in text:
        raise SystemExit(f"README is missing the {start} / {end} markers")
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{start}\n{body}\n{end}{tail}"


def render() -> str:
    text = README.read_text()
    text = replace_block(text, BENCH_START, BENCH_END, bench_block())
    text = replace_block(text, COST_START, COST_END, cost_block())
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if README.md is stale")
    args = parser.parse_args()

    current = README.read_text()
    rendered = render()
    if args.check:
        if current != rendered:
            print(
                "README.md does not match the committed results. Run `make readme` "
                "and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("README matches bench/results.json and cost/break_even.csv")
        return 0
    README.write_text(rendered)
    print(f"rendered {README.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

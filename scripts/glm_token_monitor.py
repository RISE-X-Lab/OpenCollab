#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def _float_env(name: str) -> float | None:
    value = os.environ.get(name)
    if value in (None, ""):
        return None
    return float(value)


def _iter_records(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def collect(trajectories_dir: Path, model_filter: str | None) -> dict:
    totals = {
        "files": 0,
        "runs": set(),
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "split_total_tokens": 0,
        "unknown_split_tokens": 0,
        "estimated_calls": 0,
        "latency_s": 0.0,
    }
    for path in sorted(trajectories_dir.glob("*.jsonl")):
        seen_file = False
        for record in _iter_records(path):
            if record.get("type") != "llm_call":
                continue
            payload = record.get("payload") or {}
            model = str(payload.get("model") or "")
            if model_filter and model_filter not in model:
                continue

            usage = payload.get("usage") or {}
            metrics = record.get("metrics") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or metrics.get("tokens") or 0)

            totals["runs"].add(record.get("run_id") or path.stem)
            totals["calls"] += 1
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["latency_s"] += float(metrics.get("latency_s") or 0.0)
            if usage.get("estimated"):
                totals["estimated_calls"] += 1
            if input_tokens or output_tokens:
                totals["split_total_tokens"] += total_tokens
            else:
                totals["unknown_split_tokens"] += total_tokens
            seen_file = True
        if seen_file:
            totals["files"] += 1
    totals["runs"] = len(totals["runs"])
    return totals


def estimate_cost(
    totals: dict,
    total_price_per_mtok: float | None,
    input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
) -> tuple[float | None, str]:
    split_cost = 0.0
    has_split_price = input_price_per_mtok is not None or output_price_per_mtok is not None
    if has_split_price:
        split_cost += totals["input_tokens"] / 1_000_000 * (input_price_per_mtok or 0.0)
        split_cost += totals["output_tokens"] / 1_000_000 * (output_price_per_mtok or 0.0)

    unknown_tokens = totals["unknown_split_tokens"]
    if unknown_tokens and total_price_per_mtok is not None:
        return split_cost + unknown_tokens / 1_000_000 * total_price_per_mtok, "mixed"
    if unknown_tokens and has_split_price:
        return None, "unknown_split"
    if has_split_price:
        return split_cost, "split"
    if total_price_per_mtok is not None:
        total_tokens = totals["split_total_tokens"] + unknown_tokens
        return total_tokens / 1_000_000 * total_price_per_mtok, "total"
    return None, "no_price"


def print_report(args: argparse.Namespace) -> None:
    totals = collect(Path(args.trajectories_dir), args.model)
    total_tokens = totals["split_total_tokens"] + totals["unknown_split_tokens"]
    cost, mode = estimate_cost(
        totals,
        args.total_price_per_mtok,
        args.input_price_per_mtok,
        args.output_price_per_mtok,
    )

    print(f"model_filter: {args.model or '(all)'}")
    print(f"trajectory_files: {totals['files']}  runs: {totals['runs']}  llm_calls: {totals['calls']}")
    print(
        "tokens: "
        f"input={totals['input_tokens']} "
        f"output={totals['output_tokens']} "
        f"unknown_split={totals['unknown_split_tokens']} "
        f"total={total_tokens}"
    )
    print(f"latency_s: {totals['latency_s']:.1f}  estimated_calls: {totals['estimated_calls']}")
    if cost is None:
        if mode == "unknown_split":
            print("cost_usd: unknown because older logs only contain total tokens; set GLM_TOTAL_USD_PER_MTOK to price those logs.")
        else:
            print("cost_usd: set GLM_TOTAL_USD_PER_MTOK, or GLM_INPUT_USD_PER_MTOK and GLM_OUTPUT_USD_PER_MTOK")
    else:
        print(f"cost_usd_estimate: ${cost:.6f} ({mode})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize GLM token usage from OpenCollab trajectory logs")
    parser.add_argument(
        "--trajectories-dir",
        default="/Volumes/GKM/SSG/OpenCollab/logs/trajectories",
    )
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument("--total-price-per-mtok", type=float, default=_float_env("GLM_TOTAL_USD_PER_MTOK"))
    parser.add_argument("--input-price-per-mtok", type=float, default=_float_env("GLM_INPUT_USD_PER_MTOK"))
    parser.add_argument("--output-price-per-mtok", type=float, default=_float_env("GLM_OUTPUT_USD_PER_MTOK"))
    parser.add_argument("--watch", type=float, default=0.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    while True:
        print_report(args)
        if not args.watch:
            break
        print()
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

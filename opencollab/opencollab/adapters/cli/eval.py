"""Headless batch evaluation command for benchmarks (SWE-bench, etc.).

``eval_cmd`` is registered on the Typer app in ``cli.main``.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import typer
from rich.console import Console

from opencollab.adapters.cli.config_resolve import resolve_config

console = Console()


def eval_cmd(
    tasks_file: str = typer.Argument(..., help="JSONL file with eval tasks"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model (default from config)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider (default from config)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key (default from config)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (default from config)"),
    output_dir: str = typer.Option("eval_results", "--output", "-o"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Parallel tasks"),
    max_tokens: int = typer.Option(100_000, "--max-tokens"),
    timeout: float = typer.Option(600.0, "--timeout"),
):
    """Headless evaluation mode for benchmarks (SWE-bench, etc.)."""
    cfg = resolve_config(".", model, provider, api_key, base_url, None)
    asyncio.run(_eval(
        tasks_file=tasks_file, model=cfg["model"], provider=cfg["provider"],
        api_key=cfg["api_key"], base_url=cfg["base_url"], output_dir=output_dir,
        concurrency=concurrency, max_tokens=max_tokens, timeout=timeout,
        temperature=cfg["temperature"],
        thinking=cfg["thinking"], thinking_params=cfg["thinking_params"],
    ))


async def _eval(
    tasks_file: str, model: str, provider: str,
    api_key: str | None, base_url: str | None,
    output_dir: str, concurrency: int,
    max_tokens: int, timeout: float,
    temperature: float,
    thinking: bool = False,
    thinking_params: dict | None = None,
):
    from opencollab.harness.evaluator import EvalTask, run_eval_batch, save_results

    tasks = []
    with open(tasks_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            tasks.append(EvalTask(
                task_id=data["task_id"],
                description=data["description"],
                repo_path=data.get("repo_path"),
                docker_image=data.get("docker_image"),
                timeout=data.get("timeout", timeout),
                max_tokens=data.get("max_tokens", max_tokens),
            ))

    console.print(f"[bold]Running {len(tasks)} eval tasks[/bold] (concurrency={concurrency})")

    results = await run_eval_batch(
        tasks,
        concurrency=concurrency,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        output_dir=output_dir,
        temperature=temperature,
        thinking=thinking,
        thinking_params=thinking_params,
    )

    results_path = os.path.join(output_dir, "results.jsonl")
    save_results(results, results_path)

    produced = sum(1 for r in results if r.patch_produced)
    console.print(f"\n[bold]Results: {produced}/{len(results)} produced a patch[/bold]")
    console.print(f"Results saved to {results_path}")

    for r in results:
        status = "[green]PATCH[/green]" if r.patch_produced else "[red]NO PATCH[/red]"
        console.print(f"  {r.task_id}: {status} ({r.tokens_used:,} tokens, {r.duration:.1f}s)")

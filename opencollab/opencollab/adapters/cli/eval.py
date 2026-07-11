"""Headless batch evaluation command for benchmarks (SWE-bench, etc.).

``eval_cmd`` is registered on the Typer app in ``cli.main``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console

from opencollab.adapters.cli.config_resolve import resolve_config
from opencollab.application.async_timeout import run_with_bounded_shutdown
from opencollab.harness.swe_eval_records import open_regular_binary

console = Console()
MAX_EVAL_TASK_FILE_BYTES = 64 * 1024 * 1024
MAX_EVAL_TASK_LINE_BYTES = 8 * 1024 * 1024
MAX_EVAL_TASKS = 10_000


def _result_counts(results) -> tuple[int, int]:
    eligible_patches = sum(
        1 for result in results if result.patch_produced and result.submission_eligible
    )
    ineligible_results = sum(1 for result in results if not result.submission_eligible)
    return eligible_patches, ineligible_results


def _read_task_payloads(tasks_file: str) -> list[tuple[int, dict[str, Any]]]:
    path = Path(tasks_file)
    payloads: list[tuple[int, dict[str, Any]]] = []
    try:
        with open_regular_binary(path) as handle:
            file_size = os.fstat(handle.fileno()).st_size
            if file_size > MAX_EVAL_TASK_FILE_BYTES:
                raise ValueError(
                    f"eval tasks file exceeds {MAX_EVAL_TASK_FILE_BYTES}-byte limit: "
                    f"{tasks_file}"
                )
            bytes_read = 0
            line_number = 0
            while True:
                remaining_file_bytes = MAX_EVAL_TASK_FILE_BYTES - bytes_read
                line = handle.readline(
                    min(MAX_EVAL_TASK_LINE_BYTES, remaining_file_bytes) + 1
                )
                if not line:
                    break
                line_number += 1
                bytes_read += len(line)
                if bytes_read > MAX_EVAL_TASK_FILE_BYTES:
                    raise ValueError(
                        f"eval tasks file exceeds {MAX_EVAL_TASK_FILE_BYTES}-byte limit: "
                        f"{tasks_file}"
                    )
                if len(line) > MAX_EVAL_TASK_LINE_BYTES:
                    raise ValueError(
                        f"eval task line {line_number} exceeds "
                        f"{MAX_EVAL_TASK_LINE_BYTES}-byte limit"
                    )
                if not line.strip():
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"eval task line {line_number} is not valid UTF-8"
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"eval task line {line_number} is not valid JSON: {exc.msg}"
                    ) from exc
                except (ValueError, RecursionError) as exc:
                    raise ValueError(
                        f"eval task line {line_number} cannot be decoded safely"
                    ) from exc
                if not isinstance(data, dict):
                    raise ValueError(
                        f"eval task line {line_number} must be a JSON object"
                    )
                payloads.append((line_number, data))
                if len(payloads) > MAX_EVAL_TASKS:
                    raise ValueError(
                        f"eval tasks file exceeds {MAX_EVAL_TASKS}-task limit"
                    )
    except OSError as exc:
        raise ValueError(
            f"eval tasks file must be a readable regular file: {tasks_file}"
        ) from exc
    return payloads


def eval_cmd(
    tasks_file: str = typer.Argument(..., help="JSONL file with eval tasks"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model (default from config)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider (default from config)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key (default from config)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (default from config)"),
    output_dir: str = typer.Option("eval_results", "--output", "-o"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Parallel tasks"),
    max_tokens: int = typer.Option(1_000_000, "--max-tokens"),
    timeout: float = typer.Option(600.0, "--timeout"),
):
    """Headless evaluation mode for benchmarks (SWE-bench, etc.)."""
    cfg = resolve_config(".", model, provider, api_key, base_url, None)
    run_with_bounded_shutdown(_eval(
        tasks_file=tasks_file, model=cfg["model"], provider=cfg["provider"],
        api_key=cfg["api_key"], base_url=cfg["base_url"], output_dir=output_dir,
        concurrency=concurrency, max_tokens=max_tokens, timeout=timeout,
        temperature=cfg["temperature"], top_p=cfg.get("top_p"),
        thinking=cfg["thinking"], thinking_params=cfg["thinking_params"],
    ))


async def _eval(
    tasks_file: str, model: str, provider: str,
    api_key: str | None, base_url: str | None,
    output_dir: str, concurrency: int,
    max_tokens: int, timeout: float,
    temperature: float,
    top_p: float | None = None,
    thinking: bool = False,
    thinking_params: dict | None = None,
):
    from opencollab.harness.evaluator import EvalTask, run_eval_batch, save_results

    tasks = []
    for line_number, data in _read_task_payloads(tasks_file):
        extras = data.get("extras")
        if extras is not None and not isinstance(extras, dict):
            raise ValueError(
                f"eval task line {line_number} extras must be a JSON object"
            )
        if (
            isinstance(extras, dict)
            and "test_patch" in extras
            and not isinstance(extras["test_patch"], str)
        ):
            raise ValueError(
                f"eval task line {line_number} extras test_patch must be a string"
            )
        try:
            task_id = data["task_id"]
            description = data["description"]
        except KeyError as exc:
            raise ValueError(
                f"eval task line {line_number} is missing {exc.args[0]!r}"
            ) from exc
        tasks.append(EvalTask(
            task_id=task_id,
            description=description,
            repo_path=data.get("repo_path"),
            docker_image=data.get("docker_image"),
            timeout=data.get("timeout", timeout),
            max_tokens=data.get("max_tokens", max_tokens),
            extras=extras,
            harness_artifact_paths=(os.path.abspath(tasks_file),),
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
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
    )

    results_path = os.path.join(output_dir, "results.jsonl")
    save_results(results, results_path)

    produced, ineligible = _result_counts(results)
    console.print(
        f"\n[bold]Results: {produced}/{len(results)} eligible patches; "
        f"{ineligible} ineligible results[/bold]"
    )
    console.print(f"Results saved to {results_path}")

    for r in results:
        if not r.submission_eligible:
            status = "[red]INELIGIBLE[/red]"
        elif r.patch_produced:
            status = "[green]PATCH[/green]"
        else:
            status = "[red]NO PATCH[/red]"
        console.print(f"  {r.task_id}: {status} ({r.tokens_used:,} tokens, {r.duration:.1f}s)")

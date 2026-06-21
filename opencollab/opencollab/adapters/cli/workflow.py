"""``workflow`` CLI subcommands: list and run.

``opencollab workflow list``        — print registered workflows.
``opencollab workflow run <name>``  — run a workflow, printing phase/log
progress lines followed by the final result as a JSON block on stdout.

Workflows are discovered from a ``workflows/`` directory (override with
``OPENCOLLAB_WORKFLOWS_DIR``). Config resolution reuses the same file-first
machinery as the rest of the CLI, so a stale shell ``ANTHROPIC_API_KEY`` cannot
shadow the configured provider key.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import typer
from rich.console import Console

from opencollab.adapters.cli.config_resolve import (
    missing_api_key_for,
    print_missing_key_hint,
    resolve_config,
)
from opencollab.application.ports import EventPublisherPort
from opencollab.application.workflow_registry import Registry
from opencollab.bootstrap.session_factory import make_run_dir
from opencollab.bootstrap.workflow_runtime import discover_workflows, run_workflow

app = typer.Typer(name="workflow", help="Run deterministic Python workflows.")
console = Console()

DEFAULT_WORKFLOWS_DIR = "workflows"


def load_registry() -> Registry:
    """Discover workflows from the workflows directory.

    The directory is ``OPENCOLLAB_WORKFLOWS_DIR`` when set, else ``workflows/``
    relative to the current working directory.
    """
    directory = os.environ.get("OPENCOLLAB_WORKFLOWS_DIR", DEFAULT_WORKFLOWS_DIR)
    return discover_workflows(directory)


class _ConsoleEventSink:
    """Prints workflow phase/log events as simple progress lines."""

    def __init__(self, output: Console) -> None:
        self._console = output

    async def emit(self, event: Any) -> None:
        kind = getattr(event, "kind", "log")
        message = getattr(event, "message", str(event))
        marker = "==" if kind == "phase" else "--"
        self._console.print(f"[dim]{marker} {message}[/dim]")


@app.command(name="list")
def list_cmd() -> None:
    """List the registered workflows with their descriptions."""
    registry = load_registry()
    specs = registry.list_specs()
    if not specs:
        console.print("[dim]No workflows found.[/dim]")
        return
    for spec in specs:
        console.print(f"[bold]{spec.name}[/bold]  {spec.description}")


@app.command(name="run")
def run_cmd(
    name: str = typer.Argument(..., help="Name of the workflow to run"),
    args: str = typer.Option("{}", "--args", help="JSON object of workflow arguments"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model (default from config)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider (default from config)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key (default from config)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (default from config)"),
    workspace: str = typer.Option(".", "--workspace", "-w", help="Working directory"),
    budget: Optional[int] = typer.Option(
        None,
        "--budget",
        help="Max token budget (default: max(config budget, 500000); workflows fan out many sessions)",
    ),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Max concurrent agent sessions"),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="Persist each session's transcript under <workspace>/.opencollab/sessions/<run>/",
    ),
    trace: bool = typer.Option(
        True,
        "--trace/--no-trace",
        help="Record a fine-grained JSONL trajectory (llm_call/tool_exec, tokens, "
        "latency) at <run>/trajectory.jsonl. Requires --save.",
    ),
) -> None:
    """Run a workflow and print its result as JSON."""
    registry = load_registry()
    try:
        spec = registry.get(name)
    except KeyError:
        names = ", ".join(s.name for s in registry.list_specs()) or "(none)"
        console.print(f"[red]Unknown workflow:[/red] {name}. Available: {names}")
        raise typer.Exit(code=1) from None

    try:
        parsed_args = json.loads(args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid --args JSON:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if not isinstance(parsed_args, dict):
        console.print("[red]--args must be a JSON object.[/red]")
        raise typer.Exit(code=1)

    cfg = resolve_config(workspace, model, provider, api_key, base_url, budget)
    if missing_api_key_for(cfg["provider"], cfg["api_key"], cfg["base_url"]):
        print_missing_key_hint(console, cfg["provider"], cfg["base_url"])
        raise typer.Exit(code=1)

    # Workflows fan out many one-shot sessions (mirroring main.py's spawn-aware
    # default), so lift the fallback budget when --budget was not given. The
    # explicit budget arg stays None below, so run_workflow falls back to this
    # raised cfg["budget"].
    if budget is None:
        cfg["budget"] = max(cfg["budget"], 500_000)

    save_dir = make_run_dir(workspace) if save else None

    event_sink: EventPublisherPort = _ConsoleEventSink(console)
    result = asyncio.run(
        run_workflow(
            spec,
            parsed_args,
            cfg=cfg,
            workspace=workspace,
            event_sink=event_sink,
            budget=budget,
            max_concurrency=concurrency,
            save_dir=save_dir,
            trace=trace,
        )
    )
    if save_dir is not None:
        console.print(f"[dim]== sessions saved to {save_dir}[/dim]")
        if trace:
            trace_path = os.path.join(save_dir, "trajectory.jsonl")
            console.print(f"[dim]== trajectory at {trace_path}[/dim]")
    console.print(json.dumps(result, indent=2, default=str))


__all__ = ["app", "load_registry", "run_workflow"]

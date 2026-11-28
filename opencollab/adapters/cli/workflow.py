"""CLI commands to list workflows and run one with progress and JSON output.

Loads installed workflows and ``workflows/`` under the workspace, overridable
with ``OPENCOLLAB_WORKFLOWS_DIR``. File-first config resolution keeps stale
shell API keys from overriding the configured provider key.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Optional

import typer
from rich.console import Console
from rich.text import Text

from opencollab.adapters.cli.config_resolve import (
    missing_api_key_for,
    print_missing_key_hint,
    resolve_config,
    resolve_model_route,
)
from opencollab.adapters.cli.text_input import resolve_text_input
from opencollab.application._session_run_shared import _FILTERED_OUTPUT_REASON
from opencollab.application.async_timeout import run_with_bounded_shutdown
from opencollab.application.ports import EventPublisherPort
from opencollab.application.workflow_registry import Registry
from opencollab.bootstrap.session_factory import WORKFLOW_RUN_PREFIX, make_run_dir
from opencollab.bootstrap.workflow_runtime import WorkflowRuntimeResult, discover_workflows, run_workflow
from opencollab.domain.json_data import loads_finite_json

app = typer.Typer(name="workflow", help="Run deterministic Python workflows.")
console = Console()

DEFAULT_WORKFLOWS_DIR = "workflows"
MAX_CLI_TASK_FILE_BYTES = 4 * 1024 * 1024

def load_registry(workspace: str = ".") -> Registry:
    """Combine installed workflows with the workspace's workflow directory.

    Relative paths, including the default ``workflows/``, are resolved from
    ``workspace``. An absolute ``OPENCOLLAB_WORKFLOWS_DIR`` remains absolute.
    """
    directory = os.environ.get("OPENCOLLAB_WORKFLOWS_DIR", DEFAULT_WORKFLOWS_DIR)
    if not os.path.isabs(directory):
        directory = os.path.join(workspace, directory)
    return discover_workflows(directory, include_builtin=True)

class _ConsoleEventSink:
    """Prints workflow phase/log events as simple progress lines."""

    def __init__(self, output: Console) -> None:
        self._console = output

    async def emit(self, event: Any) -> None:
        kind = getattr(event, "kind", "log")
        message = getattr(event, "message", str(event))
        marker = "==" if kind == "phase" else "--"
        self._console.print(Text(f"{marker} {message}", style="dim"))

class _WorkflowOutputError(ValueError):
    runtime_result: WorkflowRuntimeResult

def _format_workflow_output(output: Any) -> str:
    try:
        return json.dumps(output, indent=2, default=str, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _WorkflowOutputError(f"Could not serialize workflow result as JSON. {exc}") from None

@app.command(name="list")
def list_cmd(
    workspace: str = typer.Option(".", "--workspace", "-w", help="Working directory"),
) -> None:
    """List the registered workflows with their descriptions."""
    registry = load_registry(workspace)
    specs = registry.list_specs()
    if not specs:
        console.print("[dim]No workflows found.[/dim]")
        return
    for spec in specs:
        console.print(f"[bold]{spec.name}[/bold]  {spec.description}")
    next_name = "duo" if any(spec.name == "duo" for spec in specs) else specs[0].name
    console.print(Text(f"Next run opencollab workflow run {shlex.quote(next_name)} --help", style="dim"))

@app.command(name="run")
def run_cmd(
    name: str = typer.Argument(..., help="Name of the workflow to run"),
    args: str = typer.Option("{}", "--args", help="JSON object of workflow arguments"),
    task: Optional[str] = typer.Option(None, "--task", help="Set workflow goal as plain text"),
    task_file: Optional[str] = typer.Option(
        None, "--task-file", help="Read workflow goal from a UTF-8 text file (max 4 MiB)",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model (default from config)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider (default from config)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key (default from config)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (default from config)"),
    workspace: str = typer.Option(".", "--workspace", "-w", help="Working directory"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Original run identity for workflow continuation"),
    budget: Optional[int] = typer.Option(
        None,
        "--budget",
        help="Max token budget. An explicit value overrides unbounded mode. The bounded default is at least 1,000,000.",
    ),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Max concurrent agent sessions"),
    task_concurrency: Optional[int] = typer.Option(
        None,
        "--task-concurrency",
        help="Max active parallel/pipeline units (defaults to --concurrency)",
    ),
    isolation: Optional[str] = typer.Option(None, "--isolation", help="Default agent environment strategy"),
    cleanup_timeout: Optional[float] = typer.Option(
        None, "--cleanup-timeout", min=0.001, help="Cleanup grace in seconds (default from the VM profile or runtime)",
    ),
    agent_profile: Optional[str] = typer.Option(
        None,
        "--agent-profile",
        help="Agent profile for every workflow role (base or a concrete profile such as single2)",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="Persist each session's transcript under <workspace>/.opencollab/sessions/<run>/",
    ),
    trace: bool = typer.Option(
        True,
        "--trace/--no-trace",
        help="Record the run's orchestration signals (phases/logs + per-step "
        "tokens/latency) at <run>/orchestration.jsonl. Requires --save.",
    ),
) -> None:
    """Run a workflow and print its result as JSON."""
    registry = load_registry(workspace)
    try:
        spec = registry.get(name)
    except KeyError:
        names = ", ".join(s.name for s in registry.list_specs()) or "(none)"
        console.print(f"[red]Unknown workflow:[/red] {name}. Available: {names}")
        raise typer.Exit(code=1) from None

    try:
        parsed_args = loads_finite_json(args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid --args JSON:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if not isinstance(parsed_args, dict):
        console.print("[red]--args must be a JSON object.[/red]")
        raise typer.Exit(code=1)

    if "goal" in parsed_args and (task is not None or task_file is not None):
        raise typer.BadParameter("--args already contains 'goal'. Use --args or --task/--task-file for goal.")
    task_text = resolve_text_input(
        task,
        task_file,
        text_option="--task",
        file_option="--task-file",
        max_bytes=MAX_CLI_TASK_FILE_BYTES,
    )
    if task_text is not None:
        parsed_args["goal"] = task_text

    cfg = resolve_config(workspace, model, provider, api_key, base_url, budget)
    model_cfg = resolve_model_route(cfg)
    if missing_api_key_for(
        model_cfg["provider"], model_cfg["api_key"], model_cfg["base_url"], model_cfg.get("auth_token"),
    ):
        print_missing_key_hint(console, model_cfg["provider"], model_cfg["base_url"])
        raise typer.Exit(code=1)

    # Fan-out workflows raise the fallback budget. Leave an explicit budget
    # unset so ``run_workflow`` uses the raised configuration value.
    if budget is None:
        cfg["budget"] = max(cfg["budget"], 1_000_000)

    save_dir = make_run_dir(workspace, prefix=WORKFLOW_RUN_PREFIX) if save else None

    event_sink: EventPublisherPort = _ConsoleEventSink(console)
    output_error = None
    try:
        result = run_with_bounded_shutdown(
            run_workflow(
                spec,
                parsed_args,
                cfg=cfg,
                workspace=workspace,
                event_sink=event_sink,
                budget=budget,
                limit_mode="explicit" if budget is not None else "environment",
                max_concurrency=concurrency,
                task_concurrency=task_concurrency,
                agent_profile=agent_profile,
                **({"isolation": isolation} if isolation is not None else {}),
                **({"cleanup_timeout": cleanup_timeout} if cleanup_timeout is not None else {}),
                **({"run_id": run_id} if run_id is not None else {}),
                save_dir=save_dir,
                trace=trace,
                return_details=True,
                format_output=_format_workflow_output,
            ),
            shutdown_timeout=(cleanup_timeout if cleanup_timeout is not None else
                              (cfg.get("vm") or {}).get("cleanup_timeout", 2.0)),
        )
    except _WorkflowOutputError as exc:
        result = exc.runtime_result
        output_error = exc
    for failure in getattr(result, "agent_failures", ()):
        if (failure["exception_type"], failure["provider_error_type"]) == ("AgentStopped", "content_filter"):
            message = f"-- agent stopped ({failure['label']}): {_FILTERED_OUTPUT_REASON}"
            console.print(Text(message, style="dim"))
    if save_dir is not None:
        console.print(f"[dim]== sessions saved to {save_dir}[/dim]")
        if trace:
            trace_path = os.path.join(save_dir, "orchestration.jsonl")
            console.print(f"[dim]== orchestration at {trace_path}[/dim]")
    if output_error is not None:
        typer.echo(str(output_error), err=True)
        for note in getattr(output_error, "__notes__", ()):
            typer.echo(note, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(result.output)
    if result.stop_reason is not None:
        raise typer.Exit(code=1)

__all__ = ["app", "load_registry", "run_workflow"]

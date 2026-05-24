"""CLI entry point — Typer-based.

Two surfaces:
- the default (no subcommand): one unified interactive agent. Agent 0 is the
  first session and can spawn child agents via the Scheduler.
- ``eval``: headless batch evaluation for benchmarks (SWE-bench, etc.).

Ref:
- kimi-cli: Typer app with callback, async main, session management
- opencode: bun run dev with agent mode selection
- Design doc: cli/main.py as the interface layer — parses args and drives the
  REPL only; all wiring lives in opencollab.bootstrap.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from html import escape
from typing import Any, Optional

import typer
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from rich.console import Console

app = typer.Typer(
    name="opencollab",
    help="OpenCollab — Minimal Multi-Agent Software Development Framework",
    add_completion=False,
)
console = Console()
_prompt_session: Any | None = None
_PROMPT_STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse bg:default fg:ansibrightblack",
    "bottom-toolbar.text": "noreverse bg:default fg:ansibrightblack",
})


def _get_prompt_session() -> Any:
    """Lazy-init prompt_toolkit session for robust terminal line editing (IME-safe)."""
    global _prompt_session
    if _prompt_session is None:
        from prompt_toolkit import PromptSession

        _prompt_session = PromptSession(style=_PROMPT_STYLE)
    return _prompt_session


async def _read_line(prompt_text: str, bottom_toolbar: Any = None) -> str:
    """Read one input line; fall back to rich console input if needed.

    ``bottom_toolbar`` (a callable returning text) renders a status line under
    the input, à la a HUD — used to show the live team roster at the prompt.
    """
    try:
        session = _get_prompt_session()
        return await session.prompt_async(prompt_text, bottom_toolbar=bottom_toolbar)
    except Exception:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: console.input(prompt_text))


_TOOLBAR_MUTED = "ansibrightblack"
_TOOLBAR_ACCENT = "ansicyan"
_TOOLBAR_SUCCESS = "ansigreen"
_TOOLBAR_WARNING = "ansiyellow"
_TOOLBAR_ERROR = "ansired"

_TOOLBAR_STATE_STYLES = {
    "busy": _TOOLBAR_WARNING,
    "running": _TOOLBAR_WARNING,
    "scheduled": _TOOLBAR_MUTED,
    "idle": _TOOLBAR_MUTED,
    "done": _TOOLBAR_SUCCESS,
    "completed": _TOOLBAR_SUCCESS,
    "failed": _TOOLBAR_ERROR,
    "cancelled": _TOOLBAR_WARNING,
}


def _toolbar_style(text: Any, color: str) -> str:
    return f'<style fg="{color}">{escape(str(text))}</style>'


def _display_team_state(entry: dict) -> str:
    if entry.get("busy"):
        return "busy"
    phase = entry.get("phase", "?")
    return "idle" if phase == "done" else phase


def _format_team_toolbar(snapshot: list[dict]) -> HTML | str:
    """One-line team roster for the prompt bottom toolbar."""
    if not snapshot:
        return ""
    parts = []
    for entry in snapshot:
        aid = entry.get("aid")
        label = "Lead" if aid == 0 else f"A{aid} {entry.get('role', '?')}"
        state = _display_team_state(entry)
        state_style = _TOOLBAR_STATE_STYLES.get(str(state).lower(), _TOOLBAR_MUTED)
        parts.append(
            _toolbar_style(label, _TOOLBAR_MUTED)
            + _toolbar_style("(", _TOOLBAR_MUTED)
            + _toolbar_style(state, state_style)
            + _toolbar_style(")", _TOOLBAR_MUTED)
        )
    separator = _toolbar_style("  ", _TOOLBAR_MUTED)
    return HTML(
        _toolbar_style("Team:", _TOOLBAR_ACCENT)
        + _toolbar_style(" ", _TOOLBAR_MUTED)
        + separator.join(parts)
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _required_env_key(provider: str | None) -> str:
    p = (provider or "openai").lower()
    return "ANTHROPIC_API_KEY" if p == "anthropic" else "OPENAI_API_KEY"


def _missing_api_key(provider: str | None, api_key: str | None) -> bool:
    if api_key:
        return False
    return not bool(os.environ.get(_required_env_key(provider)))


def _print_missing_key_hint(provider: str | None) -> None:
    from opencollab.adapters.tui import TUI

    tui = TUI(console)
    tui.print_welcome()
    env_key = _required_env_key(provider)
    console.print(
        f"[red]Missing API key[/red]: pass [bold]--api-key[/bold] or set [bold]{env_key}[/bold]."
    )


def _resolve_config(workspace: str, model: str | None, provider: str | None,
                     api_key: str | None, base_url: str | None, budget: int | None) -> dict:
    """Merge CLI args with .env defaults. CLI args take precedence."""
    from opencollab.bootstrap.config import get_config
    cfg = get_config(workspace)
    return {
        "model": model or cfg["model"],
        "provider": provider or cfg["provider"],
        "api_key": api_key or cfg["api_key"],
        "base_url": base_url or cfg["base_url"],
        "budget": budget if budget is not None else _safe_int(cfg["budget"], 200_000),
    }


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model (default from config)"),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="LLM provider (default from config or openai)"
    ),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key (default from config)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="API base URL (default from config)"),
    workspace: str = typer.Option(".", "--workspace", "-w", help="Working directory"),
    budget: Optional[int] = typer.Option(None, "--budget", help="Max token budget (default from config or 500000)"),
    session_file: Optional[str] = typer.Option(None, "--session", "-s", help="Resume from session JSONL"),
    trace: bool = typer.Option(False, "--trace", help="Enable trajectory recording"),
    yolo: bool = typer.Option(False, "--yolo", help="Auto-approve risky commands"),
    no_worktrees: bool = typer.Option(False, "--no-worktrees", help="Disable git worktree isolation"),
):
    """Interactive agent. Agent 0 works directly and can spawn child agents."""
    if ctx.invoked_subcommand is not None:
        return

    cfg = _resolve_config(workspace, model, provider, api_key, base_url, budget)
    if _missing_api_key(cfg["provider"], cfg["api_key"]):
        _print_missing_key_hint(cfg["provider"])
        raise typer.Exit(code=1)

    # Agent 0 can spawn, so default to the higher budget.
    if budget is None:
        cfg["budget"] = max(cfg["budget"], 500_000)
    asyncio.run(_run(workspace=workspace, cfg=cfg, session_file=session_file,
                     trace=trace, yolo=yolo, use_worktrees=not no_worktrees))


@app.command(name="eval")
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
    cfg = _resolve_config(".", model, provider, api_key, base_url, None)
    asyncio.run(_eval(
        tasks_file=tasks_file, model=cfg["model"], provider=cfg["provider"],
        api_key=cfg["api_key"], base_url=cfg["base_url"], output_dir=output_dir,
        concurrency=concurrency, max_tokens=max_tokens, timeout=timeout,
    ))


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _read_command(tui, bottom_toolbar: Any = None) -> str | None:
    """Prompt for a user line, returning None on EOF/interrupt."""
    was_suspended = tui.suspend_live()
    try:
        return await _read_line("> ", bottom_toolbar=bottom_toolbar)
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        tui.resume_live(was_suspended)


async def _repl_loop(tui: Any, handle_turn, bottom_toolbar: Any = None) -> None:
    """Shared REPL: read line, dispatch built-in slash commands, run a turn."""
    while True:
        line = await _read_command(tui, bottom_toolbar=bottom_toolbar)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit", "/exit", "/quit"):
            break
        result = await handle_turn(line)
        if result is False:
            break


async def _run(workspace: str, cfg: dict, session_file: str | None,
               trace: bool, yolo: bool, use_worktrees: bool):
    from opencollab.adapters.tui import TUI, TuiEventSink, TuiPermissionPolicy
    from opencollab.bootstrap import build_runtime_context, build_scheduler

    tui = TUI(console)
    tui.print_welcome()

    permission_policy = None
    if not yolo:
        permission_policy = TuiPermissionPolicy(render=tui, read_line=_read_line)

    ctx = build_runtime_context(
        workspace, cfg, trace=trace,
        event_sink=TuiEventSink(tui),
        permission_policy=permission_policy,
        run_id_prefix="scheduler-",
    )
    scheduler = build_scheduler(
        ctx, use_worktrees=use_worktrees, interactive=True,
        session_file=session_file, auto_save=True,
    )
    lead = scheduler.lead_session
    if session_file and os.path.exists(session_file):
        console.print(f"[dim]Restored session from {session_file}[/dim]")
    elif lead.auto_save_path:
        console.print(f"[dim]Session auto-saving to {lead.auto_save_path}[/dim]")

    cancel_event = asyncio.Event()

    async def turn(line: str) -> None:
        if line.lower() == "/save":
            path = f"session-{uuid.uuid4().hex[:8]}.jsonl"
            lead.save(path)
            console.print(f"[dim]Session saved to {path}[/dim]")
            return
        cancel_event.clear()
        tui.reset()
        tui.start_live()
        try:
            await scheduler.run(line)
        except KeyboardInterrupt:
            cancel_event.set()
        finally:
            tui.stop_live()
            tui.print_stats(scheduler.used_tokens, lead.step_count)

    def team_toolbar() -> str:
        return _format_team_toolbar(scheduler.team_snapshot())

    await _repl_loop(tui, turn, bottom_toolbar=team_toolbar)

    await scheduler.cleanup()
    if ctx.tracer:
        ctx.tracer.close()
        console.print(f"[dim]Trajectory saved to {ctx.tracer.path}[/dim]")
    console.print("[dim]Goodbye.[/dim]")


async def _eval(
    tasks_file: str, model: str, provider: str,
    api_key: str | None, base_url: str | None,
    output_dir: str, concurrency: int,
    max_tokens: int, timeout: float,
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
    )

    results_path = os.path.join(output_dir, "results.jsonl")
    save_results(results, results_path)

    passed = sum(1 for r in results if r.success)
    console.print(f"\n[bold]Results: {passed}/{len(results)} passed[/bold]")
    console.print(f"Results saved to {results_path}")

    for r in results:
        status = "[green]PASS[/green]" if r.success else "[red]FAIL[/red]"
        console.print(f"  {r.task_id}: {status} ({r.tokens_used:,} tokens, {r.duration:.1f}s)")


def main():
    app()


if __name__ == "__main__":
    main()

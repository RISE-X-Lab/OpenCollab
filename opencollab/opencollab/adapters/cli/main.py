"""CLI entry point — Typer-based.

Two surfaces:
- the default (no subcommand): one unified interactive agent. Agent 0 is the
  first session and can spawn child agents via the Scheduler.
- ``eval``: headless batch evaluation for benchmarks (SWE-bench, etc.).

Split by concern: ``toolbar`` (prompt bottom-toolbar), ``config_resolve``
(CLI args + .env merge, API-key checks), ``eval`` (headless eval command).
This module keeps the Typer app, the chat REPL, and ``main()``.

Ref:
- kimi-cli: Typer app with callback, async main, session management
- opencode: bun run dev with agent mode selection
- Design doc: cli/main.py as the interface layer — parses args and drives the
  REPL only; all wiring lives in opencollab.bootstrap.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

import typer
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from rich.console import Console

from opencollab.adapters.cli.config_resolve import (
    missing_api_key_for,
    print_missing_key_hint,
    resolve_config,
)
from opencollab.adapters.cli.eval import eval_cmd
from opencollab.adapters.cli.toolbar import format_team_toolbar
from opencollab.adapters.cli.workflow import app as workflow_app

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
# Cyan chevron input prompt — ties the input line to the brand accent.
_PROMPT = HTML('<style fg="ansicyan"><b>❯</b></style><style fg="ansibrightblack"> </style>')

app.command(name="eval")(eval_cmd)
app.add_typer(workflow_app, name="workflow")


def _get_prompt_session() -> Any:
    """Lazy-init prompt_toolkit session for robust terminal line editing (IME-safe)."""
    global _prompt_session
    if _prompt_session is None:
        from prompt_toolkit import PromptSession

        _prompt_session = PromptSession(style=_PROMPT_STYLE)
    return _prompt_session


async def _read_line(prompt_text: Any, bottom_toolbar: Any = None) -> str:
    """Read one input line; fall back to rich console input if needed.

    ``prompt_text`` may be a plain ``str`` or a prompt_toolkit ``HTML`` object
    (the styled chevron). ``bottom_toolbar`` (a callable returning text) renders
    a status line under the input, à la a HUD — used to show the live team
    roster at the prompt.
    """
    try:
        session = _get_prompt_session()
        return await session.prompt_async(prompt_text, bottom_toolbar=bottom_toolbar)
    except Exception:
        loop = asyncio.get_running_loop()
        fallback = prompt_text if isinstance(prompt_text, str) else "> "
        return await loop.run_in_executor(None, lambda: console.input(fallback))


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
    budget: Optional[int] = typer.Option(None, "--budget", help="Max token budget (default from config or 1000000)"),
    session_file: Optional[str] = typer.Option(
        None, "--session", "-s", help="Resume from a saved agent JSON (or legacy JSONL)"
    ),
    trace: bool = typer.Option(False, "--trace", help="Enable trajectory recording"),
    yolo: bool = typer.Option(False, "--yolo", help="Auto-approve risky commands"),
    no_worktrees: bool = typer.Option(False, "--no-worktrees", help="Disable git worktree isolation"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Run a single prompt and exit (no REPL)"),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file", help="Read the one-shot prompt from a file"),
):
    """Interactive agent. Agent 0 works directly and can spawn child agents."""
    if ctx.invoked_subcommand is not None:
        return

    cfg = resolve_config(workspace, model, provider, api_key, base_url, budget)
    if missing_api_key_for(cfg["provider"], cfg["api_key"], cfg["base_url"]):
        print_missing_key_hint(console, cfg["provider"], cfg["base_url"])
        raise typer.Exit(code=1)

    one_shot = _resolve_one_shot_prompt(prompt, prompt_file)

    # Agent 0 can spawn, so default to the higher budget.
    if budget is None:
        cfg["budget"] = max(cfg["budget"], 1_000_000)
    asyncio.run(_run(workspace=workspace, cfg=cfg, session_file=session_file,
                     trace=trace, yolo=yolo, use_worktrees=not no_worktrees,
                     one_shot_prompt=one_shot))


def _resolve_one_shot_prompt(prompt: str | None, prompt_file: str | None) -> str | None:
    if prompt and prompt_file:
        raise typer.BadParameter("--prompt and --prompt-file are mutually exclusive.")
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            raise typer.BadParameter(f"Cannot read --prompt-file: {exc}") from exc
        if not text.strip():
            raise typer.BadParameter(f"--prompt-file is empty: {prompt_file}")
        return text
    if prompt:
        return prompt
    return None


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _read_command(tui, bottom_toolbar: Any = None) -> str | None:
    """Prompt for a user line, returning None on EOF/interrupt."""
    was_suspended = tui.suspend_live()
    try:
        return await _read_line(_PROMPT, bottom_toolbar=bottom_toolbar)
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        tui.resume_live(was_suspended)


_EXIT_COMMANDS = frozenset({"exit", "quit", "/exit", "/quit"})


def _save_session(lead: Any) -> None:
    """`/save` REPL command: write the lead session to a fresh JSONL file."""
    path = f"session-{uuid.uuid4().hex[:8]}.jsonl"
    lead.save(path)
    console.print(f"[dim]Session saved to {path}[/dim]")


def _dispatch_repl_command(line: str, lead: Any) -> bool:
    """Handle a built-in REPL command. Returns True to keep looping, False to exit.

    ``None`` (not a command) is signalled by raising ``KeyError`` to the caller.
    """
    command = line.lower()
    if command in _EXIT_COMMANDS:
        return False
    if command == "/save":
        _save_session(lead)
        return True
    raise KeyError(line)


async def _repl_loop(tui: Any, handle_turn, lead: Any, bottom_toolbar: Any = None) -> None:
    """Shared REPL: read line, dispatch built-in slash commands, run a turn."""
    while True:
        line = await _read_command(tui, bottom_toolbar=bottom_toolbar)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            if not _dispatch_repl_command(line, lead):
                break
            continue
        except KeyError:
            pass
        result = await handle_turn(line)
        if result is False:
            break
        tui.print_turn_divider()


async def _run(workspace: str, cfg: dict, session_file: str | None,
               trace: bool, yolo: bool, use_worktrees: bool,
               one_shot_prompt: str | None = None):
    from opencollab.adapters.tui import (
        TUI,
        TuiAskUserPolicy,
        TuiEventSink,
        TuiPermissionPolicy,
    )
    from opencollab.bootstrap import build_runtime_context, build_scheduler

    tui = TUI(console, filter_messages=cfg["filter_messages"])
    tui.print_welcome(
        model=cfg["model"],
        provider=cfg["provider"],
        workspace=workspace,
        budget=cfg.get("budget"),
    )

    # ``--yolo`` only auto-approves risky commands; a human is still present, so
    # the ask-user tool stays interactive (routed through the TUI's suspend/resume).
    permission_policy = None
    if not yolo:
        permission_policy = TuiPermissionPolicy(render=tui, read_line=_read_line)
    ask_policy = TuiAskUserPolicy(render=tui, read_line=_read_line)

    ctx = build_runtime_context(
        workspace, cfg, trace=trace,
        event_sink=TuiEventSink(tui),
        permission_policy=permission_policy,
        ask_policy=ask_policy,
        run_id_prefix="scheduler-",
    )
    scheduler = build_scheduler(
        ctx, use_worktrees=use_worktrees, interactive=True,
        session_file=session_file, auto_save=True,
    )
    tui.set_team_provider(scheduler.team_roster)
    lead = scheduler.lead_session
    if session_file and os.path.exists(session_file):
        console.print(f"[grey46]─[/grey46] [grey58]restored from {session_file}[/grey58]")
    elif lead.auto_save_path:
        console.print(f"[grey46]─[/grey46] [grey58]session → {lead.auto_save_path}[/grey58]")

    async def turn(line: str) -> None:
        tui.reset()
        tui.start_live()
        try:
            await scheduler.run(line)
        except KeyboardInterrupt:
            pass  # abandon the turn, return to the REPL
        finally:
            tui.stop_live()
            tui.print_stats(scheduler.used_tokens, lead.step_count)

    def team_toolbar() -> str:
        return format_team_toolbar(scheduler.team_roster())

    if one_shot_prompt is not None:
        await turn(one_shot_prompt)
    else:
        await _repl_loop(tui, turn, lead, bottom_toolbar=team_toolbar)

    await scheduler.cleanup()
    if ctx.tracer:
        ctx.tracer.close()
        console.print(f"[grey46]─[/grey46] [grey58]trajectory → {ctx.tracer.path}[/grey58]")
    console.print("\n[grey46]─[/grey46] [grey58]session ended.[/grey58]")


def main():
    app()


if __name__ == "__main__":
    main()

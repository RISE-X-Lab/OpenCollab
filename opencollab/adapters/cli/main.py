"""CLI entry point — Typer-based.

Two surfaces:
- the default (no subcommand): one interactive team. Agent 0 is the first
  session and can spawn children; Tab selects any live session for follow-up.

Split by concern: ``toolbar`` (prompt bottom-toolbar), ``config_resolve``
(CLI args + .env merge and API-key checks).
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
import sys
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
from opencollab.adapters.cli.prompt_view import (
    build_agent_navigation_bindings,
    build_agent_prompt,
)
from opencollab.adapters.cli.toolbar import format_team_toolbar
from opencollab.adapters.cli.workflow import app as workflow_app
from opencollab.adapters.safe_files import read_regular_text
from opencollab.adapters.tui.theme import BRAND_VIOLET
from opencollab.application.async_timeout import run_with_bounded_shutdown
from opencollab.application.exception_notes import add_exception_note

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
# One OC-violet chevron; the rest of the prompt chrome stays neutral.
_PROMPT = HTML(
    f'<style fg="{BRAND_VIOLET}"><b>❯</b></style>'
    '<style fg="ansibrightblack"> </style>'
)
MAX_CLI_PROMPT_FILE_BYTES = 4 * 1024 * 1024

app.add_typer(workflow_app, name="workflow")


def _get_prompt_session() -> Any:
    """Lazy-init prompt_toolkit session for robust terminal line editing (IME-safe)."""
    global _prompt_session
    if _prompt_session is None:
        from prompt_toolkit import PromptSession

        _prompt_session = PromptSession(style=_PROMPT_STYLE, erase_when_done=True)
    return _prompt_session


async def _read_console_line(prompt_text: str) -> str:
    """Read a terminal line without leaving an executor thread on cancellation."""
    loop = asyncio.get_running_loop()
    try:
        stdin_fd = sys.stdin.fileno()
    except (AttributeError, OSError) as exc:
        raise RuntimeError("console input fallback requires a selectable stdin") from exc

    console.print(prompt_text, end="")
    result: asyncio.Future[str] = loop.create_future()

    def on_readable() -> None:
        if result.done():
            return
        try:
            line = sys.stdin.readline()
        except BaseException as exc:
            result.set_exception(exc)
            return
        if line == "":
            result.set_exception(EOFError())
            return
        result.set_result(line.rstrip("\r\n"))

    try:
        loop.add_reader(stdin_fd, on_readable)
    except (AttributeError, NotImplementedError) as exc:
        raise RuntimeError("console input fallback is unavailable on this event loop") from exc
    try:
        return await result
    finally:
        loop.remove_reader(stdin_fd)


async def _read_line(
    prompt_text: Any,
    bottom_toolbar: Any = None,
    key_bindings: Any = None,
) -> str:
    """Read one input line; fall back to rich console input if needed.

    ``prompt_text`` may be a plain ``str`` or a prompt_toolkit ``HTML`` object
    (the styled chevron). ``bottom_toolbar`` (a callable returning text) renders
    a status line under the input, à la a HUD — used to show the live team
    roster at the prompt.
    """
    try:
        session = _get_prompt_session()
        return await session.prompt_async(
            prompt_text,
            bottom_toolbar=bottom_toolbar,
            key_bindings=key_bindings,
        )
    except Exception:
        fallback = prompt_text if isinstance(prompt_text, str) else "> "
        return await _read_console_line(fallback)


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
    allow_local_child_tests: bool = typer.Option(
        False,
        "--allow-local-child-tests",
        help="Allow spawned agents to run project tests on the host without OS isolation",
    ),
    team_config: Optional[str] = typer.Option(
        None,
        "--team-config",
        help="Explicit team YAML file (default: OPENCOLLAB_TEAM_FILE or built-in lead-only)",
    ),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Run a single prompt and exit (no REPL)"),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file", help="Read the one-shot prompt from a file"),
    hold: bool = typer.Option(
        False,
        "--hold",
        help="Keep a completed one-shot TUI open for Tab inspection; press q to exit",
    ),
):
    """Interactive team. Agent 0 starts the session and can spawn child agents."""
    if ctx.invoked_subcommand is not None:
        return

    one_shot = _resolve_one_shot_prompt(prompt, prompt_file)
    if hold and one_shot is None:
        raise typer.BadParameter("--hold requires --prompt or --prompt-file.")

    cfg = resolve_config(workspace, model, provider, api_key, base_url, budget)
    if missing_api_key_for(cfg["provider"], cfg["api_key"], cfg["base_url"]):
        print_missing_key_hint(console, cfg["provider"], cfg["base_url"])
        raise typer.Exit(code=1)

    # Agent 0 can spawn, so default to the higher budget.
    if budget is None:
        cfg["budget"] = max(cfg["budget"], 1_000_000)
    run_with_bounded_shutdown(
        _run(
            workspace=workspace,
            cfg=cfg,
            session_file=session_file,
            trace=trace,
            yolo=yolo,
            use_worktrees=not no_worktrees,
            team_config_path=team_config,
            one_shot_prompt=one_shot,
            hold_after_run=hold,
            allow_unisolated_child_tests=allow_local_child_tests,
        )
    )


def _resolve_one_shot_prompt(prompt: str | None, prompt_file: str | None) -> str | None:
    if prompt is not None and prompt_file is not None:
        raise typer.BadParameter("--prompt and --prompt-file are mutually exclusive.")
    if prompt_file is not None:
        if not prompt_file.strip():
            raise typer.BadParameter("--prompt-file path must not be empty.")
        try:
            text = read_regular_text(
                prompt_file,
                max_bytes=MAX_CLI_PROMPT_FILE_BYTES,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise typer.BadParameter(f"Cannot read --prompt-file: {exc}") from exc
        if not text.strip():
            raise typer.BadParameter(f"--prompt-file is empty: {prompt_file}")
        return text
    if prompt is not None:
        if not prompt.strip():
            raise typer.BadParameter("--prompt must not be empty.")
        return prompt
    return None


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _read_command(tui, bottom_toolbar: Any = None) -> str | None:
    """Prompt for a user line, returning None on EOF/interrupt."""
    was_suspended = tui.suspend_live()
    try:
        return await _read_line(
            build_agent_prompt(tui, _PROMPT),
            bottom_toolbar=bottom_toolbar,
            key_bindings=build_agent_navigation_bindings(tui),
        )
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
        target_aid = tui.selected_aid
        result = await handle_turn(line, target_aid)
        if result is False:
            break
        tui.print_turn_divider()


async def _await_scheduler_cleanup(scheduler: Any) -> asyncio.CancelledError | None:
    """Keep the CLI-owned scheduler teardown alive through repeated cancel."""
    cleanup_task = asyncio.create_task(scheduler.cleanup())
    repeated_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(cleanup_task)
            return repeated_cancellation
        except asyncio.CancelledError as exc:
            if cleanup_task.done():
                cleanup_task.result()
                return repeated_cancellation or exc
            if repeated_cancellation is None:
                repeated_cancellation = exc


async def _run(
    workspace: str,
    cfg: dict,
    session_file: str | None,
    trace: bool,
    yolo: bool,
    use_worktrees: bool,
    *,
    team_config_path: str | None = None,
    one_shot_prompt: str | None = None,
    hold_after_run: bool = False,
    allow_unisolated_child_tests: bool = False,
):
    from opencollab.adapters.event_log import JsonlEventSink
    from opencollab.adapters.tui import (
        TUI,
        TuiAskUserPolicy,
        TuiEventSink,
        TuiPermissionPolicy,
    )
    from opencollab.application.event_bus import EventBus
    from opencollab.bootstrap import build_runtime_context, build_scheduler

    tui = TUI(console, filter_messages=cfg["filter_messages"])
    tui.print_welcome(
        model=cfg["model"],
        provider=cfg["provider"],
        workspace=workspace,
        budget=cfg.get("budget"),
        interactive=one_shot_prompt is None,
    )

    # ``--yolo`` only auto-approves risky commands; a human is still present, so
    # the ask-user tool stays interactive (routed through the TUI's suspend/resume).
    permission_policy = None
    if not yolo:
        permission_policy = TuiPermissionPolicy(render=tui, read_line=_read_line)
    ask_policy = TuiAskUserPolicy(render=tui, read_line=_read_line)

    event_sink = TuiEventSink(tui)
    events_file = os.environ.get("OPENCOLLAB_EVENTS_FILE")
    file_event_sink: JsonlEventSink | None = None
    if events_file:
        bus = EventBus(event_sink)
        file_event_sink = JsonlEventSink(events_file)
        bus.subscribe(file_event_sink)
        event_sink = bus

    ctx = build_runtime_context(
        workspace, cfg, trace=trace,
        event_sink=event_sink,
        permission_policy=permission_policy,
        ask_policy=ask_policy,
        run_id_prefix="scheduler-",
    )
    scheduler = None
    primary_failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    tracer_failure: BaseException | None = None
    repeated_cancellation: asyncio.CancelledError | None = None
    try:
        scheduler = build_scheduler(
            ctx, use_worktrees=use_worktrees, interactive=True,
            session_file=session_file, auto_save=True,
            team_config_path=team_config_path,
            allow_unisolated_child_tests=allow_unisolated_child_tests,
        )
        tui.set_team_provider(scheduler.team_roster)
        lead = scheduler.lead_session
        if session_file and os.path.exists(session_file):
            console.print(
                f"[grey46]─[/grey46] [grey58]restored from {session_file}[/grey58]"
            )
        elif lead.auto_save_path:
            console.print(
                f"[grey46]─[/grey46] [grey58]session → {lead.auto_save_path}[/grey58]"
            )

        async def turn(line: str, target_aid: int) -> None:
            tui.select_agent(target_aid)
            tui.reset()
            tui.record_user_message(target_aid, line)
            tui.start_live()
            completed = False
            try:
                await scheduler.run_turn(target_aid, line)
                completed = True
            except KeyboardInterrupt:
                pass  # abandon the turn, return to the REPL
            finally:
                try:
                    if completed and one_shot_prompt is not None and hold_after_run:
                        await tui.hold_live()
                finally:
                    tui.stop_live(
                        persist=one_shot_prompt is not None,
                        aid=target_aid,
                    )
                    tui.print_stats(
                        scheduler.used_tokens,
                        scheduler.agent_step_count(target_aid),
                    )

        def team_toolbar() -> str:
            return format_team_toolbar(
                scheduler.team_roster(),
                selected_aid=tui.selected_aid,
            )

        if one_shot_prompt is not None:
            await turn(one_shot_prompt, 0)
        else:
            await _repl_loop(tui, turn, lead, bottom_toolbar=team_toolbar)
    except BaseException as exc:
        primary_failure = exc
    if scheduler is not None:
        try:
            repeated_cancellation = await _await_scheduler_cleanup(scheduler)
        except BaseException as exc:
            cleanup_failure = exc
    if ctx.tracer:
        try:
            ctx.tracer.close()
        except BaseException as exc:
            tracer_failure = exc
        else:
            if getattr(ctx.tracer, "write_error", None):
                tracer_failure = OSError(
                    f"trajectory write failed: {ctx.tracer.write_error}"
                )
            else:
                try:
                    console.print(
                        f"[grey46]─[/grey46] "
                        f"[grey58]trajectory → {ctx.tracer.path}[/grey58]"
                    )
                except BaseException as exc:
                    tracer_failure = exc
    if file_event_sink is not None:
        try:
            event_write_error = file_event_sink.write_error
            dropped_events = file_event_sink.dropped_events
        except BaseException as exc:
            event_write_error = (
                "event sink diagnostics unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
            dropped_events = 0
        if event_write_error is not None or dropped_events:
            safe_error = " ".join(str(event_write_error or "unknown error").splitlines())
            try:
                print(
                    "Warning: event log persistence degraded for "
                    f"{events_file!r}; first error: {safe_error[:1000]}; "
                    f"dropped events: {dropped_events}",
                    file=sys.stderr,
                )
            except BaseException:
                pass
    if primary_failure is not None:
        if repeated_cancellation is not None:
            add_exception_note(
                primary_failure,
                "caller cancelled again during scheduler cleanup",
            )
        if cleanup_failure is not None:
            add_exception_note(
                primary_failure,
                "scheduler cleanup also failed: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}",
            )
        if tracer_failure is not None:
            add_exception_note(
                primary_failure,
                "tracer close also failed: "
                f"{type(tracer_failure).__name__}: {tracer_failure}",
            )
        raise primary_failure
    if cleanup_failure is not None:
        if tracer_failure is not None:
            add_exception_note(
                cleanup_failure,
                "tracer close also failed: "
                f"{type(tracer_failure).__name__}: {tracer_failure}",
            )
        raise cleanup_failure
    if repeated_cancellation is not None:
        if tracer_failure is not None:
            add_exception_note(
                repeated_cancellation,
                "tracer close also failed: "
                f"{type(tracer_failure).__name__}: {tracer_failure}",
            )
        raise repeated_cancellation
    if tracer_failure is not None:
        raise tracer_failure
    console.print("\n[grey46]─[/grey46] [grey58]session ended.[/grey58]")


def main():
    app()


if __name__ == "__main__":
    main()

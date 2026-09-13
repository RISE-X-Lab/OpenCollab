"""CLI entry point — Typer-based.

Two surfaces:
- the default (no subcommand): one interactive team. Agent 0 is the first
  session and can spawn children; Tab selects any live session for follow-up.

The input line is permanent: ``live_prompt`` owns the bottom of the terminal
for the whole session and ``turn_queue`` runs turns behind it, so typing during
a turn queues instead of waiting, and Ctrl+C interrupts the turn instead of
ending the session.

Split by concern: ``live_prompt`` (the prompt and the HUD above it),
``turn_queue`` (background turn execution), ``config_resolve`` (CLI args + .env
merge and API-key checks). This module keeps the Typer app, the read loop, and
``main()``.

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
from collections.abc import Awaitable
from typing import Any, Optional

import typer
from rich.console import Console
from rich.text import Text

from opencollab.adapters.cli.config_resolve import (
    missing_api_key_for,
    print_missing_key_hint,
    resolve_config,
)
from opencollab.adapters.cli.live_prompt import LivePrompt
from opencollab.adapters.cli.turn_queue import TurnQueue
from opencollab.adapters.cli.workflow import app as workflow_app
from opencollab.adapters.safe_files import read_regular_text
from opencollab.application.async_timeout import run_with_bounded_shutdown
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.scheduler_types import SchedulerTurnError

app = typer.Typer(
    name="opencollab",
    help="OpenCollab — Minimal Multi-Agent Software Development Framework",
    add_completion=False,
)
console = Console()
MAX_CLI_PROMPT_FILE_BYTES = 4 * 1024 * 1024

app.add_typer(workflow_app, name="workflow")


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
        help="Explicit team YAML file (default: OPENCOLLAB_TEAM_FILE or the built-in Self-Collaboration team)",
    ),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Run a single prompt and exit (no REPL)"),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file", help="Read the one-shot prompt from a file"),
    hold: bool = typer.Option(
        False,
        "--hold",
        help="Stay at the prompt after a one-shot run: Tab inspects an agent, q or exit quits",
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


def _print_hint(label: str, value: str) -> None:
    """One hairline-led chrome line, home-shortened and kept to a single row.

    Session and trajectory paths are long enough to wrap into three or four rows
    and push real output off the top. The head is elided rather than the tail:
    the run id and filename at the end are what identify the file.
    """
    home = os.path.expanduser("~")
    if home != "~" and value.startswith(home):
        value = "~" + value[len(home):]
    prefix = f"─ {label} → "
    budget = max(8, console.width - len(prefix))
    if len(value) > budget:
        value = "…" + value[-(budget - 1):]
    line = Text()
    line.append("─ ", style="grey46")
    line.append(f"{label} → {value}", style="grey58")
    console.print(line)


_EXIT_COMMANDS = frozenset({"exit", "quit", "/exit", "/quit"})
# ``--hold`` inherits the word the old post-run inspection view used for the
# same intent, now that there is no raw key listener to press it against.
_HOLD_EXIT_COMMANDS = _EXIT_COMMANDS | {"q"}


def _save_session(lead: Any) -> None:
    """`/save` REPL command: write the lead session to a fresh JSONL file."""
    path = f"session-{uuid.uuid4().hex[:8]}.jsonl"
    lead.save(path)
    console.print(f"[dim]Session saved to {path}[/dim]")


def _dispatch_repl_command(
    line: str,
    lead: Any,
    exit_words: frozenset[str] | set[str] = _EXIT_COMMANDS,
) -> bool:
    """Handle a built-in REPL command. Returns True to keep looping, False to exit.

    ``None`` (not a command) is signalled by raising ``KeyError`` to the caller.
    """
    command = line.lower()
    if command in exit_words:
        return False
    if command == "/save":
        try:
            _save_session(lead)
        except Exception as exc:
            console.print(
                Text(
                    f"Session save failed: {type(exc).__name__}: {exc}",
                    style="red",
                )
            )
        return True
    raise KeyError(line)


async def _read_loop(
    prompt: LivePrompt,
    queue: TurnQueue,
    tui: Any,
    lead: Any,
    *,
    exit_words: frozenset[str] | set[str] = _EXIT_COMMANDS,
) -> None:
    """Read the one input line until the user leaves it.

    Every line has three possible owners, in this order: an agent waiting on an
    answer, a built-in command, and finally the queue of turns. The agent wins
    because it asked first and the user can see whose question is on screen.
    """
    while True:
        try:
            line = await prompt.read()
        except EOFError:
            return
        except KeyboardInterrupt:
            # Ctrl+C is an interrupt while there is something to interrupt, and
            # the way out otherwise. Both halves run: a question left standing
            # would keep the tool that asked it parked on an answer that is
            # never coming.
            declined = prompt.decline_pending()
            interrupted = queue.interrupt()
            if declined or interrupted:
                continue
            return
        if prompt.deliver(line):
            continue
        line = line.strip()
        if not line:
            continue
        try:
            if not _dispatch_repl_command(line, lead, exit_words):
                return
            continue
        except KeyError:
            pass
        queue.submit(line, tui.selected_aid)


def _report_turn_failure(tui: Any, exc: SchedulerTurnError) -> None:
    """Print what a failed turn left behind, without repeating what it printed."""
    # The half-finished answer is salvage: print it only if the turn's cleanup
    # did not already settle the streamed text into scrollback, or the user
    # reads the same partial answer twice.
    if exc.partial_answer and not tui.drained_partial_answer(exc.aid):
        console.print(Text(exc.partial_answer))
    reason = exc.terminal_reason or exc.phase.value
    style = "yellow" if exc.phase.value == "stopped" else "red"
    console.print(Text(f"Agent {exc.aid} {exc.phase.value}: {reason}", style=style))


async def _drive(
    queue: TurnQueue,
    *,
    reader: Awaitable[None] | None,
    until_drained: bool,
) -> None:
    """Run queued turns while the input line stays live, until one side ends.

    Three ways out: the user leaves the prompt, a one-shot run drains its
    queue, or a turn fails outright — and that failure is re-raised here
    because it is the same failure the REPL used to take inline.
    """
    if reader is None and not until_drained:
        raise ValueError("a run with nothing reading must end when its queue drains")
    watchers = [asyncio.create_task(queue.run(), name="turn-worker")]
    if reader is not None:
        watchers.append(asyncio.create_task(reader, name="prompt-reader"))
    if until_drained:
        watchers.append(asyncio.create_task(queue.drain(), name="turn-drain"))
    try:
        done, _pending = await asyncio.wait(
            watchers, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in watchers:
            task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            raise task.exception()


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
    # A redirected or piped run has no bottom region to own: nothing is painted
    # and lines are read straight from stdin.
    has_screen = sys.stdin.isatty() and sys.stdout.isatty()
    prompt = LivePrompt(tui, console=console, interactive=has_screen)
    tui.print_welcome(
        model=cfg["model"],
        provider=cfg["provider"],
        workspace=workspace,
        budget=cfg.get("budget"),
        interactive=one_shot_prompt is None or (hold_after_run and has_screen),
    )

    # ``--yolo`` only auto-approves risky commands; a human is still present, so
    # the ask-user tool stays interactive — its question takes the shared input
    # line ahead of whatever the user was typing.
    permission_policy = None
    if not yolo:
        permission_policy = TuiPermissionPolicy(read_line=prompt.ask)
    ask_policy = TuiAskUserPolicy(read_line=prompt.ask)

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
            _print_hint("restored from", str(session_file))
        elif lead.auto_save_path:
            _print_hint("session", str(lead.auto_save_path))

        one_shot = one_shot_prompt is not None
        # Holding means staying at the prompt, so a run with no screen to hold
        # it on has nothing to stay for.
        holding = hold_after_run and has_screen

        async def turn(line: str, target_aid: int, cancel: asyncio.Event) -> None:
            tui.select_agent(target_aid)
            tui.reset()
            tui.record_user_message(target_aid, line)
            tui.start_turn(target_aid)
            failure: SchedulerTurnError | None = None
            try:
                await scheduler.run_turn(target_aid, line, cancel_event=cancel)
            except SchedulerTurnError as exc:
                failure = exc
            except KeyboardInterrupt:
                pass  # abandon the turn, return to the prompt
            finally:
                # Only the focused agent's blocks reach scrollback, and a
                # one-shot run has no "later" in which to Tab back. If the
                # user wandered off to watch a teammate, flush the tail of
                # the agent that owes them an answer. The tail, not a focus
                # switch: a switch reprints in full and would repeat every
                # row this agent already put on screen.
                tui.settle_turn(
                    final_aid=target_aid if one_shot else None
                )
                tui.print_stats(
                    scheduler.used_tokens,
                    scheduler.agent_step_count(target_aid),
                )
            if failure is not None:
                _report_turn_failure(tui, failure)
            if not one_shot or holding:
                # The rule separates turns. A run that ends with this one has
                # nothing to separate it from.
                tui.print_turn_divider()

        queue = TurnQueue(turn, on_depth=tui.set_queued_turns)
        exit_words = _EXIT_COMMANDS
        with prompt.attached():
            if one_shot:
                queue.submit(one_shot_prompt, 0)
                if holding:
                    exit_words = _HOLD_EXIT_COMMANDS
                    _print_hint("hold", "Tab inspects an agent · q quits")
            # A one-shot run still reads: its agents can ask for permission,
            # and Ctrl+C has to reach the turn. Only a run with no screen at
            # all skips the loop and lets the policies read stdin directly.
            reader = (
                _read_loop(prompt, queue, tui, lead, exit_words=exit_words)
                if not one_shot or has_screen
                else None
            )
            await _drive(
                queue,
                reader=reader,
                until_drained=one_shot and not holding,
            )
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
                    _print_hint("trajectory", str(ctx.tracer.path))
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

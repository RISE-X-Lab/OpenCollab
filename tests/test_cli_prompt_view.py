from __future__ import annotations

import asyncio
import fcntl
import io
import os
import struct
import termios
from functools import partial
from types import SimpleNamespace

import prompt_toolkit
import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from opencollab.adapters.cli import main as cli_main
from opencollab.adapters.cli.prompt_view import build_agent_navigation_bindings
from opencollab.adapters.tui import TUI
from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.domain.session import SessionPhase


class _FakeTUI:
    def __init__(self) -> None:
        self.selected_aid = 0
        self.gates: list = []
        self.drained_partials: set[int] = set()

    def set_scrollback_gate(self, gate) -> None:
        self.gates.append(gate)

    def drained_partial_answer(self, aid: int) -> bool:
        return aid in self.drained_partials

    def select_next_agent(self) -> int | None:
        self.selected_aid = 1 if self.selected_aid == 0 else 2
        return self.selected_aid

    def select_previous_agent(self) -> int | None:
        self.selected_aid = 1
        return self.selected_aid


def test_prompt_scrollback_gate_hands_printing_to_prompt_toolkit(monkeypatch):
    import prompt_toolkit.application as pt_application

    handed: list = []
    monkeypatch.setattr(pt_application, "run_in_terminal", handed.append)

    def emit() -> None:
        raise AssertionError("the gate must not print behind prompt_toolkit's back")

    cli_main._prompt_scrollback_gate(emit)

    assert handed == [emit]


@pytest.mark.asyncio
async def test_console_input_fallback_removes_reader_when_cancelled(monkeypatch):
    class BrokenPromptSession:
        async def prompt_async(self, *_args, **_kwargs):
            raise RuntimeError("prompt toolkit unavailable")

    read_fd, write_fd = os.pipe()
    input_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_get_prompt_session", lambda: BrokenPromptSession())
    monkeypatch.setattr(cli_main.sys, "stdin", input_stream)
    task = asyncio.create_task(cli_main._read_line("> "))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert asyncio.get_running_loop().remove_reader(read_fd) is False
    input_stream.close()
    os.close(write_fd)


@pytest.mark.asyncio
async def test_console_input_fallback_reads_selectable_pipe(monkeypatch):
    class BrokenPromptSession:
        async def prompt_async(self, *_args, **_kwargs):
            raise RuntimeError("prompt toolkit unavailable")

    read_fd, write_fd = os.pipe()
    input_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_get_prompt_session", lambda: BrokenPromptSession())
    monkeypatch.setattr(cli_main.sys, "stdin", input_stream)
    task = asyncio.create_task(cli_main._read_line("> "))
    await asyncio.sleep(0)
    os.write(write_fd, b"fallback input\n")

    assert await asyncio.wait_for(task, timeout=0.2) == "fallback input"
    assert asyncio.get_running_loop().remove_reader(read_fd) is False
    input_stream.close()
    os.close(write_fd)


def test_cli_prompt_session_erases_managed_viewport(monkeypatch):
    captured = {}
    session = object()

    def fake_prompt_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr(prompt_toolkit, "PromptSession", fake_prompt_session)
    monkeypatch.setattr(cli_main, "_prompt_session", None)

    assert cli_main._get_prompt_session() is session
    assert captured["erase_when_done"] is True


def test_prompt_tab_bindings_switch_agents_without_touching_input_buffer():
    tui = _FakeTUI()
    bindings = build_agent_navigation_bindings(tui)
    app = SimpleNamespace(invalidations=0)
    app.invalidate = lambda: setattr(app, "invalidations", app.invalidations + 1)
    buffer = SimpleNamespace(text="draft command", cursor_position=5)
    event = SimpleNamespace(app=app, current_buffer=buffer)

    bindings.get_bindings_for_keys((Keys.ControlI,))[-1].handler(event)
    assert tui.selected_aid == 1
    assert buffer.text == "draft command"
    assert buffer.cursor_position == 5

    tui.selected_aid = 2
    bindings.get_bindings_for_keys((Keys.BackTab,))[-1].handler(event)
    assert tui.selected_aid == 1
    assert app.invalidations == 2
    assert bindings.get_bindings_for_keys((Keys.Up,)) == []
    assert bindings.get_bindings_for_keys((Keys.Down,)) == []
    assert bindings.get_bindings_for_keys((Keys.Left,)) == []
    assert bindings.get_bindings_for_keys((Keys.Right,)) == []


@pytest.mark.asyncio
async def test_prompt_session_processes_tab_navigation_without_editing_text():
    tui = _FakeTUI()
    bindings = build_agent_navigation_bindings(tui)
    with create_pipe_input() as pipe_input:
        session = PromptSession(input=pipe_input, output=DummyOutput())
        prompt = asyncio.create_task(
            session.prompt_async("> ", key_bindings=bindings)
        )
        await asyncio.sleep(0)
        pipe_input.send_text("draft")
        pipe_input.send_bytes(b"\t\t\x1b[Z")
        pipe_input.send_text(" command\r")

        result = await asyncio.wait_for(prompt, timeout=2)

    assert result == "draft command"
    assert tui.selected_aid == 1


@pytest.mark.asyncio
async def test_prompt_tab_reprints_agent_into_scrollback_without_erasing_it(monkeypatch):
    """Tab appends the newly focused agent to scrollback; it never rewrites it.

    The old renderer carried each agent's history *inside* the prompt, so a
    switch had to erase the previous agent's rows — and once that history grew
    past the terminal height the erase could no longer reach them. Here the
    reprint is ordinary terminal output that prompt_toolkit scrolls away from.
    """
    monkeypatch.setenv("PROMPT_TOOLKIT_NO_CPR", "1")
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 80, 0, 0))
    output_stream = os.fdopen(os.dup(slave_fd), "w", encoding="utf-8", buffering=1)
    output = Vt100_Output.from_pty(output_stream, term="xterm")

    console = Console(file=output_stream, force_terminal=True, width=80, height=12)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "analyst", "phase": "idle", "busy": False},
        {"aid": 1, "role": "coder", "phase": "idle", "busy": False},
    ])
    # Focus is on agent 0, so agent 1's transcript stays unprinted until Tab.
    tui.record_user_message(0, "SETTLED-ON-ANALYST")
    tui.record_user_message(1, "PENDING-ON-CODER")

    raw = bytearray()
    settled_seen = asyncio.Event()
    reprint_seen = asyncio.Event()
    loop = asyncio.get_running_loop()

    def drain_output() -> None:
        while True:
            try:
                data = os.read(master_fd, 65_536)
            except BlockingIOError:
                break
            if not data:
                break
            raw.extend(data)
        if b"SETTLED-ON-ANALYST" in raw:
            settled_seen.set()
        if b"PENDING-ON-CODER" in raw:
            reprint_seen.set()

    loop.add_reader(master_fd, drain_output)
    try:
        with create_pipe_input() as pipe_input:
            session = PromptSession(
                input=pipe_input, output=output, erase_when_done=True
            )
            monkeypatch.setattr(cli_main, "_get_prompt_session", lambda: session)
            command = asyncio.create_task(cli_main._read_command(tui))
            await asyncio.wait_for(settled_seen.wait(), timeout=2)
            pipe_input.send_bytes(b"\t")
            await asyncio.wait_for(reprint_seen.wait(), timeout=2)
            pipe_input.send_text("hello\r")
            assert await asyncio.wait_for(command, timeout=2) == "hello"
            await asyncio.sleep(0)
    finally:
        loop.remove_reader(master_fd)
        drain_output()
        output_stream.close()
        os.close(master_fd)
        os.close(slave_fd)

    stream = bytes(raw)
    assert tui.selected_aid == 1
    # The band names the agent being redrawn, and the redraw is appended after
    # the analyst's settled line rather than replacing it.
    assert b"A1" in stream
    assert stream.index(b"SETTLED-ON-ANALYST") < stream.index(b"PENDING-ON-CODER")
    assert b"\x1b[2J" not in stream  # never a full-screen wipe
    # The gate is prompt-scoped: it is removed once the line is read.
    assert tui._scrollback_gate is None


@pytest.mark.asyncio
async def test_read_command_uses_a_static_prompt_and_a_prompt_scoped_gate(monkeypatch):
    tui = _FakeTUI()
    tui.suspend_live = lambda: False
    resumed: list[bool] = []
    tui.resume_live = resumed.append
    captured = {}

    async def fake_read_line(prompt_text, bottom_toolbar=None, key_bindings=None):
        captured["prompt"] = prompt_text
        captured["toolbar"] = bottom_toolbar
        captured["bindings"] = key_bindings
        captured["gate_during_prompt"] = tui.gates[-1]
        return "do work"

    monkeypatch.setattr(cli_main, "_read_line", fake_read_line)

    def toolbar() -> str:
        return "AGENTS"

    assert await cli_main._read_command(tui, bottom_toolbar=toolbar) == "do work"
    assert captured["toolbar"] is toolbar
    # No history is smuggled into the prompt any more — it is the bare chevron.
    assert captured["prompt"] is cli_main._PROMPT
    assert captured["bindings"].get_bindings_for_keys((Keys.ControlI,))
    assert captured["gate_during_prompt"] is cli_main._prompt_scrollback_gate
    assert tui.gates == [cli_main._prompt_scrollback_gate, None]
    assert resumed == [False]


@pytest.mark.asyncio
async def test_agent_asked_prompts_are_gated_like_the_repl_prompt(monkeypatch):
    """A permission y/N and an ask_user question own the screen too.

    Both are read while other agents keep running, so an ungated settled block
    from a concurrent agent would print into prompt_toolkit's redraw.
    """
    from opencollab.adapters.tui import TuiAskUserPolicy, TuiPermissionPolicy

    tui = _FakeTUI()
    tui.suspend_live = lambda: True
    tui.resume_live = lambda was_suspended: None
    seen: list = []

    async def fake_read_line(prompt_text, **kwargs):
        seen.append((prompt_text, tui.gates[-1]))
        return "y"

    monkeypatch.setattr(cli_main, "_read_line", fake_read_line)
    read_line = partial(cli_main._read_line_at_prompt, tui)

    assert await TuiPermissionPolicy(render=tui, read_line=read_line).confirm("Allow?") is True
    assert await TuiAskUserPolicy(render=tui, read_line=read_line).ask("Which one?") == "y"

    assert [gate for _prompt, gate in seen] == [cli_main._prompt_scrollback_gate] * 2
    assert tui.gates[-1] is None


@pytest.mark.asyncio
async def test_repl_routes_input_to_selected_agent(monkeypatch):
    tui = _FakeTUI()
    tui.selected_aid = 2
    lines = iter(["message selected agent", None])
    monkeypatch.setattr(
        cli_main,
        "_read_command",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=next(lines)),
    )
    calls = []

    async def handle_turn(line: str, aid: int) -> None:
        calls.append((aid, line))

    tui.print_turn_divider = lambda: None
    await cli_main._repl_loop(tui, handle_turn, object())

    assert calls == [(2, "message selected agent")]


@pytest.mark.asyncio
async def test_repl_reports_stopped_turn_and_accepts_next_input(monkeypatch):
    tui = _FakeTUI()
    tui.selected_aid = 2
    lines = iter(["first", "second", None])
    monkeypatch.setattr(
        cli_main,
        "_read_command",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=next(lines)),
    )
    printed: list[str] = []
    monkeypatch.setattr(
        cli_main,
        "console",
        SimpleNamespace(print=lambda value: printed.append(str(value))),
    )
    calls: list[tuple[int, str]] = []

    async def handle_turn(line: str, aid: int) -> None:
        calls.append((aid, line))
        if line == "first":
            raise SchedulerTurnError(
                aid,
                SessionPhase.STOPPED,
                "token budget exhausted",
                "partial answer",
            )

    dividers: list[None] = []
    tui.print_turn_divider = lambda: dividers.append(None)

    await cli_main._repl_loop(tui, handle_turn, object())

    assert calls == [(2, "first"), (2, "second")]
    assert printed == [
        "partial answer",
        "Agent 2 stopped: token budget exhausted",
    ]
    assert len(dividers) == 2


@pytest.mark.asyncio
async def test_stopped_turn_does_not_print_a_partial_answer_already_in_scrollback(monkeypatch):
    """The partial answer is salvage, and the turn's cleanup may have got there first.

    ``stop_live`` settles the trailing streamed text into scrollback, so printing
    ``partial_answer`` unconditionally shows the user the same text twice.
    """
    tui = _FakeTUI()
    tui.selected_aid = 2
    tui.drained_partials = {2}
    lines = iter(["first", None])
    monkeypatch.setattr(
        cli_main,
        "_read_command",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=next(lines)),
    )
    printed: list[str] = []
    monkeypatch.setattr(
        cli_main,
        "console",
        SimpleNamespace(print=lambda value: printed.append(str(value))),
    )

    async def handle_turn(line: str, aid: int) -> None:
        raise SchedulerTurnError(aid, SessionPhase.STOPPED, "token budget exhausted", "partial answer")

    tui.print_turn_divider = lambda: None

    await cli_main._repl_loop(tui, handle_turn, object())

    assert printed == ["Agent 2 stopped: token budget exhausted"]


@pytest.mark.asyncio
async def test_repl_prints_partial_answer_as_literal_rich_text(monkeypatch):
    tui = _FakeTUI()
    lines = iter(["first", "second", None])
    monkeypatch.setattr(
        cli_main,
        "_read_command",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=next(lines)),
    )
    output = io.StringIO()
    monkeypatch.setattr(
        cli_main,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )
    calls: list[str] = []

    async def handle_turn(line: str, _aid: int) -> None:
        calls.append(line)
        if line == "first":
            raise SchedulerTurnError(
                0,
                SessionPhase.STOPPED,
                "token budget exhausted",
                "Here is malformed rich: [bold]x[/red]",
            )

    tui.print_turn_divider = lambda: None

    await cli_main._repl_loop(tui, handle_turn, object())

    assert calls == ["first", "second"]
    assert "Here is malformed rich: [bold]x[/red]" in output.getvalue()
    assert "Agent 0 stopped: token budget exhausted" in output.getvalue()

"""The one input line: what it paints, and who gets to read from it."""

from __future__ import annotations

import asyncio
import fcntl
import os
import struct
import termios
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from opencollab.adapters.cli.live_prompt import (
    LivePrompt,
    _pin_status_row_under_input,
    _Question,
    read_console_line,
)
from opencollab.adapters.cli.prompt_view import build_agent_navigation_bindings
from opencollab.adapters.tui import TUI, TuiAskUserPolicy, TuiPermissionPolicy


class _FakeTUI:
    def __init__(self, hud: str | None = None, status: str | None = None) -> None:
        self.selected_aid = 0
        self.redraws: list = []
        self._hud = hud
        self._status = status

    def set_redraw(self, redraw) -> None:
        self.redraws.append(redraw)

    def hud_ansi(self, width=None) -> str | None:
        return self._hud

    def status_ansi(self, width=None) -> str | None:
        return self._status

    def select_next_agent(self) -> int | None:
        self.selected_aid = 1 if self.selected_aid == 0 else 2
        return self.selected_aid

    def select_previous_agent(self) -> int | None:
        self.selected_aid = 1
        return self.selected_aid


def _text(fragments) -> str:
    return "".join(text for _style, text in fragments)


SCREEN_WIDTH, SCREEN_HEIGHT = 80, 12


def _render_layout_rows(app) -> list[str]:
    """Render a prompt's layout into a screen and read its rows back."""
    with set_app(app):
        screen = Screen()
        app.layout.container.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT),
            "",
            False,
            None,
        )
    return [
        "".join(screen.data_buffer[y][x].char for x in range(SCREEN_WIDTH)).rstrip()
        for y in range(SCREEN_HEIGHT)
    ]


def test_the_prompt_paints_the_hud_above_its_own_input_line():
    """The HUD is rows the prompt owns, not output printed near it.

    Two in-place redrawers cannot share rows, so the in-flight frame is
    rendered to ANSI by the TUI and handed over as part of the message.
    """
    prompt = LivePrompt(_FakeTUI("▸ bash · 2s   AGENTS 1/2"), console=Console())

    rendered = _text(prompt._message())

    assert rendered == "▸ bash · 2s   AGENTS 1/2\n❯ "


def test_the_status_row_is_handed_to_the_toolbar_below_the_input_line():
    """The answer above the input line, the status row below it.

    The row is a ``bottom_toolbar``, so it must not also be in the message —
    the message is painted above the cursor, and a row in both frames is a row
    printed twice.
    """
    prompt = LivePrompt(_FakeTUI("◆ streaming", "● thinking  AGENTS 1/3"), console=Console())
    prompt._session = SimpleNamespace(
        app=SimpleNamespace(renderer=SimpleNamespace(height_is_known=True))
    )

    assert _text(prompt._message()) == "◆ streaming\n❯ "
    assert _text(prompt._status()) == "● thinking  AGENTS 1/3"


def test_the_status_row_moves_back_above_the_input_when_the_toolbar_cannot_paint():
    """A terminal that answers no cursor-position request gets no toolbar.

    prompt_toolkit holds a ``bottom_toolbar`` back until the renderer knows the
    room below the cursor. The status row is the run's only evidence of what
    the team is doing, so it goes back above the input rather than disappearing.
    """
    prompt = LivePrompt(_FakeTUI("◆ streaming", "● thinking  AGENTS 1/3"), console=Console())
    prompt._session = SimpleNamespace(
        app=SimpleNamespace(renderer=SimpleNamespace(height_is_known=False))
    )

    assert _text(prompt._message()) == "◆ streaming\n● thinking  AGENTS 1/3\n❯ "


@pytest.mark.asyncio
async def test_a_question_holding_the_input_line_keeps_the_status_row_under_it():
    prompt = LivePrompt(_FakeTUI(None, "● thinking  AGENTS 1/3"), console=Console())
    prompt._session = SimpleNamespace(
        app=SimpleNamespace(renderer=SimpleNamespace(height_is_known=True))
    )
    loop = asyncio.get_running_loop()
    prompt._questions.append(_Question("[Agent asks] Which one?\n> ", loop.create_future()))

    assert _text(prompt._message()) == "[Agent asks] Which one?\n> "
    assert _text(prompt._status()) == "● thinking  AGENTS 1/3"


@pytest.mark.asyncio
async def test_the_status_row_lands_on_the_row_under_the_input_line():
    """Where the two frames end up on screen, row by row.

    A prompt claims every row between the cursor and the bottom of the screen,
    which is slack whenever it is not already at the bottom. Left to spread,
    that slack lands *between* the input line and the toolbar, and the status
    row drifts down the screen. ``_pin_status_row_under_input`` is what keeps
    the two adjacent, so this renders the real layout into a screen far taller
    than its content and reads the rows back.
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(input=pipe_input, output=DummyOutput())
        _pin_status_row_under_input(session)
        prompt = LivePrompt(_FakeTUI("HUD-BODY-ROW", "STATUS-ROW"), console=Console())
        prompt._session = session
        session.message = prompt._message
        session.bottom_toolbar = prompt._status

        app = session.app
        # A toolbar is held back until the renderer knows the room below the
        # cursor; a DummyOutput never reports it, so state the room outright.
        app.renderer._min_available_height = SCREEN_HEIGHT
        rows = _render_layout_rows(app)

    assert rows[0] == "HUD-BODY-ROW"
    assert rows[1].startswith("❯")
    assert rows[2] == "STATUS-ROW"
    # ...and every row the prompt claimed but does not need is below them.
    assert all(row == "" for row in rows[3:])


def test_a_prompt_with_nothing_in_flight_is_just_the_chevron():
    prompt = LivePrompt(_FakeTUI(None), console=Console())

    assert _text(prompt._message()) == "❯ "


@pytest.mark.asyncio
async def test_an_agent_question_takes_the_input_line_and_gets_its_answer():
    prompt = LivePrompt(_FakeTUI("AGENTS 1/2"), console=Console())

    asked = asyncio.create_task(prompt.ask("Allow rm -rf? [y/N] "))
    await asyncio.sleep(0)

    assert prompt.pending_question == "Allow rm -rf? [y/N] "
    assert _text(prompt._message()) == "AGENTS 1/2\nAllow rm -rf? [y/N] "
    assert prompt.deliver("y") is True
    assert await asyncio.wait_for(asked, timeout=1) == "y"
    # The line belongs to the user again.
    assert prompt.pending_question is None
    assert _text(prompt._message()) == "AGENTS 1/2\n❯ "


@pytest.mark.asyncio
async def test_overlapping_questions_are_answered_in_the_order_they_were_asked():
    """Two agents can be waiting at once; one input line serves them in turn.

    The old design gave each question its own prompt and let them overlap,
    which is what made ownership of the screen a stack. One line means one
    queue, and the user always sees whose question they are answering.
    """
    prompt = LivePrompt(_FakeTUI(None), console=Console())

    first = asyncio.create_task(prompt.ask("first? "))
    await asyncio.sleep(0)
    second = asyncio.create_task(prompt.ask("second? "))
    await asyncio.sleep(0)

    assert prompt.pending_question == "first? "
    assert prompt.deliver("one") is True
    assert prompt.pending_question == "second? "
    assert prompt.deliver("two") is True

    assert await asyncio.wait_for(first, timeout=1) == "one"
    assert await asyncio.wait_for(second, timeout=1) == "two"
    assert prompt.deliver("nobody is asking") is False


@pytest.mark.asyncio
async def test_a_cancelled_question_gives_the_line_back():
    prompt = LivePrompt(_FakeTUI(None), console=Console())

    asked = asyncio.create_task(prompt.ask("still there? "))
    await asyncio.sleep(0)
    asked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked

    assert prompt.pending_question is None
    assert prompt.deliver("free line") is False


@pytest.mark.asyncio
async def test_a_question_stashes_the_half_typed_message_and_puts_it_back():
    """The user may be mid-sentence when an agent asks something.

    Their draft cannot stay in the buffer: Enter would answer the agent's
    question with it, and a permission prompt reads anything that is not "y"
    as "no".
    """
    prompt = LivePrompt(_FakeTUI(None), console=Console())
    buffer = SimpleNamespace(text="please refactor the parser", cursor_position=26)
    prompt._session = SimpleNamespace(default_buffer=buffer, app=None)

    asked = asyncio.create_task(prompt.ask("Allow? [y/N] "))
    await asyncio.sleep(0)
    assert buffer.text == ""

    assert prompt.deliver("n") is True
    assert await asyncio.wait_for(asked, timeout=1) == "n"
    assert buffer.text == "please refactor the parser"
    assert buffer.cursor_position == len("please refactor the parser")


@pytest.mark.asyncio
async def test_declining_turns_every_waiting_question_away():
    """Ctrl+C has to answer the questions, not only stop the turn.

    A tool parked on an answer never reaches the step boundary where the
    turn's cooperative cancel is read, so interrupting without declining
    would leave the agent waiting on a line the user has already abandoned.
    """
    prompt = LivePrompt(_FakeTUI(None), console=Console())
    first = asyncio.create_task(prompt.ask("Allow? [y/N] "))
    second = asyncio.create_task(prompt.ask("Which one? "))
    await asyncio.sleep(0)

    assert prompt.decline_pending() is True

    for waiting in (first, second):
        with pytest.raises(EOFError):
            await asyncio.wait_for(waiting, timeout=1)
    assert prompt.pending_question is None
    assert prompt.decline_pending() is False


@pytest.mark.asyncio
async def test_a_declined_permission_reads_as_a_no():
    prompt = LivePrompt(_FakeTUI(None), console=Console())
    confirm = asyncio.create_task(TuiPermissionPolicy(read_line=prompt.ask).confirm("Allow?"))
    await asyncio.sleep(0)

    prompt.decline_pending()

    assert await asyncio.wait_for(confirm, timeout=1) is False


@pytest.mark.asyncio
async def test_a_run_with_no_screen_reads_straight_from_stdin(monkeypatch):
    prompt = LivePrompt(_FakeTUI(None), console=Console(file=StringIO()), interactive=False)
    read_fd, write_fd = os.pipe()
    input_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr("opencollab.adapters.cli.live_prompt.sys.stdin", input_stream)

    task = asyncio.create_task(prompt.read())
    await asyncio.sleep(0)
    os.write(write_fd, b"piped input\n")

    assert await asyncio.wait_for(task, timeout=1) == "piped input"
    input_stream.close()
    os.close(write_fd)


@pytest.mark.asyncio
async def test_console_input_fallback_removes_reader_when_cancelled(monkeypatch):
    read_fd, write_fd = os.pipe()
    input_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr("opencollab.adapters.cli.live_prompt.sys.stdin", input_stream)
    console = Console(file=StringIO())

    task = asyncio.create_task(read_console_line(console, "> "))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert asyncio.get_running_loop().remove_reader(read_fd) is False
    input_stream.close()
    os.close(write_fd)


@pytest.mark.asyncio
async def test_a_broken_prompt_falls_back_to_the_console_rather_than_failing(monkeypatch):
    class BrokenPromptSession:
        app = None

        async def prompt_async(self, *_args, **_kwargs):
            raise RuntimeError("prompt toolkit unavailable")

    prompt = LivePrompt(_FakeTUI(None), console=Console(file=StringIO()))
    prompt._session = BrokenPromptSession()
    read_fd, write_fd = os.pipe()
    input_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr("opencollab.adapters.cli.live_prompt.sys.stdin", input_stream)

    task = asyncio.create_task(prompt.read())
    await asyncio.sleep(0)
    os.write(write_fd, b"fallback input\n")

    assert await asyncio.wait_for(task, timeout=1) == "fallback input"
    input_stream.close()
    os.close(write_fd)


def test_attaching_registers_and_releases_the_redraw_callback():
    tui = _FakeTUI(None)
    prompt = LivePrompt(tui, console=Console(), interactive=False)

    with prompt.attached():
        pass

    # Nothing is painted without a screen, so nothing asks for a repaint.
    assert tui.redraws == []


def test_prompt_tab_bindings_switch_agents_without_touching_input_buffer():
    tui = _FakeTUI(None)
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
    tui = _FakeTUI(None)
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
            prompt = LivePrompt(tui, console=console)
            prompt._session = session
            command = asyncio.create_task(prompt.read())
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


@pytest.mark.asyncio
async def test_agent_policies_read_through_the_shared_input_line():
    """A permission y/N and an ask_user question are reads of the one line.

    Nothing is suspended around them any more: the prompt owns the bottom of
    the screen for the whole session, so the question is simply the next thing
    that line is asked to read.
    """
    prompt = LivePrompt(_FakeTUI(None), console=Console())

    confirm = asyncio.create_task(TuiPermissionPolicy(read_line=prompt.ask).confirm("Allow?"))
    await asyncio.sleep(0)
    assert prompt.pending_question == "Allow? [y/N] "
    prompt.deliver("y")
    assert await asyncio.wait_for(confirm, timeout=1) is True

    asked = asyncio.create_task(TuiAskUserPolicy(read_line=prompt.ask).ask("Which one?"))
    await asyncio.sleep(0)
    assert prompt.pending_question == "[Agent asks] Which one?\n> "
    prompt.deliver("the second")
    assert await asyncio.wait_for(asked, timeout=1) == "the second"

from __future__ import annotations

import asyncio
import fcntl
import os
import struct
import termios
from types import SimpleNamespace

import prompt_toolkit
import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML, fragment_list_to_text, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from opencollab.adapters.cli import main as cli_main
from opencollab.adapters.cli.prompt_view import (
    build_agent_navigation_bindings,
    build_agent_prompt,
)


class _FakeTUI:
    def __init__(self) -> None:
        self.selected_aid = 0
        self.history = {1: "agent one full history", 2: "agent two full history"}
        self.revisions = {0: 0, 1: 0, 2: 0}
        self.render_calls = 0

    @property
    def selected_history_cache_key(self) -> tuple[int, int, int]:
        return self.selected_aid, self.revisions[self.selected_aid], 80

    def select_next_agent(self) -> int | None:
        self.selected_aid = 1 if self.selected_aid == 0 else 2
        return self.selected_aid

    def select_previous_agent(self) -> int | None:
        self.selected_aid = 1
        return self.selected_aid

    def render_selected_history(self) -> str:
        self.render_calls += 1
        return self.history.get(self.selected_aid, "")


def _plain_prompt(prompt) -> str:
    return fragment_list_to_text(to_formatted_text(prompt))


def test_agent_prompt_replaces_history_and_refreshes_on_revision():
    tui = _FakeTUI()
    prompt = build_agent_prompt(tui, HTML("<b>&gt;</b> "))

    assert _plain_prompt(prompt) == "> "
    tui.selected_aid = 1
    assert _plain_prompt(prompt) == "agent one full history\n> "
    assert _plain_prompt(prompt) == "agent one full history\n> "
    assert tui.render_calls == 2

    tui.history[1] = "agent one latest history"
    tui.revisions[1] += 1
    assert _plain_prompt(prompt) == "agent one latest history\n> "
    assert tui.render_calls == 3

    tui.selected_aid = 2
    assert _plain_prompt(prompt) == "agent two full history\n> "
    assert tui.render_calls == 4


def test_agent_prompt_render_failure_falls_back_to_plain_input():
    tui = _FakeTUI()
    tui.selected_aid = 1

    def fail_render() -> str:
        raise RuntimeError("render failed")

    tui.render_selected_history = fail_render
    prompt = build_agent_prompt(tui, "> ")

    assert _plain_prompt(prompt) == "> "


def test_agent_prompt_history_cache_evicts_old_revisions():
    tui = _FakeTUI()
    tui.selected_aid = 1
    prompt = build_agent_prompt(tui, "> ")

    for revision in range(17):
        tui.revisions[1] = revision
        tui.history[1] = f"revision {revision}"
        assert f"revision {revision}" in _plain_prompt(prompt)

    assert tui.render_calls == 17
    tui.revisions[1] = 0
    tui.history[1] = "revision zero rerendered"
    assert "revision zero rerendered" in _plain_prompt(prompt)
    assert tui.render_calls == 18

    tui.revisions[1] = 16
    assert "revision 16" in _plain_prompt(prompt)
    assert tui.render_calls == 18


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
async def test_prompt_pty_redraw_clears_rows_from_longer_agent_history(monkeypatch):
    monkeypatch.setenv("PROMPT_TOOLKIT_NO_CPR", "1")
    tui = _FakeTUI()
    tui.selected_aid = 1
    tui.history[1] = "OLD-ONE\nOLD-TWO\nOLD-THREE"
    tui.history[2] = "NEW-TWO"
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 12, 80, 0, 0),
    )
    output_stream = os.fdopen(os.dup(slave_fd), "w", encoding="utf-8", buffering=1)
    output = Vt100_Output.from_pty(output_stream, term="xterm")
    raw = bytearray()
    old_rendered = asyncio.Event()
    new_rendered = asyncio.Event()
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
        if b"OLD-THREE" in raw:
            old_rendered.set()
        if b"NEW-TWO" in raw:
            new_rendered.set()

    loop.add_reader(master_fd, drain_output)
    try:
        with create_pipe_input() as pipe_input:
            session = PromptSession(
                input=pipe_input,
                output=output,
                erase_when_done=True,
            )
            prompt = asyncio.create_task(
                session.prompt_async(
                    build_agent_prompt(tui, "> "),
                    key_bindings=build_agent_navigation_bindings(tui),
                )
            )
            await asyncio.wait_for(old_rendered.wait(), timeout=2)
            pipe_input.send_bytes(b"\t")
            await asyncio.wait_for(new_rendered.wait(), timeout=2)
            pipe_input.send_bytes(b"\r")
            assert await asyncio.wait_for(prompt, timeout=2) == ""
            await asyncio.sleep(0)
    finally:
        loop.remove_reader(master_fd)
        drain_output()
        output_stream.close()
        os.close(master_fd)
        os.close(slave_fd)

    redraw = bytes(raw).split(b"OLD-THREE", 1)[1]
    before_new, after_new = redraw.split(b"NEW-TWO", 1)
    assert b"\x1b[3A" in before_new
    assert after_new.count(b"\x1b[K") >= 2
    assert b"\x1b[J" in after_new


@pytest.mark.asyncio
async def test_read_command_wires_dynamic_prompt_and_navigation(monkeypatch):
    tui = _FakeTUI()
    tui.suspend_live = lambda: False
    resumed: list[bool] = []
    tui.resume_live = resumed.append
    captured = {}

    async def fake_read_line(prompt_text, bottom_toolbar=None, key_bindings=None):
        captured["prompt"] = prompt_text
        captured["toolbar"] = bottom_toolbar
        captured["bindings"] = key_bindings
        return "do work"

    monkeypatch.setattr(cli_main, "_read_line", fake_read_line)

    def toolbar() -> str:
        return "AGENTS"

    assert await cli_main._read_command(tui, bottom_toolbar=toolbar) == "do work"
    assert captured["toolbar"] is toolbar
    assert callable(captured["prompt"])
    assert captured["bindings"].get_bindings_for_keys((Keys.ControlI,))
    assert resumed == [False]


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

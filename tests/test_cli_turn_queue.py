"""Turns run behind the prompt: queueing, interrupting, and reporting."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from opencollab.adapters.cli import main as cli_main
from opencollab.adapters.cli.turn_queue import TurnQueue
from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.domain.session import SessionPhase


class _FakePrompt:
    """A scripted input line. ``None`` raises EOF, ``KeyboardInterrupt`` is sent."""

    def __init__(self, lines, answers=()):
        self._lines = list(lines)
        self._answers = list(answers)
        self.delivered: list[str] = []

    async def read(self) -> str:
        if not self._lines:
            raise EOFError
        line = self._lines.pop(0)
        if isinstance(line, BaseException):
            raise line
        return line

    def deliver(self, line: str) -> bool:
        if not self._answers:
            return False
        self._answers.pop(0)
        self.delivered.append(line)
        return True


def _tui(selected_aid: int = 0):
    return SimpleNamespace(selected_aid=selected_aid, print_turn_divider=lambda: None)


@pytest.mark.asyncio
async def test_a_line_is_addressed_to_the_agent_that_holds_focus():
    submitted: list[tuple[str, int]] = []
    queue = SimpleNamespace(submit=lambda line, aid: submitted.append((line, aid)))

    await cli_main._read_loop(
        _FakePrompt(["message selected agent"]), queue, _tui(2), object()
    )

    assert submitted == [("message selected agent", 2)]


@pytest.mark.asyncio
async def test_an_agents_question_is_answered_before_the_line_becomes_a_turn():
    submitted: list = []
    queue = SimpleNamespace(submit=lambda line, aid: submitted.append((line, aid)))
    prompt = _FakePrompt(["y", "now a real turn"], answers=["question"])

    await cli_main._read_loop(prompt, queue, _tui(0), object())

    assert prompt.delivered == ["y"]
    assert submitted == [("now a real turn", 0)]


@pytest.mark.asyncio
async def test_exit_words_end_the_loop_and_q_only_counts_while_holding():
    submitted: list = []
    queue = SimpleNamespace(submit=lambda line, aid: submitted.append((line, aid)))

    await cli_main._read_loop(_FakePrompt(["q", "exit"]), queue, _tui(0), object())
    assert submitted == [("q", 0)]

    submitted.clear()
    await cli_main._read_loop(
        _FakePrompt(["q", "exit"]),
        queue,
        _tui(0),
        object(),
        exit_words=cli_main._HOLD_EXIT_COMMANDS,
    )
    assert submitted == []


@pytest.mark.asyncio
async def test_ctrl_c_interrupts_a_running_turn_and_keeps_the_prompt():
    """Ctrl+C is an interrupt while there is something to interrupt.

    Ending the session on it would throw away the team the user is still
    talking to; the way out is EOF, or an exit word.
    """
    interrupts = iter([True, False])
    submitted: list = []
    queue = SimpleNamespace(
        submit=lambda line, aid: submitted.append((line, aid)),
        interrupt=lambda: next(interrupts),
    )
    prompt = _FakePrompt([KeyboardInterrupt(), "still here", KeyboardInterrupt(), "never read"])
    prompt.decline_pending = lambda: False

    await cli_main._read_loop(prompt, queue, _tui(0), object())

    assert submitted == [("still here", 0)]


@pytest.mark.asyncio
async def test_ctrl_c_declines_a_waiting_question_even_with_no_turn_to_stop():
    declines = iter([True, False])
    queue = SimpleNamespace(submit=lambda line, aid: None, interrupt=lambda: False)
    prompt = _FakePrompt([KeyboardInterrupt(), KeyboardInterrupt(), "never read"])
    prompt.decline_pending = lambda: next(declines)

    await cli_main._read_loop(prompt, queue, _tui(0), object())


@pytest.mark.asyncio
async def test_typing_during_a_turn_queues_instead_of_interleaving():
    """Turns stay strictly ordered: an agent's transcript has one order."""
    started: list[str] = []
    release = asyncio.Event()
    depths: list[int] = []

    async def run_turn(line, aid, cancel):
        started.append(line)
        if line == "first":
            await release.wait()

    queue = TurnQueue(run_turn, on_depth=depths.append)
    worker = asyncio.create_task(queue.run())
    queue.submit("first", 0)
    await asyncio.sleep(0)
    queue.submit("second", 1)
    queue.submit("third", 1)

    assert started == ["first"]
    assert queue.depth == 2
    assert queue.busy is True

    release.set()
    await asyncio.wait_for(queue.drain(), timeout=1)
    worker.cancel()

    assert started == ["first", "second", "third"]
    assert depths[-1] == 0
    assert queue.busy is False


@pytest.mark.asyncio
async def test_interrupt_cancels_the_running_turn_and_drops_what_was_queued():
    """Cooperative, not a task cancel: the agent stops with its session settled."""
    cancels: list[asyncio.Event] = []
    ran: list[str] = []

    async def run_turn(line, aid, cancel):
        ran.append(line)
        cancels.append(cancel)
        await cancel.wait()

    queue = TurnQueue(run_turn)
    worker = asyncio.create_task(queue.run())
    queue.submit("first", 0)
    await asyncio.sleep(0)
    queue.submit("queued behind it", 0)

    assert queue.interrupt() is True
    assert cancels[0].is_set() is True
    await asyncio.wait_for(queue.drain(), timeout=1)
    worker.cancel()

    assert ran == ["first"]
    assert queue.depth == 0
    # Nothing running and nothing queued: there is nothing left to interrupt.
    assert queue.interrupt() is False


@pytest.mark.asyncio
async def test_a_failing_turn_stops_the_run_rather_than_dying_in_the_background():
    async def run_turn(line, aid, cancel):
        raise RuntimeError("turn blew up")

    queue = TurnQueue(run_turn)
    queue.submit("do work", 0)

    with pytest.raises(RuntimeError, match="turn blew up"):
        await asyncio.wait_for(
            cli_main._drive(queue, reader=None, until_drained=True), timeout=2
        )


@pytest.mark.asyncio
async def test_a_one_shot_run_ends_when_its_queue_drains():
    ran: list[str] = []

    async def run_turn(line, aid, cancel):
        ran.append(line)

    queue = TurnQueue(run_turn)
    queue.submit("do work", 0)

    await asyncio.wait_for(
        cli_main._drive(queue, reader=None, until_drained=True), timeout=2
    )

    assert ran == ["do work"]


@pytest.mark.asyncio
async def test_a_run_with_nothing_reading_must_be_allowed_to_end():
    queue = TurnQueue(lambda *args: asyncio.sleep(0))

    with pytest.raises(ValueError, match="must end when its queue drains"):
        await cli_main._drive(queue, reader=None, until_drained=False)


@pytest.mark.asyncio
async def test_leaving_the_prompt_ends_the_run_even_mid_turn():
    stopped = asyncio.Event()

    async def run_turn(line, aid, cancel):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    queue = TurnQueue(run_turn)
    queue.submit("long turn", 0)

    async def reader():
        await asyncio.sleep(0)

    await asyncio.wait_for(
        cli_main._drive(queue, reader=reader(), until_drained=False), timeout=2
    )

    assert stopped.is_set() is True


def test_a_stopped_turn_reports_its_reason_and_salvages_the_partial_answer():
    output = StringIO()
    tui = SimpleNamespace(drained_partial_answer=lambda aid: False)
    console = Console(file=output, force_terminal=False, color_system=None, width=100)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_main, "console", console)
        cli_main._report_turn_failure(
            tui,
            SchedulerTurnError(
                2,
                SessionPhase.STOPPED,
                "token budget exhausted",
                "Here is malformed rich: [bold]x[/red]",
            ),
        )

    printed = output.getvalue()
    assert "Here is malformed rich: [bold]x[/red]" in printed
    assert "Agent 2 stopped: token budget exhausted" in printed


def test_a_partial_answer_already_in_scrollback_is_not_printed_again():
    """The partial answer is salvage, and the turn's cleanup may have got there first.

    ``settle_turn`` commits the trailing streamed text to scrollback, so
    printing ``partial_answer`` unconditionally shows the user the same text
    twice.
    """
    output = StringIO()
    tui = SimpleNamespace(drained_partial_answer=lambda aid: aid == 2)
    console = Console(file=output, force_terminal=False, color_system=None, width=100)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_main, "console", console)
        cli_main._report_turn_failure(
            tui,
            SchedulerTurnError(2, SessionPhase.STOPPED, "token budget exhausted", "partial answer"),
        )

    printed = output.getvalue()
    assert "partial answer" not in printed
    assert "Agent 2 stopped: token budget exhausted" in printed

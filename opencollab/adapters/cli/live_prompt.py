"""The always-on input line, and the bottom region of the terminal it owns.

prompt_toolkit paints two things, in this order: the HUD the TUI renders to
ANSI rows (streaming text, then the one status row), and the input line under
it. It stays up for the whole session — while a turn runs, between turns, and
while an agent is waiting on an answer — because two in-place redrawers cannot
share rows, which is what a Rich ``Live`` and a prompt used to try to do.

Everything printed while it is up goes above it: a standing ``patch_stdout``
routes every write — this package's and anyone else's — through
``run_in_terminal``, so scrollback stays the transcript and the prompt keeps
its rows.

One input line means one queue of readers. A permission y/N and an ``ask_user``
question arrive from agents running concurrently, so they take the line in
turn, ahead of whatever the user was typing.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from opencollab.adapters.cli.prompt_view import build_agent_navigation_bindings
from opencollab.adapters.tui.theme import BRAND_VIOLET

# Fast enough that a breathing dot and a seconds counter read as live, slow
# enough that an idle prompt is not re-rendering for nothing.
REFRESH_INTERVAL = 0.1

_PROMPT_STYLE = Style.from_dict({
    "prompt.chevron": f"{BRAND_VIOLET} bold",
})
_CHEVRON = [("class:prompt.chevron", "❯"), ("", " ")]
_FALLBACK_PROMPT = "> "


@dataclass
class _Question:
    """One agent waiting for the user to answer on the shared input line."""

    text: str
    future: asyncio.Future


async def read_console_line(console: Any, prompt_text: str) -> str:
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


class LivePrompt:
    """The one input line, and the HUD painted above it.

    ``interactive`` is False for a redirected or piped run: there is no screen
    to own, so nothing is patched, nothing is painted, and lines are read
    straight from stdin.
    """

    def __init__(self, tui: Any, *, console: Any, interactive: bool = True):
        self._tui = tui
        self._console = console
        self._interactive = interactive
        self._session: Any | None = None
        self._questions: deque[_Question] = deque()
        self._stashed_input = ""

    @property
    def interactive(self) -> bool:
        return self._interactive

    @contextlib.contextmanager
    def attached(self):
        """Own the bottom region for the duration of the block.

        ``patch_stdout`` is the whole scrollback contract: it is stood up once,
        around everything, rather than claimed and released per prompt. The
        redraw callback is the other direction — an event that changes the HUD
        has to ask this prompt to repaint it.
        """
        if not self._interactive:
            yield self
            return
        with patch_stdout(raw=True):
            self._tui.set_redraw(self.invalidate)
            try:
                yield self
            finally:
                self._tui.set_redraw(None)

    def invalidate(self) -> None:
        """Repaint the bottom region, if it is on screen."""
        session = self._session
        app = getattr(session, "app", None)
        if app is not None and getattr(app, "is_running", False):
            app.invalidate()

    # -- reading ------------------------------------------------------------

    async def read(self) -> str:
        """Read one line: a user turn, or an answer an agent is waiting for."""
        if not self._interactive:
            return await read_console_line(self._console, _FALLBACK_PROMPT)
        session = self._ensure_session()
        try:
            return await session.prompt_async(
                self._message,
                refresh_interval=REFRESH_INTERVAL,
                key_bindings=build_agent_navigation_bindings(self._tui),
                style=_PROMPT_STYLE,
            )
        except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception:
            return await read_console_line(self._console, _FALLBACK_PROMPT)

    async def ask(self, text: str) -> str:
        """Ask the user something on an agent's behalf and wait for the answer.

        The question takes the input line ahead of whatever the user was
        typing; their half-written message is put back when the last pending
        question has been answered.
        """
        if not self._interactive:
            return await read_console_line(self._console, text)
        question = _Question(text, asyncio.get_running_loop().create_future())
        self._questions.append(question)
        if len(self._questions) == 1:
            self._stash_input()
        self.invalidate()
        try:
            return await question.future
        finally:
            self._retire(question)

    def deliver(self, line: str) -> bool:
        """Hand ``line`` to the agent that asked first, if one is waiting."""
        while self._questions:
            question = self._questions.popleft()
            if question.future.done():
                continue
            question.future.set_result(line)
            self._settle_questions()
            return True
        return False

    def decline_pending(self) -> bool:
        """Turn away every waiting question, as an interrupt should.

        A question blocks the agent that asked it, and the turn's cooperative
        cancel cannot reach a tool that is parked on an answer — so Ctrl+C has
        to answer them, not only stop the turn. ``EOFError`` is the decline the
        policies already understand: a permission goes to no, an ``ask_user``
        reports that the user declined.
        """
        pending = list(self._questions)
        self._questions.clear()
        for question in pending:
            if not question.future.done():
                question.future.set_exception(EOFError())
        if pending:
            self._settle_questions()
        return bool(pending)

    @property
    def pending_question(self) -> str | None:
        """The question currently holding the input line, if any."""
        return self._questions[0].text if self._questions else None

    # -- internals ----------------------------------------------------------

    def _ensure_session(self) -> Any:
        if self._session is None:
            from prompt_toolkit import PromptSession

            self._session = PromptSession(erase_when_done=True)
        return self._session

    def _message(self) -> Any:
        """The rows above the cursor: the HUD, then a question or the chevron."""
        rows: list[Any] = []
        hud = self._tui.hud_ansi()
        if hud:
            rows.extend(to_formatted_text(ANSI(hud)))
            rows.append(("", "\n"))
        question = self.pending_question
        if question is not None:
            rows.extend(to_formatted_text(question))
            return rows
        rows.extend(_CHEVRON)
        return rows

    def _buffer(self) -> Any | None:
        return getattr(self._session, "default_buffer", None)

    def _stash_input(self) -> None:
        buffer = self._buffer()
        if buffer is None:
            return
        self._stashed_input = buffer.text
        buffer.text = ""

    def _settle_questions(self) -> None:
        """Give the input line back to the user once no agent is waiting."""
        if self._questions:
            self.invalidate()
            return
        buffer = self._buffer()
        stashed, self._stashed_input = self._stashed_input, ""
        if buffer is not None and stashed:
            buffer.text = stashed
            buffer.cursor_position = len(stashed)
        self.invalidate()

    def _retire(self, question: _Question) -> None:
        """Drop a question that was cancelled rather than answered."""
        try:
            self._questions.remove(question)
        except ValueError:
            return
        self._settle_questions()


__all__ = ["LivePrompt", "REFRESH_INTERVAL", "read_console_line"]

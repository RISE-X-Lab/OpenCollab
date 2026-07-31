"""Turn-scoped keyboard navigation for the Rich TUI.

The project targets Linux and macOS. ``cbreak`` makes escape sequences available
without waiting for Enter while deliberately preserving the terminal's ``ISIG``
flag, so Ctrl+C keeps its existing process-interrupt behavior.
"""

from __future__ import annotations

import asyncio
import os
import sys
import termios
import tty
from collections.abc import Callable
from typing import TextIO

_PREVIOUS_SEQUENCES = (b"\x1b[Z",)
_NEXT_SEQUENCES = (b"\t",)


class TabKeyNavigator:
    """Read Shift+Tab and Tab while a Live turn is running."""

    def __init__(
        self,
        on_previous: Callable[[], object],
        on_next: Callable[[], object],
        *,
        stream: TextIO | None = None,
    ) -> None:
        self._on_previous = on_previous
        self._on_next = on_next
        self._on_quit: Callable[[], object] | None = None
        self._stream = stream or sys.stdin
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None
        self._original_attrs: list | None = None
        self._buffer = b""

    @property
    def active(self) -> bool:
        return self._fd is not None

    def set_quit_callback(self, callback: Callable[[], object] | None) -> None:
        """Enable ``q`` only for an explicit post-run inspection phase."""
        self._on_quit = callback

    def start(self) -> bool:
        """Start listening, or quietly decline when stdin is not an interactive TTY."""
        if self.active:
            return False
        try:
            fd = self._stream.fileno()
            if not os.isatty(fd):
                return False
            loop = asyncio.get_running_loop()
            original_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd, termios.TCSANOW)
            loop.add_reader(fd, self._read_ready)
        except (AttributeError, OSError, RuntimeError, termios.error):
            if "fd" in locals() and "original_attrs" in locals():
                self._restore_terminal(fd, original_attrs)
            return False

        self._loop = loop
        self._fd = fd
        self._original_attrs = original_attrs
        self._buffer = b""
        return True

    def stop(self) -> bool:
        """Stop listening and restore the exact terminal attributes."""
        if not self.active:
            return False
        fd = self._fd
        loop = self._loop
        original_attrs = self._original_attrs
        self._fd = None
        self._loop = None
        self._original_attrs = None
        self._buffer = b""
        if loop is not None and fd is not None:
            try:
                loop.remove_reader(fd)
            except (OSError, RuntimeError):
                pass
        if fd is not None and original_attrs is not None:
            self._restore_terminal(fd, original_attrs)
        return True

    def _read_ready(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            data = os.read(fd, 64)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.stop()
            return
        if not data:
            self.stop()
            return
        self._buffer += data
        self._drain_buffer()

    def _drain_buffer(self) -> None:
        sequences = list(
            (sequence, callback)
            for group, callback in (
                (_PREVIOUS_SEQUENCES, self._on_previous),
                (_NEXT_SEQUENCES, self._on_next),
            )
            for sequence in group
        )
        if self._on_quit is not None:
            sequences.append((b"q", self._on_quit))
        while self._buffer:
            matches = [
                (index, sequence, callback)
                for sequence, callback in sequences
                if (index := self._buffer.find(sequence)) >= 0
            ]
            if matches:
                index, sequence, callback = min(matches, key=lambda item: item[0])
                self._buffer = self._buffer[index + len(sequence):]
                callback()
                continue

            keep = max(
                (
                    length
                    for sequence, _callback in sequences
                    for length in range(1, min(len(sequence), len(self._buffer)) + 1)
                    if self._buffer.endswith(sequence[:length])
                ),
                default=0,
            )
            self._buffer = self._buffer[-keep:] if keep else b""
            return

    @staticmethod
    def _restore_terminal(fd: int, attrs: list) -> None:
        try:
            # On Darwin, restoring canonical mode with TCSANOW leaves the
            # kernel-managed PENDIN bit set. This listener owns and discards
            # turn-scoped input, so flushing it also restores the exact state.
            termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)
        except (OSError, termios.error):
            pass


__all__ = ["TabKeyNavigator"]

from __future__ import annotations

import asyncio
import os
import termios

import pytest

from opencollab.adapters.tui.keyboard import TabKeyNavigator


async def _wait_for_count(events: list[str], count: int) -> None:
    for _ in range(50):
        if len(events) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"received {len(events)} key events, expected {count}")


@pytest.mark.asyncio
async def test_tab_navigation_reads_tab_and_backtab_and_restores_tty():
    master_fd, slave_fd = os.openpty()
    stream = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8")
    original_attrs = termios.tcgetattr(stream.fileno())
    events: list[str] = []
    navigator = TabKeyNavigator(
        lambda: events.append("previous"),
        lambda: events.append("next"),
        stream=stream,
    )
    try:
        assert navigator.start() is True
        active_attrs = termios.tcgetattr(stream.fileno())
        assert active_attrs[3] & termios.ISIG
        assert not active_attrs[3] & termios.ICANON
        assert not active_attrs[3] & termios.ECHO

        os.write(master_fd, b"ignored\x1b[A\x1b[B\t\x1b[Z\t\x1b[Z")
        await _wait_for_count(events, 4)

        assert events == ["next", "previous", "next", "previous"]
        assert navigator.stop() is True
        assert termios.tcgetattr(stream.fileno()) == original_attrs
    finally:
        navigator.stop()
        stream.close()
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.asyncio
async def test_tab_navigation_keeps_fragmented_backtab_sequence():
    master_fd, slave_fd = os.openpty()
    stream = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8")
    events: list[str] = []
    navigator = TabKeyNavigator(
        lambda: events.append("previous"),
        lambda: events.append("next"),
        stream=stream,
    )
    try:
        assert navigator.start() is True
        for fragment in (b"\x1b", b"[", b"Z"):
            os.write(master_fd, fragment)
            await asyncio.sleep(0.01)
        await _wait_for_count(events, 1)
        assert events == ["previous"]
    finally:
        navigator.stop()
        stream.close()
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.asyncio
async def test_quit_key_is_active_only_with_an_explicit_callback():
    master_fd, slave_fd = os.openpty()
    stream = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8")
    events: list[str] = []
    navigator = TabKeyNavigator(
        lambda: events.append("previous"),
        lambda: events.append("next"),
        stream=stream,
    )
    try:
        assert navigator.start() is True
        os.write(master_fd, b"q")
        await asyncio.sleep(0.02)
        assert events == []

        navigator.set_quit_callback(lambda: events.append("quit"))
        os.write(master_fd, b"q")
        await _wait_for_count(events, 1)
        assert events == ["quit"]

        navigator.set_quit_callback(None)
        os.write(master_fd, b"q")
        await asyncio.sleep(0.02)
        assert events == ["quit"]
    finally:
        navigator.stop()
        stream.close()
        os.close(master_fd)
        os.close(slave_fd)


def test_tab_navigation_quietly_disables_for_non_tty(tmp_path):
    path = tmp_path / "stdin.txt"
    path.write_text("", encoding="utf-8")
    with path.open(encoding="utf-8") as stream:
        navigator = TabKeyNavigator(lambda: None, lambda: None, stream=stream)
        assert navigator.start() is False
        assert navigator.active is False
        assert navigator.stop() is False

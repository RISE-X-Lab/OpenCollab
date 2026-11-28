"""Timed ownership of provider transport operations."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")
_TransportInterruption = asyncio.CancelledError | asyncio.TimeoutError

async def _wait_for_transport(
    operation: Awaitable[_T],
    timeout: float | None,
    *,
    on_interrupt: Callable[[_TransportInterruption], None] | None = None,
    repeat_cancellation: bool = True,
) -> _T:
    if sys.version_info >= (3, 12) and on_interrupt is None and repeat_cancellation:
        return await asyncio.wait_for(operation, timeout)
    # Retain terminal usage when a cancelled transport still delivers a result.
    task = asyncio.ensure_future(operation)
    try:
        # Older wait_for implementations can consume a concurrent caller cancellation.
        if not task.done() and (timeout is None or timeout > 0):
            await asyncio.wait((task,), timeout=timeout)
        if not task.done():
            raise asyncio.TimeoutError
    except (asyncio.CancelledError, asyncio.TimeoutError) as interruption:
        if task.done():
            if on_interrupt is not None and isinstance(interruption, asyncio.CancelledError) and not task.cancelled():
                on_interrupt(interruption)
            return task.result()
        if on_interrupt is not None:
            on_interrupt(interruption)
        task.cancel()
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as cancellation:
                if isinstance(interruption, asyncio.TimeoutError):
                    interruption = cancellation
                    if on_interrupt is not None:
                        on_interrupt(interruption)
                if repeat_cancellation and not task.done():
                    task.cancel()
        if task.cancelled():
            raise interruption
    return task.result()

from __future__ import annotations

import asyncio
import sys

CANCEL_METADATA_SURVIVES_TASK_BOUNDARY = sys.version_info >= (3, 11)


def assert_cancel_reason(
    error: asyncio.CancelledError,
    expected: str,
) -> None:
    """Check the cancellation message supported by the active interpreter."""
    if CANCEL_METADATA_SURVIVES_TASK_BOUNDARY:
        assert error.args == (expected,)
    else:
        # CPython 3.10 synthesizes a fresh empty CancelledError when a cancelled
        # Task is awaited, even when Task.cancel(message) supplied a reason.
        assert error.args == ()


def assert_cancel_note(
    error: asyncio.CancelledError,
    *required_fragments: str,
) -> None:
    """Check PEP 678 diagnostics where Task cancellation preserves them."""
    notes = tuple(getattr(error, "__notes__", ()))
    if CANCEL_METADATA_SURVIVES_TASK_BOUNDARY:
        assert any(
            all(fragment in note for fragment in required_fragments)
            for note in notes
        )
    else:
        assert notes == ()

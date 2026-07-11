"""Bound arbitrary writer callbacks through a pipe into an owned candidate."""

from __future__ import annotations

import os
import select
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

from opencollab.application.exception_notes import add_exception_note

_PIPE_DRAIN_GRACE_SECONDS = 1.0


def write_bounded_candidate(
    candidate_fd: int,
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    context: str,
) -> int:
    """Run ``writer`` while keeping the candidate at or below ``max_bytes``."""
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(read_fd, False)
    except BaseException as error:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except BaseException as close_error:
                add_exception_note(
                    error,
                    f"pipe setup fd close failed with {type(close_error).__name__}: "
                    f"{close_error}",
                )
        raise
    writer_done = threading.Event()
    reader_errors: list[BaseException] = []
    bytes_written = 0

    def drain() -> None:
        nonlocal bytes_written
        deadline: float | None = None
        sink_failed = False
        try:
            while True:
                readable, _writable, _exceptional = select.select(
                    [read_fd],
                    [],
                    [],
                    0.05,
                )
                if readable:
                    chunk = os.read(read_fd, 65_536)
                    if not chunk:
                        return
                    remaining = max(max_bytes - bytes_written, 0)
                    accepted = chunk[:remaining] if not sink_failed else b""
                    try:
                        view = memoryview(accepted)
                        while view:
                            written = os.write(candidate_fd, view)
                            if written <= 0:
                                raise OSError(
                                    f"{context} candidate write made no progress"
                                )
                            bytes_written += written
                            view = view[written:]
                    except BaseException as error:
                        reader_errors.append(error)
                        sink_failed = True
                    if len(accepted) != len(chunk) and not reader_errors:
                        reader_errors.append(
                            OSError(
                                f"{context} exceeds {max_bytes}-byte retirement budget"
                            )
                        )
                if writer_done.is_set():
                    if deadline is None:
                        deadline = time.monotonic() + _PIPE_DRAIN_GRACE_SECONDS
                    elif time.monotonic() >= deadline:
                        raise OSError(f"{context} writer retained a pipe descriptor")
        except BaseException as error:
            reader_errors.append(error)

    reader = threading.Thread(
        target=drain,
        name="opencollab-bounded-candidate-writer",
        daemon=True,
    )
    try:
        reader.start()
    except BaseException as error:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except BaseException as close_error:
                add_exception_note(
                    error,
                    f"pipe thread-start fd close failed with "
                    f"{type(close_error).__name__}: {close_error}",
                )
        raise
    try:
        handle = os.fdopen(write_fd, "wb")
    except BaseException as error:
        try:
            os.close(write_fd)
        except BaseException as close_error:
            add_exception_note(error, f"pipe write fd close failed: {close_error}")
        writer_done.set()
        reader.join(_PIPE_DRAIN_GRACE_SECONDS + 1.0)
        try:
            os.close(read_fd)
        except BaseException as close_error:
            add_exception_note(error, f"pipe read fd close failed: {close_error}")
        raise
    writer_error: BaseException | None = None
    try:
        writer(handle)
        handle.flush()
    except BaseException as error:
        writer_error = error
    try:
        handle.close()
    except BaseException as close_error:
        if writer_error is None:
            writer_error = close_error
        else:
            add_exception_note(
                writer_error,
                f"{context} pipe close failed with "
                f"{type(close_error).__name__}: {close_error}",
            )
    writer_done.set()
    reader.join(_PIPE_DRAIN_GRACE_SECONDS + 1.0)
    if reader.is_alive():
        reader_errors.append(OSError(f"{context} pipe drain thread did not exit"))
    try:
        os.close(read_fd)
    except BaseException as close_error:
        reader_errors.append(close_error)
    if reader.is_alive():
        reader.join(0.2)
    try:
        os.ftruncate(candidate_fd, bytes_written)
    except BaseException as truncate_error:
        reader_errors.append(truncate_error)

    if writer_error is not None:
        for error in reader_errors:
            add_exception_note(
                writer_error,
                f"{context} bounded drain failed with "
                f"{type(error).__name__}: {error}",
            )
        raise writer_error
    if reader_errors:
        primary = reader_errors[0]
        for error in reader_errors[1:]:
            add_exception_note(
                primary,
                f"additional bounded drain failure: {type(error).__name__}: {error}",
            )
        raise primary
    return bytes_written


__all__ = ["write_bounded_candidate"]

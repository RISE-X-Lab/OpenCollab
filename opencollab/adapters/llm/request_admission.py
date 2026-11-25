"""Request slots shared by independent processes on one local filesystem."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from filelock import FileLock, Timeout

from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.application.run_control import _raise_if_request_admission_cancelled


@asynccontextmanager
async def _locked(lock: FileLock) -> AsyncIterator[None]:
    while True:
        _raise_if_request_admission_cancelled()
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


@asynccontextmanager
async def request_slot(pool: str | None, capacity: int | None) -> AsyncIterator[None]:
    if pool is None:
        yield
        return
    directory = Path(pool)
    if not directory.is_dir():
        raise ValueError(
            f"request_pool must be an existing shared local directory: {pool}. "
            "Mount the same directory into every participating process or container."
        )
    ensure_directory_no_symlinks(directory)
    async with _locked(FileLock(str(directory / "capacity.lock"))):
        record = directory / "capacity"
        try:
            configured_capacity = int(read_regular_text(record, max_bytes=64))
        except FileNotFoundError:
            create_regular_bytes_atomic(record, str(capacity).encode())
        else:
            if configured_capacity != capacity:
                raise ValueError(f"request pool {pool!r} already has capacity {configured_capacity}, got {capacity}")
    while True:
        _raise_if_request_admission_cancelled()
        for index in range(capacity):
            lock = FileLock(str(directory / f"slot-{index}.lock"))
            try:
                lock.acquire(timeout=0)
            except Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        await asyncio.sleep(0.05)

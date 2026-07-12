"""Cross-platform descriptor-relative atomic rename operations."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from collections.abc import Callable

from opencollab.adapters._posix_file_support import (
    UnsupportedSafeFilePlatformError,
    require_posix_file_support,
)

_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004


def _load_native_rename() -> tuple[Callable[..., int] | None, int, int]:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        noreplace_flag = _RENAME_NOREPLACE
        exchange_flag = _RENAME_EXCHANGE
    elif sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        noreplace_flag = _RENAME_EXCL
        exchange_flag = _RENAME_SWAP
    else:
        function = None
        noreplace_flag = 0
        exchange_flag = 0
    if function is None:
        return None, noreplace_flag, exchange_flag
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function, noreplace_flag, exchange_flag


_native_rename, _native_flag, _native_exchange_flag = _load_native_rename()
_native_rename_noreplace = _native_rename
_native_rename_exchange = _native_rename


def _entry_name(value: str | bytes, *, label: str) -> bytes:
    encoded = os.fsencode(value)
    separators = [os.fsencode(os.sep)]
    if os.altsep is not None:
        separators.append(os.fsencode(os.altsep))
    if not encoded or b"\0" in encoded or encoded in {b".", b".."} or any(
        separator in encoded for separator in separators
    ):
        raise ValueError(f"{label} must be one directory entry")
    return encoded


def rename_noreplace(
    source: str | bytes,
    destination: str | bytes,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically move one entry while preserving an existing destination."""
    require_posix_file_support()
    function = _native_rename_noreplace
    if function is None:
        raise UnsupportedSafeFilePlatformError(
            "descriptor-safe local file operations require atomic rename-noreplace"
        )
    source_bytes = _entry_name(source, label="rename source")
    destination_bytes = _entry_name(destination, label="rename destination")
    ctypes.set_errno(0)
    result = function(
        src_dir_fd,
        source_bytes,
        dst_dir_fd,
        destination_bytes,
        _native_flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            os.fsdecode(destination_bytes),
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        os.fsdecode(destination_bytes),
    )


def rename_exchange(
    first: str | bytes,
    second: str | bytes,
    *,
    first_dir_fd: int,
    second_dir_fd: int,
) -> None:
    """Atomically exchange two existing directory entries."""
    require_posix_file_support()
    function = _native_rename_exchange
    if function is None:
        raise UnsupportedSafeFilePlatformError(
            "descriptor-safe local file operations require atomic rename-exchange"
        )
    first_bytes = _entry_name(first, label="first exchange entry")
    second_bytes = _entry_name(second, label="second exchange entry")
    ctypes.set_errno(0)
    result = function(
        first_dir_fd,
        first_bytes,
        second_dir_fd,
        second_bytes,
        _native_exchange_flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno() or errno.EIO
    raise OSError(
        error_number,
        os.strerror(error_number),
        os.fsdecode(second_bytes),
    )


__all__ = ["rename_exchange", "rename_noreplace"]

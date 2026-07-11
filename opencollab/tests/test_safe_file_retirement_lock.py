"""Retirement-lock regressions for reusable safe-file helpers."""

import os
from contextlib import contextmanager

import opencollab.adapters._owned_file_cleanup as owned_cleanup_mod
import pytest


def test_unverified_retirement_reads_entry_size_after_lock_acquisition(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "target"
    target.write_bytes(b"x")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"oversized")

    @contextmanager
    def swap_before_lock(_parent_fd):
        os.replace(replacement, target)
        yield

    monkeypatch.setattr(owned_cleanup_mod, "retirement_lock", swap_before_lock)
    monkeypatch.setattr(owned_cleanup_mod, "MAX_RETIRED_BYTES_PER_DIRECTORY", 4)
    parent_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="retired-file byte limit"):
            owned_cleanup_mod.retire_unverified_file(
                parent_fd,
                target.name,
                path_label=str(target),
            )
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"oversized"
    assert not list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))


def test_retirement_lock_preserves_primary_error_when_unlock_fails(monkeypatch):
    class FailingUnlock:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(_fd, operation):
            if operation == FailingUnlock.LOCK_UN:
                raise OSError("unlock failed")

    monkeypatch.setattr(
        owned_cleanup_mod,
        "require_posix_file_support",
        lambda: FailingUnlock,
    )

    with pytest.raises(ValueError, match="primary failed") as raised:
        with owned_cleanup_mod.retirement_lock(7):
            raise ValueError("primary failed")

    assert any(
        "retirement lock release failed with OSError: unlock failed" in note
        for note in raised.value.__notes__
    )

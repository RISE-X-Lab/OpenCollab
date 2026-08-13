"""Cross-process regressions for autosave journal compaction."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from opencollab.adapters.storage import SessionStore

_META = {"snapshot_version": 1, "session_state": {}}
_BASE_MESSAGES = [
    {"role": "system", "content": "system"},
    {"role": "assistant", "content": "base"},
]


class _PausedCheckpointStore(SessionStore):
    def __init__(self, published: object, release: object) -> None:
        self._published = published
        self._release = release

    def _atomic_json_write(self, path: str, value: object) -> None:
        super()._atomic_json_write(path, value)
        self._published.wait(timeout=10)
        if not self._release.wait(timeout=10):
            raise TimeoutError("test checkpoint was not released")


def _checkpoint_worker(path: str, published: object, release: object) -> None:
    _PausedCheckpointStore(published, release).checkpoint_snapshot(
        path,
        _BASE_MESSAGES,
        meta=_META,
        sequence=2,
    )


def _append_worker(path: str, attempted: object, completed: object) -> None:
    attempted.wait(timeout=10)
    SessionStore().append_snapshot_delta(
        path,
        sequence=3,
        replace_from=2,
        messages=[{"role": "assistant", "content": "new"}],
        meta=_META,
    )
    completed.set()


def _stop_process(process: multiprocessing.Process | None) -> None:
    if process is None:
        return
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)


def test_checkpoint_and_append_are_serialized_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "race.json"
    store = SessionStore()
    store.append_snapshot_delta(
        str(path),
        sequence=2,
        replace_from=0,
        messages=_BASE_MESSAGES,
        meta=_META,
    )
    context = multiprocessing.get_context("spawn")
    published = context.Barrier(2)
    append_attempted = context.Barrier(2)
    release_checkpoint = context.Event()
    append_completed = context.Event()
    checkpoint = context.Process(
        target=_checkpoint_worker,
        args=(str(path), published, release_checkpoint),
    )
    append: multiprocessing.Process | None = None
    append_completed_before_release = False
    try:
        checkpoint.start()
        published.wait(timeout=10)
        append = context.Process(
            target=_append_worker,
            args=(str(path), append_attempted, append_completed),
        )
        append.start()
        append_attempted.wait(timeout=10)
        append_completed_before_release = append_completed.wait(timeout=1)
    finally:
        release_checkpoint.set()
        _stop_process(checkpoint)
        _stop_process(append)

    assert checkpoint.exitcode == 0
    assert append is not None and append.exitcode == 0
    assert not append_completed_before_release
    restored = store.load_snapshot(str(path), "system")
    assert restored["_autosave_sequence"] == 3
    assert restored["messages"][-1]["content"] == "new"

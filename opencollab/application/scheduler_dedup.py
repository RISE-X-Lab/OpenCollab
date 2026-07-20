"""Single-flight spawn dedup for the Scheduler.

A (role, task) is reserved synchronously at spawn and freed when the child
reaches a terminal phase, so a model that re-issues an identical spawn is
refused (see ``inflight_spawn``) rather than spinning up a duplicate —
tool-level enforcement of "don't spawn the same task twice", which prompt
guidance alone cannot guarantee.

``InflightDedupMixin`` is composed into ``Scheduler`` and relies on the
``_inflight`` / ``_inflight_key_of`` maps created in ``Scheduler.__init__``.
"""

from __future__ import annotations

import hashlib


class InflightDedupMixin:
    """Reserve/lookup/release for in-flight (role, task) spawns."""

    @staticmethod
    def _task_key(role: str, task: str) -> str:
        """Stable dedup key for a (role, task). Whitespace is collapsed so a
        reflowed re-prompt of the same instruction maps to the same key.
        """
        normalized = " ".join(task.split())
        return hashlib.md5(f"{role}\x00{normalized}".encode()).hexdigest()

    def inflight_spawn(self, role: str, task: str) -> int | None:
        """The aid already handling this (role, task) if a spawn is in flight,
        else ``None``. The spawn tool consults this to refuse duplicates.
        """
        return self._inflight.get(self._task_key(role, task))

    def _reserve_inflight(self, aid: int, role: str, task: str) -> None:
        key = self._task_key(role, task)
        self._inflight[key] = aid
        self._inflight_key_of[aid] = key

    def _clear_inflight(self, aid: int) -> None:
        """Release a child's reservation once it is terminal (idempotent)."""
        key = self._inflight_key_of.pop(aid, None)
        if key is not None and self._inflight.get(key) == aid:
            del self._inflight[key]

"""Single-flight spawn dedup for the Scheduler.

A (parent, role, task, context) identity is reserved synchronously at spawn and
freed when the child reaches a terminal phase, so a model that re-issues the
same delegated work is refused (see ``inflight_spawn``) rather than spinning up
a duplicate. Different parents and contexts remain independent.

``InflightDedupMixin`` is composed into ``Scheduler`` and relies on the
``_inflight`` / ``_inflight_key_of`` maps created in ``Scheduler.__init__``.
"""

from __future__ import annotations

import hashlib


class InflightDedupMixin:
    """Reserve/lookup/release for in-flight delegated-work identities."""

    @staticmethod
    def _task_key(parent_aid: int, role: str, task: str, context: str) -> str:
        """Stable identity for one parent's exact delegated work.

        Newline encodings are normalized for cross-platform stability, but all
        other whitespace remains significant: indentation can change a code
        block or shell command's meaning.
        """
        normalized_task = task.replace("\r\n", "\n").replace("\r", "\n")
        normalized_context = context.replace("\r\n", "\n").replace("\r", "\n")
        digest = hashlib.sha256()
        for value in (str(parent_aid), role, normalized_task, normalized_context):
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def inflight_spawn(
        self,
        role: str,
        task: str,
        *,
        parent_aid: int = 0,
        context: str = "",
    ) -> int | None:
        """The aid already handling this parent's exact delegation, if any."""
        return self._inflight.get(self._task_key(parent_aid, role, task, context))

    def _reserve_inflight(
        self,
        aid: int,
        parent_aid: int,
        role: str,
        task: str,
        context: str,
    ) -> None:
        key = self._task_key(parent_aid, role, task, context)
        self._inflight[key] = aid
        self._inflight_key_of[aid] = key

    def _clear_inflight(self, aid: int) -> None:
        """Release a child's reservation once it is terminal (idempotent)."""
        key = self._inflight_key_of.pop(aid, None)
        if key is not None and self._inflight.get(key) == aid:
            del self._inflight[key]

"""AutoSaveSubscriber — persists session messages on lifecycle events.

Listens for the three events that change persisted state:
- `user_message_appended` — user added a turn
- `compaction_applied`   — compactor rewrote message history
- `step_end`             — assistant finished a step

Save failures are caught and swallowed (the EventBus already isolates
subscribers, but we belt-and-brace here so a disk-full does not log noise
on every step).
"""

from __future__ import annotations

import logging
from typing import Callable

from opencollab.application.event_bus import EventSink
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent


SAVE_TRIGGERS = frozenset({
    "user_message_appended",
    "compaction_applied",
    "step_end",
})


class AutoSaveSubscriber(EventSink):
    def __init__(self, save_fn: Callable[[], None]):
        self._save = save_fn

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS:
            return
        try:
            self._save()
        except Exception as exc:
            logging.getLogger(__name__).debug("auto-save failed: %s", exc)

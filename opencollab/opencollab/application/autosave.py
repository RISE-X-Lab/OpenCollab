"""AutoSaveSubscriber — persists session messages on lifecycle events.

Listens for the events that change persisted state:
- `user_message_appended` — user added a turn
- `step_end`             — assistant finished a step

Save failures are caught and swallowed (the EventBus already isolates
subscribers, but we belt-and-brace here so a disk-full does not log noise
on every step).
"""

from __future__ import annotations

import logging
from typing import Callable

from opencollab.application.ports import EventPublisherPort
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

SAVE_TRIGGERS = frozenset({
    "user_message_appended",
    "step_end",
})


class AutoSaveSubscriber(EventPublisherPort):
    def __init__(self, save_fn: Callable[[], None]):
        self._save = save_fn

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS:
            return
        try:
            self._save()
        except Exception as exc:
            logging.getLogger(__name__).debug("auto-save failed: %s", exc)

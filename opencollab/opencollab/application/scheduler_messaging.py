"""Inter-agent (teammate) messaging for the Scheduler.

A teammate message is queued for async delivery and surfaces to the recipient
as a normal user turn wrapped in an XML envelope. If the recipient is idle it is
scheduled in the background; if it is running or awaiting delegated work the
message stays in an out-of-history inbox until the session can safely accept
another user turn.

``MessagingMixin`` is composed into ``Scheduler`` and relies on the
``_sessions`` / ``_tasks`` / ``_locks`` / ``_message_inbox`` maps and the
``_role_of`` / ``_autosave_session`` / ``_emit_scheduler_event`` / ``_drive_agent``
helpers defined on ``Scheduler``.
"""

from __future__ import annotations

import asyncio
from xml.sax.saxutils import escape, quoteattr

from opencollab.application.scheduler_types import QueuedTeammateMessage
from opencollab.domain.session import SessionPhase


class MessagingMixin:
    """Queue, format, and drain teammate messages between agents."""

    async def send_message(self, from_aid: int, to_aid: int, summary: str, content: str) -> str:
        """Queue a teammate message for async delivery and return immediately.

        The recipient sees the message as a normal user turn with an XML
        envelope. If the recipient is idle, it is scheduled in the background;
        if it is running or awaiting delegated work, the message stays in an
        out-of-history inbox until the session can safely accept another user
        turn.
        """
        if to_aid == from_aid:
            return "Error: an agent cannot message itself."
        target = self._sessions.get(to_aid)
        if target is None:
            return f"Error: no agent with aid {to_aid}."
        if self._topology is not None and not self._topology.allows(
            self._role_of(from_aid), self._role_of(to_aid)
        ):
            return (
                f"Error: role '{self._role_of(from_aid)}' is not permitted to "
                f"message '{self._role_of(to_aid)}' under the team topology."
            )

        lock = self._locks.setdefault(to_aid, asyncio.Lock())
        async with lock:
            message = QueuedTeammateMessage(
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                xml=self._format_teammate_message(from_aid, summary, content),
            )
            self._message_inbox.setdefault(to_aid, []).append(message)
            target.state.queue_pending_user_message(
                {
                    "role": "user",
                    "content": message.xml,
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "summary": summary,
                }
            )
            self._autosave_session(to_aid)
            await self._emit_scheduler_event(
                "agent_message_sent",
                {
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "role": self._role_of(to_aid),
                    "summary": summary,
                },
            )
            await self._drain_message_inbox_locked(to_aid)
        return f"Message queued to aid {to_aid}."

    @staticmethod
    def _format_teammate_message(from_aid: int, summary: str, content: str) -> str:
        sender = f"A{from_aid}"
        return (
            f"<teammate-message teammate_id={quoteattr(sender)} "
            f"summary={quoteattr(summary)}>\n"
            f"{escape(content)}\n"
            "</teammate-message>"
        )

    async def _drain_message_inbox(self, aid: int, *, allow_current_task: bool = False) -> None:
        lock = self._locks.setdefault(aid, asyncio.Lock())
        async with lock:
            await self._drain_message_inbox_locked(aid, allow_current_task=allow_current_task)

    async def _drain_message_inbox_locked(
        self,
        aid: int,
        *,
        allow_current_task: bool = False,
    ) -> None:
        inbox = self._message_inbox.get(aid)
        if not inbox:
            return
        session = self._sessions.get(aid)
        scb = self.table.get(aid)
        if session is None or scb is None:
            return
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return
        if scb.state.phase is SessionPhase.AWAITING_EVENTS or not scb.state.pending_events.is_empty():
            return

        messages = list(inbox)
        inbox.clear()
        for message in messages:
            session.state.discard_pending_user_message(message.xml)
            await session.add_user_message(message.xml)
            await self._emit_scheduler_event(
                "agent_message_delivered",
                {
                    "from_aid": message.from_aid,
                    "to_aid": message.to_aid,
                    "summary": message.summary,
                    "content_len": len(message.content),
                },
            )
        self._autosave_session(aid)

        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))

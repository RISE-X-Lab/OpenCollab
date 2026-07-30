"""Inter-agent (teammate) messaging for the Scheduler.

A teammate message is queued for async delivery and surfaces to the recipient
as a normal user turn wrapped in an XML envelope. If the recipient is idle it is
scheduled in the background; if it is running or awaiting delegated work the
message stays in an out-of-history inbox until the session can safely accept
another user turn.

``MessagingMixin`` is composed into ``Scheduler`` and relies on the
``_sessions`` / ``_tasks`` / ``_locks`` / ``_message_inbox`` maps and the
``_role_of`` / ``_autosave_session`` / ``emit_scheduler_event`` / ``_drive_agent``
helpers defined on ``Scheduler``.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any
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
        if self._shutting_down:
            return "Error: scheduler is shutting down."
        if self._sessions.get(from_aid) is None or self.table.get(from_aid) is None:
            return f"Error: no sending agent with aid {from_aid}."
        target = self._sessions.get(to_aid)
        if target is None:
            return f"Error: no agent with aid {to_aid}."
        if self._topology_forbids(self._role_of(from_aid), self._role_of(to_aid)):
            return (
                f"Error: role '{self._role_of(from_aid)}' is not permitted to "
                f"message '{self._role_of(to_aid)}' under the team topology."
            )

        lock = self._locks.setdefault(to_aid, asyncio.Lock())
        delivered_events = []
        async with lock:
            if self._shutting_down:
                return "Error: scheduler is shutting down."
            xml = self._format_teammate_message(from_aid, summary, content)
            target.state.queue_pending_user_message(
                {
                    "role": "user",
                    "content": xml,
                    "message_content": content,
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "summary": summary,
                }
            )
            sent_at = str(target.state.pending_user_messages[-1]["timestamp"])
            message = QueuedTeammateMessage(
                from_aid=from_aid,
                to_aid=to_aid,
                summary=summary,
                content=content,
                xml=xml,
                sent_at=sent_at,
            )
            self._message_inbox.setdefault(to_aid, []).append(message)
            self._autosave_session(to_aid)
            delivered_events = await self._drain_message_inbox_locked(to_aid)
        # Scheduler events are observational and may re-enter send_message. Emit
        # only after releasing the per-target lock, and isolate sink failures so
        # durable queue mutation and the drive task cannot be rolled back halfway.
        await self._safe_emit_scheduler_event(
            self._events.agent_message_sent(
                from_aid, to_aid, self._role_of(to_aid), summary
            )
        )
        for event in delivered_events:
            await self._safe_emit_scheduler_event(event)
        return f"Message queued to aid {to_aid}."

    async def _append_user_turn_txn(
        self, aid: int, session: Any, message: str, prior_lease: tuple[int, int] | None
    ) -> None:
        """Append ``message`` as a user turn under a rollback transaction.

        The single transaction shared by the external user-turn loop
        (``_run_turn_exclusive``) and the message-inbox drain
        (``_drain_message_inbox_locked``). The caller has already reserved the turn lease and captured
        ``prior_lease``; this owns only the atomic part: mark the delivery task,
        snapshot the turn, try the append, and on ANY failure roll the turn back
        byte-identical (``restore_user_turn``) and release-then-restore the lease
        before re-raising, always popping the delivery-task marker in
        ``finally``. Each caller keeps its own preamble (reserve) and postscript
        (drive-task / shutting-down handling).
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            self._message_delivery_tasks[aid] = current_task
        checkpoint = session.state.checkpoint_user_turn()
        try:
            await session.add_user_message(message)
        except BaseException:
            session.state.restore_user_turn(checkpoint)
            self._autosave_session(aid)
            self._release_turn_lease(aid)
            if not self._shutting_down:
                self._restore_turn_lease(aid, prior_lease)
            raise
        finally:
            if self._message_delivery_tasks.get(aid) is current_task:
                self._message_delivery_tasks.pop(aid, None)

    def _restore_message_inbox(self, aid: int, state: object) -> None:
        """Rebuild scheduler-owned delivery records from a durable sidecar."""
        pending = getattr(state, "pending_user_messages", None)
        if not isinstance(pending, list) or not pending:
            return
        restored: list[QueuedTeammateMessage] = []
        for item in pending:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            xml = str(item["content"])
            restored.append(
                QueuedTeammateMessage(
                    from_aid=self._restored_aid(item.get("from_aid"), default=-1),
                    to_aid=self._restored_aid(item.get("to_aid"), default=aid),
                    summary=str(item.get("summary") or "restored teammate message"),
                    content=str(
                        item.get("message_content")
                        or self._message_content_from_xml(xml)
                    ),
                    xml=xml,
                    sent_at=str(item.get("timestamp") or ""),
                )
            )
        if restored:
            self._message_inbox[aid] = restored

    @staticmethod
    def _restored_aid(value: object, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _message_content_from_xml(xml: str) -> str:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return xml
        content = "".join(root.itertext())
        if content.startswith("\n") and content.endswith("\n"):
            return content[1:-1]
        return content

    @staticmethod
    def _format_teammate_message(from_aid: int, summary: str, content: str) -> str:
        sender = f"A{from_aid}"
        return (
            f"<teammate-message teammate_id={quoteattr(sender)} "
            f"summary={quoteattr(summary)}>\n"
            f"{escape(content)}\n"
            "</teammate-message>"
        )

    @staticmethod
    def _format_teammate_message_batch(messages: list[QueuedTeammateMessage]) -> str:
        envelopes = []
        for message in messages:
            sender = f"A{message.from_aid}"
            envelopes.append(
                f"<teammate-message teammate_id={quoteattr(sender)} "
                f"summary={quoteattr(message.summary)} "
                f"sent_at={quoteattr(message.sent_at)}>\n"
                f"{escape(message.content)}\n"
                "</teammate-message>"
            )
        return (
            f'<teammate-messages count="{len(messages)}">\n'
            + "\n".join(envelopes)
            + "\n</teammate-messages>"
        )

    async def _drain_message_inbox(self, aid: int, *, allow_current_task: bool = False) -> None:
        lock = self._locks.setdefault(aid, asyncio.Lock())
        async with lock:
            events = await self._drain_message_inbox_locked(
                aid, allow_current_task=allow_current_task
            )
        for event in events:
            await self._safe_emit_scheduler_event(event)

    async def _drain_ready_message_inboxes(self) -> None:
        """Retry durable messages when another turn has returned budget headroom."""
        for aid in list(self._message_inbox):
            if self._message_inbox.get(aid):
                await self._drain_message_inbox(aid)

    async def _drain_message_inbox_locked(
        self,
        aid: int,
        *,
        allow_current_task: bool = False,
    ) -> list[object]:
        inbox = self._message_inbox.get(aid)
        if not inbox:
            return []
        session = self._sessions.get(aid)
        scb = self.table.get(aid)
        if session is None or scb is None:
            return []
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return []
        if scb.state.phase is SessionPhase.AWAITING_EVENTS or not scb.state.pending_events.is_empty():
            return []
        prior_lease = self._current_turn_lease(aid)
        if self._shutting_down or not self._reserve_message_budget(aid):
            return []

        messages = list(inbox)
        delivery = (
            messages[0].xml
            if len(messages) == 1
            else self._format_teammate_message_batch(messages)
        )
        await self._append_user_turn_txn(aid, session, delivery, prior_lease)

        if self._shutting_down:
            self._release_turn_lease(aid)
            return []
        del inbox[: len(messages)]
        for message in messages:
            session.state.discard_pending_user_message(message.xml)
        self._autosave_session(aid)

        self._tasks[aid] = asyncio.create_task(self._drive_agent(aid, session))
        return [
            (
                self._events.agent_message_delivered(
                    message.from_aid,
                    message.to_aid,
                    message.summary,
                    len(message.content),
                )
            )
            for message in messages
        ]

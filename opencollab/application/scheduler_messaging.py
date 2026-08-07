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
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from opencollab.application._scheduler_constants import (
    MAX_TEAMMATE_DELIVERY_BYTES,
    MAX_TEAMMATE_INBOX_BYTES,
    MAX_TEAMMATE_INBOX_MESSAGES,
    MAX_TEAMMATE_MESSAGE_BYTES,
)
from opencollab.application.scheduler_types import QueuedTeammateMessage
from opencollab.domain.identity import role_collision_key
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
            message_id = uuid.uuid4().hex
            from_role = self._role_of(from_aid)
            to_role = self._role_of(to_aid)
            xml = self._format_teammate_message(
                from_aid,
                summary,
                content,
                message_id=message_id,
            )
            message_bytes = self._encoded_size(xml)
            if message_bytes > MAX_TEAMMATE_MESSAGE_BYTES:
                return (
                    "Error: teammate message exceeds the "
                    f"{MAX_TEAMMATE_MESSAGE_BYTES}-byte limit."
                )
            inbox = self._message_inbox.get(to_aid, [])
            if len(inbox) >= MAX_TEAMMATE_INBOX_MESSAGES:
                return f"Error: teammate inbox for aid {to_aid} is full (backpressure)."
            if self._inbox_size(inbox) + message_bytes > MAX_TEAMMATE_INBOX_BYTES:
                return (
                    f"Error: teammate inbox for aid {to_aid} exceeds the "
                    f"{MAX_TEAMMATE_INBOX_BYTES}-byte limit (backpressure)."
                )
            target.state.queue_pending_user_message(
                {
                    "role": "user",
                    "content": xml,
                    "message_content": content,
                    "from_aid": from_aid,
                    "to_aid": to_aid,
                    "from_role": from_role,
                    "to_role": to_role,
                    "summary": summary,
                    "message_id": message_id,
                    "delivery_status": "pending",
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
                message_id=message_id,
                from_role=from_role,
                to_role=to_role,
            )
            inbox.append(message)
            self._message_inbox[to_aid] = inbox
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
        committed_ids = self._message_ids_in_history(state)
        restored: list[QueuedTeammateMessage] = []
        for item in list(pending):
            if not isinstance(item, dict) or not item.get("content"):
                continue
            if item.get("delivery_status") == "rejected":
                continue
            xml = str(item["content"])
            message_id = str(item.get("message_id") or self._message_id_from_xml(xml) or "")
            if message_id and message_id in committed_ids:
                state.discard_pending_user_message_id(message_id)
                continue
            if message_id and not item.get("message_id"):
                item["message_id"] = message_id
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
                    message_id=message_id,
                    from_role=str(item.get("from_role") or ""),
                    to_role=str(item.get("to_role") or ""),
                    restored=True,
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
    def _message_id_from_xml(xml: str) -> str | None:
        try:
            return ET.fromstring(xml).attrib.get("message_id")
        except ET.ParseError:
            return None

    @staticmethod
    def _message_ids_in_history(state: object) -> set[str]:
        message_ids: set[str] = set()
        for message in getattr(state, "messages", ()):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                root = ET.fromstring(content)
            except ET.ParseError:
                continue
            for element in root.iter():
                message_id = element.attrib.get("message_id")
                if message_id:
                    message_ids.add(message_id)
        return message_ids

    @staticmethod
    def _format_teammate_message(
        from_aid: int,
        summary: str,
        content: str,
        *,
        message_id: str = "",
    ) -> str:
        sender = f"A{from_aid}"
        message_id_attr = f" message_id={quoteattr(message_id)}" if message_id else ""
        return (
            f"<teammate-message teammate_id={quoteattr(sender)} "
            f"summary={quoteattr(summary)}{message_id_attr}>\n"
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
                f"sent_at={quoteattr(message.sent_at)}"
                f"{f' message_id={quoteattr(message.message_id)}' if message.message_id else ''}>\n"
                f"{escape(message.content)}\n"
                "</teammate-message>"
            )
        return (
            f'<teammate-messages count="{len(messages)}">\n'
            + "\n".join(envelopes)
            + "\n</teammate-messages>"
        )

    @staticmethod
    def _encoded_size(value: str) -> int:
        return len(value.encode("utf-8"))

    @classmethod
    def _inbox_size(cls, inbox: list[QueuedTeammateMessage]) -> int:
        return sum(cls._encoded_size(message.xml) for message in inbox)

    @classmethod
    def _bounded_message_batch(
        cls, inbox: list[QueuedTeammateMessage]
    ) -> list[QueuedTeammateMessage]:
        """Select the largest FIFO prefix whose rendered prompt is bounded."""
        batch: list[QueuedTeammateMessage] = []
        for message in inbox:
            candidate = [*batch, message]
            delivery = (
                candidate[0].xml
                if len(candidate) == 1
                else cls._format_teammate_message_batch(candidate)
            )
            if cls._encoded_size(delivery) > MAX_TEAMMATE_DELIVERY_BYTES:
                break
            batch = candidate
        return batch

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
        events = []
        retained = []
        rejected = False
        for message in inbox:
            reason = self._message_route_error(aid, message)
            if reason is None:
                retained.append(message)
                continue
            rejected = True
            self._mark_message_rejected(session.state, message, reason)
            events.append(
                self._events.agent_message_rejected_on_restore(
                    message.from_aid,
                    aid,
                    reason,
                )
            )
        if rejected:
            inbox[:] = retained
            self._autosave_session(aid)
        if not inbox:
            return events
        task = self._tasks.get(aid)
        current_task = asyncio.current_task()
        if (
            task is not None
            and not task.done()
            and not (allow_current_task and task is current_task)
        ):
            return events
        if scb.state.phase is SessionPhase.AWAITING_EVENTS or not scb.state.pending_events.is_empty():
            return events
        messages = self._bounded_message_batch(inbox)
        if not messages or self._shutting_down:
            return events
        prior_lease = self._current_turn_lease(aid)
        if not self._reserve_message_budget(aid):
            return events

        delivery = (
            messages[0].xml
            if len(messages) == 1
            else self._format_teammate_message_batch(messages)
        )
        await self._append_user_turn_txn(aid, session, delivery, prior_lease)

        # Commit the durable dequeue immediately after the history append. A
        # shutdown may stop the follow-on driver, but it must never leave the
        # already-appended message in a sidecar that restore would re-deliver.
        del inbox[: len(messages)]
        for message in messages:
            if message.message_id:
                session.state.discard_pending_user_message_id(message.message_id)
            else:
                session.state.discard_pending_user_message(message.xml)
        self._autosave_session(aid)

        if self._shutting_down:
            self._release_turn_lease(aid)
            return events

        self._start_agent_task(aid, session)
        return events + [
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

    def _message_route_error(
        self,
        aid: int,
        message: QueuedTeammateMessage,
    ) -> str | None:
        """Validate a durable message against the current roster and topology."""
        if message.to_aid != aid:
            return (
                f"restored target aid changed from {message.to_aid} to {aid}"
            )
        if message.from_aid == aid:
            return "restored route would deliver a message to its sender"
        sender = self.table.get(message.from_aid)
        target = self.table.get(aid)
        if sender is None or self._sessions.get(message.from_aid) is None:
            return f"restored sender aid {message.from_aid} no longer exists"
        if target is None or self._sessions.get(aid) is None:
            return f"restored target aid {aid} no longer exists"

        current_from_role = sender.agent.name
        current_to_role = target.agent.name
        if message.from_role:
            if role_collision_key(message.from_role) != role_collision_key(
                current_from_role
            ):
                return (
                    f"restored sender role changed from {message.from_role!r} "
                    f"to {current_from_role!r}"
                )
        elif message.restored and self._topology is not None:
            return "restored message has no durable sender role identity"
        if message.to_role:
            if role_collision_key(message.to_role) != role_collision_key(
                current_to_role
            ):
                return (
                    f"restored target role changed from {message.to_role!r} "
                    f"to {current_to_role!r}"
                )
        elif message.restored and self._topology is not None:
            return "restored message has no durable target role identity"
        if self._topology_forbids(current_from_role, current_to_role):
            return (
                f"restored route {current_from_role!r} to {current_to_role!r} "
                "is forbidden by the current team topology"
            )
        return None

    @staticmethod
    def _mark_message_rejected(
        state: object,
        message: QueuedTeammateMessage,
        reason: str,
    ) -> None:
        """Move one pending sidecar record into a durable rejected state."""
        pending = getattr(state, "pending_user_messages", ())
        for item in pending:
            if not isinstance(item, dict):
                continue
            same_message = (
                bool(message.message_id)
                and item.get("message_id") == message.message_id
            ) or (
                not message.message_id
                and item.get("content") == message.xml
                and item.get("from_aid") == message.from_aid
            )
            if not same_message:
                continue
            item["delivery_status"] = "rejected"
            item["rejection_reason"] = reason
            return

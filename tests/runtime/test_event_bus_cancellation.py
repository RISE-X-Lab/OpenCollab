from __future__ import annotations

import asyncio

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap import build_session
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from tests.support.session_characterization_test_support import FakeAgent, FakeLLMClient


def test_cancelled_emit_returns_while_subscriber_cleanup_is_tracked():
    bus = EventBus()
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_resistant(_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    bus.subscribe(cancellation_resistant)
    session = build_session(agent=FakeAgent(), llm=FakeLLMClient(), event_sink=bus)

    async def scenario():
        emit = asyncio.create_task(bus.emit(SessionEvent(type="ping")))
        await started.wait()
        emit.cancel()
        emit.cancel()
        await cancellation_seen.wait()

        finished, _pending = await asyncio.wait({emit}, timeout=0.05)
        promptly_cancelled = emit in finished and emit.cancelled()
        pending_tasks = bus.pending_tasks
        session_pending = session.pending_cleanup_tasks

        release.set()
        await asyncio.gather(emit, return_exceptions=True)
        await asyncio.gather(*bus.pending_tasks, return_exceptions=True)

        assert promptly_cancelled
        assert pending_tasks
        assert session_pending == pending_tasks
        assert bus.pending_tasks == ()

    asyncio.run(scenario())


def test_event_bus_accepts_callback_returning_an_existing_task():
    bus = EventBus()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    async def scenario():
        task = asyncio.create_task(work())
        await started.wait()
        bus.subscribe(lambda _event: task)
        emit = asyncio.create_task(bus.emit(SessionEvent(type="ping")))
        await asyncio.sleep(0)
        assert not emit.done()
        release.set()
        await emit
        assert task.done()

    asyncio.run(scenario())

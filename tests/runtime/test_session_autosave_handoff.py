from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.application.session import Session, SessionBusyError
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.environments import local_environment
from tests.support.paths import REPO_ROOT
from tests.support.session_run_test_support import FakeLLM, llm_response

_AUTOSAVE = "autosave.json"
_TIMEOUT = 10
_WORKER_MODULE = "tests.runtime.test_session_autosave_handoff"


class _LateProvider(FakeLLM):
    def __init__(self) -> None:
        super().__init__([
            llm_response(content="Initial review recorded.", total_tokens=10),
            llm_response(content="Late review response.", total_tokens=20),
        ])
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, *args, **kwargs):
        if self.calls:
            self.admitted.set()
            # The provider reports usage after the caller has cancelled the turn.
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
        return await super().complete(*args, **kwargs)


def _session_options(root: Path, llm: FakeLLM):
    return {
        "agent": Agent(name="reviewer", system_prompt="Continue the review."),
        "llm": llm,
        "env": local_environment(root),
        "auto_save_path": str(root / _AUTOSAVE),
        "llm_timeout": 600,
    }


def _saved_state(root: Path) -> tuple[str, int]:
    state = OpenCollab.read_session_snapshot(root / _AUTOSAVE)["session_state"]
    return state["phase"], state["used_tokens"]


async def _drain(session: Session) -> None:
    while tasks := session.pending_cleanup_tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _hold_autosave(root: Path) -> None:
    provider = _LateProvider()
    session = build_session(**_session_options(root, provider))
    await session.add_user_message("Record the initial review.")
    assert await session.run_loop() == "Initial review recorded."
    await session.add_user_message("Review the next section.")
    turn = asyncio.create_task(session.run_loop())
    await asyncio.wait_for(provider.admitted.wait(), _TIMEOUT)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, _TIMEOUT)
    await session.aclose()
    assert session.pending_execution_tasks
    print("waiting for provider", flush=True)

    await asyncio.to_thread(sys.stdin.readline)
    provider.release.set()
    await asyncio.wait_for(_drain(session), _TIMEOUT)
    assert session.used_tokens == 30
    print("settled", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await session.env.cleanup()


async def _resume_autosave(root: Path) -> None:
    llm = FakeLLM([llm_response(content="Follow-up recorded.", total_tokens=5)])
    options = _session_options(root, llm)
    try:
        session = load_session(str(root / _AUTOSAVE), **options)
    except SessionBusyError:
        print("busy", flush=True)
        return
    inherited_tokens = session.used_tokens
    try:
        await session.add_user_message("Continue from the saved review.")
        assert await session.run_loop() == "Follow-up recorded."
    finally:
        await session.aclose()
        await session.env.cleanup()
    print(f"resumed from {inherited_tokens} tokens", flush=True)


async def _start_worker(role: str, root: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", _WORKER_MODULE, role, str(root),
        cwd=REPO_ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _kill_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.communicate(), _TIMEOUT)


async def _finish_worker(process: asyncio.subprocess.Process, command: bytes | None = None) -> str:
    try:
        output, errors = await asyncio.wait_for(process.communicate(command), _TIMEOUT)
    finally:
        await _kill_worker(process)
    assert process.returncode == 0, errors.decode(errors="replace")
    return output.decode().strip()


async def _expect_worker_line(process: asyncio.subprocess.Process, expected: bytes) -> None:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), _TIMEOUT)
    if not line:
        await _finish_worker(process)
    assert line == expected


@pytest.mark.parametrize("crash_owner", [False, True], ids=["settled", "killed"])
async def test_autosave_handoff_between_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_owner: bool):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    owner = await _start_worker("hold", tmp_path)
    try:
        await _expect_worker_line(owner, b"waiting for provider\n")
        assert _saved_state(tmp_path) == ("stopped", 10)
        for destination in (None, str(tmp_path / "copy.json")):
            options = _session_options(tmp_path, FakeLLM())
            options["auto_save_path"] = destination
            observer = load_session(str(tmp_path / _AUTOSAVE), **options)
            try:
                assert observer.used_tokens == 10
                if destination is not None:
                    assert OpenCollab.read_session_snapshot(destination)["session_state"]["used_tokens"] == 10
            finally:
                await observer.aclose()
                await observer.env.cleanup()
        contender = await _start_worker("resume", tmp_path)
        assert await _finish_worker(contender) == "busy"
        assert _saved_state(tmp_path) == ("stopped", 10)

        if crash_owner:
            await _kill_worker(owner)
            assert owner.returncode != 0
            inherited_tokens = 10
        else:
            assert owner.stdin is not None
            owner.stdin.write(b"release\n")
            await owner.stdin.drain()
            await _expect_worker_line(owner, b"settled\n")
            assert _saved_state(tmp_path) == ("stopped", 30)
            inherited_tokens = 30

        successor = await _start_worker("resume", tmp_path)
        assert await _finish_worker(successor) == f"resumed from {inherited_tokens} tokens"
        assert _saved_state(tmp_path) == ("done", inherited_tokens + 5)
        if not crash_owner:
            assert owner.returncode is None
            await _finish_worker(owner, command=b"exit\n")
    finally:
        await _kill_worker(owner)

async def test_queued_autosave_cannot_replace_a_live_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    path = tmp_path / _AUTOSAVE
    admitted, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(FakeLLM):
        async def complete(self, *args, **kwargs):
            admitted.set()
            await release.wait()
            return await super().complete(*args, **kwargs)

    older = build_session(**_session_options(
        tmp_path, FakeLLM([llm_response(content="Recorded one task.", total_tokens=10)]),
    ))
    newer = turn = None
    try:
        await older.add_user_message("Review the first section.")
        assert await older.run_loop() == "Recorded one task."
        newer = load_session(str(path), **_session_options(
            tmp_path, WaitingProvider([llm_response(content="Recorded the second task.", total_tokens=20)]),
        ))
        await newer.add_user_message("Review the second section.")
        turn = asyncio.create_task(newer.run_loop())
        await asyncio.wait_for(admitted.wait(), _TIMEOUT)
        await asyncio.gather(*newer.event_bus.pending_tasks)
        before = OpenCollab.read_session_snapshot(path)
        assert any(row["content"] == "Review the second section." for row in before["messages"])
        with pytest.raises(SessionBusyError, match="active writer"):
            older.enqueue_auto_save()
        assert OpenCollab.read_session_snapshot(path) == before
        release.set()
        assert await asyncio.wait_for(turn, _TIMEOUT) == "Recorded the second task."
        assert _saved_state(tmp_path) == ("done", 30)
    finally:
        release.set()
        if turn is not None:
            await asyncio.gather(turn, return_exceptions=True)
        if newer is not None:
            await newer.aclose()
            await newer.env.cleanup()
        await older.aclose()
        await older.env.cleanup()


if __name__ == "__main__":
    workers = {"hold": _hold_autosave, "resume": _resume_autosave}
    asyncio.run(workers[sys.argv[1]](Path(sys.argv[2])))

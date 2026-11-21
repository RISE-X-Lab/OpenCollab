from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.docker_native_edits_support import LocalDockerTransport, docker_environment


class _PublishedReceipt(LocalDockerTransport):
    def __init__(self, workspace, *, delay):
        super().__init__(workspace, pair_reads=False)
        self.delay = delay
        self.published = asyncio.Event()

    async def __call__(self, *argv, **kwargs):
        result = await super().__call__(*argv, **kwargs)
        if kwargs.get("input_bytes") is not None and "opencollab-write " in argv[-1]:
            self.published.set()
            if self.delay:
                await asyncio.Event().wait()
        return result


@pytest.mark.parametrize("cancel", [False, True], ids=["completed", "lost-receipt"])
async def test_attached_cleanup_removes_published_temporary_after_lost_receipt(tmp_path, cancel):
    transport = _PublishedReceipt(tmp_path, delay=cancel)
    env = docker_environment(tmp_path, transport)
    targets = []
    write_atomic = env._write_file_atomic

    async def observe_target(target, payload, **kwargs):
        targets.append(Path(target))
        return await write_atomic(target, payload, **kwargs)

    env._write_file_atomic = observe_target
    writer = asyncio.create_task(env.write_temp_file("retained candidate\n", prefix="oc-owned-temp-"))
    try:
        await asyncio.wait_for(transport.published.wait(), 5)
        if cancel:
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
        else:
            await writer
        assert len(targets) == 1
        assert targets[0].read_text() == "retained candidate\n"
        assert {Path(path).resolve() for path in env._temporary_files} == {targets[0].resolve()}
        await env.cleanup()
        assert not targets[0].exists()
        assert not env._temporary_files
    finally:
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await env.cleanup()
        for target in targets:
            target.unlink(missing_ok=True)

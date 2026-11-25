"""Repeated public team runs release owned workspace descriptors."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.storage import SessionStore
from opencollab.environments import local_environment
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _anchors(workspace: Path) -> set[int]:
    identity = workspace.stat()
    found = set()
    for name in os.listdir('/dev/fd'):
        fd = int(name)
        try:
            opened = os.fstat(fd)
        except OSError:
            continue
        if (opened.st_dev, opened.st_ino) == (identity.st_dev, identity.st_ino):
            found.add(fd)
    return found


@pytest.fixture
def team_workspace(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    for args in (('init', '-q'), ('-c', 'user.name=Tests', '-c', 'user.email=tests@example.invalid',
                                 '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'initial')):
        subprocess.run(['git', *args], cwd=workspace, check=True, capture_output=True)
    team = tmp_path / 'team.yaml'
    team.write_text('entry: lead\nroles:\n  lead:\n    prompt: Reply once.\n    tools: []\n')
    return workspace, team


@pytest.mark.parametrize('record_delivery_tree', [False, True])
@pytest.mark.parametrize('supplied', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
async def test_repeated_public_teams_release_owned_anchors(
    monkeypatch, team_workspace, record_delivery_tree, supplied, outcome,
):
    workspace, team = team_workspace
    entered = asyncio.Event()

    async def respond(request):
        if outcome == 'failure':
            return http_response(request, 400, json={'error': {
                'message': 'offline provider failure', 'type': 'invalid_request_error',
            }})
        if outcome == 'cancel':
            entered.set()
            await asyncio.Event().wait()
        return completion_http_response(request, text='finished')

    install_sdk_transport(monkeypatch, 'openai', respond)
    external = local_environment(workspace) if supplied else None
    app = OpenCollab(workspace, model='offline', provider='openai',
                     api_key='offline-unused',  # pragma: allowlist secret
                     base_url='https://offline.invalid/v1', environment=external,
                     config={'llm_max_retries': 0})
    baseline = _anchors(workspace)
    try:
        for _ in range(3):
            entered.clear()
            task = asyncio.create_task(app.team(
                'Reply once.', config=team, trace=False, use_worktrees=False,
                record_delivery_tree=record_delivery_tree,
            ))
            if outcome == 'cancel':
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result.status == ('completed' if outcome == 'success' else 'failed')
                assert result.metrics['session_quiesced'] is True
            assert _anchors(workspace) == baseline
            if external is not None:
                checked = await external.exec_cmd('printf environment-alive')
                assert checked.returncode == 0
                assert checked.stdout == 'environment-alive'
    finally:
        if external is not None:
            await external.cleanup()


@pytest.mark.parametrize('record_delivery_tree', [False, True])
@pytest.mark.parametrize('supplied', [False, True])
@pytest.mark.parametrize('stage', ['session', 'snapshot'])
async def test_team_construction_failure_releases_only_owned_anchors(
    monkeypatch, team_workspace, tmp_path, record_delivery_tree, supplied, stage,
):
    from opencollab.bootstrap import session_factory

    workspace, team = team_workspace
    install_sdk_transport(monkeypatch, 'openai', lambda request: completion_http_response(request, text='finished'))
    external = local_environment(workspace) if supplied else None
    app = OpenCollab(workspace, model='offline', provider='openai',
                     api_key='offline-unused',  # pragma: allowlist secret
                     base_url='https://offline.invalid/v1', environment=external)
    baseline = _anchors(workspace)

    def failed_save(*args, **kwargs):
        raise OSError('initial snapshot unavailable')

    if stage == 'session':
        monkeypatch.setattr(session_factory, 'build_session', failed_save)
    else:
        monkeypatch.setattr(SessionStore, 'checkpoint_snapshot', failed_save)
    try:
        with pytest.raises(OSError, match='initial snapshot unavailable'):
            await app.team('Reply once.', config=team, trace=False, use_worktrees=False,
                           artifacts=tmp_path / 'artifacts', record_delivery_tree=record_delivery_tree)
        assert _anchors(workspace) == baseline
        if external is not None:
            checked = await external.exec_cmd('printf environment-alive')
            assert checked.stdout == 'environment-alive'
    finally:
        if external is not None:
            await external.cleanup()


@pytest.mark.parametrize('record_delivery_tree', [False, True])
async def test_owned_team_anchor_waits_for_provider_quiescence(
    monkeypatch, team_workspace, record_delivery_tree,
):
    from opencollab.bootstrap import programmatic

    workspace, team = team_workspace
    entered, released = asyncio.Event(), asyncio.Event()
    schedulers = []
    original = programmatic.build_scheduler

    def remember_scheduler(*args, **kwargs):
        scheduler = original(*args, **kwargs)
        schedulers.append(scheduler)
        return scheduler

    async def respond(request):
        entered.set()
        while not released.is_set():
            try:
                await released.wait()
            except asyncio.CancelledError:
                pass
        return completion_http_response(request, text='late response')

    monkeypatch.setattr(programmatic, 'build_scheduler', remember_scheduler)
    install_sdk_transport(monkeypatch, 'openai', respond)
    app = OpenCollab(workspace, model='offline', provider='openai',
                     api_key='offline-unused',  # pragma: allowlist secret
                     base_url='https://offline.invalid/v1', config={'llm_max_retries': 0, 'llm_timeout': 0.05})
    baseline = _anchors(workspace)
    run = asyncio.create_task(app.team(
        'Reply once.', config=team, trace=False, use_worktrees=False,
        cleanup_timeout=0.05, record_delivery_tree=record_delivery_tree,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await asyncio.wait_for(run, 5)
        assert result.metrics['execution_quiesced'] is False
        assert _anchors(workspace) > baseline
    finally:
        released.set()
        await asyncio.gather(run, return_exceptions=True)
        scheduler = schedulers[0]
        async def wait_for_provider():
            while scheduler.lead_session.pending_cleanup_tasks:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait_for_provider(), 5)
        await scheduler.cleanup()
    assert _anchors(workspace) == baseline


@pytest.mark.parametrize('record_delivery_tree', [False, True])
async def test_owned_team_anchor_survives_cleanup_failure_for_retry(
    monkeypatch, team_workspace, record_delivery_tree,
):
    from opencollab.adapters.env import LocalEnvironment
    from opencollab.bootstrap import programmatic

    workspace, team = team_workspace
    schedulers = []
    original_build = programmatic.build_scheduler
    original_cleanup = LocalEnvironment._cleanup_files
    failed = False

    def remember_scheduler(*args, **kwargs):
        scheduler = original_build(*args, **kwargs)
        schedulers.append(scheduler)
        return scheduler

    async def fail_first_cleanup(env):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError('temporary cleanup failure')
        await original_cleanup(env)

    monkeypatch.setattr(programmatic, 'build_scheduler', remember_scheduler)
    monkeypatch.setattr(LocalEnvironment, '_cleanup_files', fail_first_cleanup)
    install_sdk_transport(monkeypatch, 'openai', lambda request: completion_http_response(request, text='finished'))
    app = OpenCollab(workspace, model='offline', provider='openai',
                     api_key='offline-unused',  # pragma: allowlist secret
                     base_url='https://offline.invalid/v1', config={'llm_max_retries': 0})
    baseline = _anchors(workspace)
    try:
        result = await app.team('Reply once.', config=team, trace=False, use_worktrees=False,
                                record_delivery_tree=record_delivery_tree)
        assert result.status == 'failed'
        assert result.metrics['execution_quiesced'] is False
        assert _anchors(workspace) > baseline
    finally:
        await schedulers[0].cleanup()
    assert _anchors(workspace) == baseline


@pytest.mark.parametrize("interruption", ["caller", "timeout"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_interrupted_team_keeps_its_outcome_when_cleanup_retries(
    monkeypatch, team_workspace, interruption, cleanup_fails,
):
    from opencollab.adapters.env import LocalEnvironment

    workspace, team = team_workspace
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    cleanup_calls = 0
    real_cleanup = LocalEnvironment._cleanup_files

    async def respond(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def cleanup(environment):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1 and cleanup_fails:
            raise OSError("temporary workspace cleanup failure")
        await real_cleanup(environment)

    install_sdk_transport(monkeypatch, "openai", respond)
    monkeypatch.setattr(LocalEnvironment, "_cleanup_files", cleanup)
    app = OpenCollab(
        workspace, model="offline", provider="openai",
        api_key="offline-unused",  # pragma: allowlist secret
        base_url="https://offline.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    baseline = _anchors(workspace)
    running = asyncio.create_task(app.team(
        "Reply once.", config=team, trace=False, use_worktrees=False,
        timeout=2 if interruption == "timeout" else None, cleanup_timeout=1,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if interruption == "caller":
            running.cancel("caller ended team")
            with pytest.raises(asyncio.CancelledError, match="caller ended team") as caught:
                await running
            cancellation = caught.value
        else:
            result = await running
            assert (result.status, result.reason) == ("stopped", "timeout")
            assert isinstance(result.error, asyncio.TimeoutError)
            assert result.metrics["execution_quiesced"] is True
            cancellation = result.error.__cause__
            assert isinstance(cancellation, asyncio.CancelledError)
        assert any(
            "scheduler cleanup also failed" in note for note in getattr(cancellation, "__notes__", ())
        ) is cleanup_fails
        assert cancelled.is_set()
        assert cleanup_calls == (2 if cleanup_fails else 1)
        assert _anchors(workspace) == baseline
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

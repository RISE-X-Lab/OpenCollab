"""Tests for the ``workflow`` CLI subcommands (list + run).

The registry and runtime are stubbed so no real LLM session is ever built. We
drive the Typer commands through ``CliRunner`` and assert on stdout.
"""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from opencollab.adapters.cli import workflow as workflow_cli
from opencollab.application.workflow_registry import Registry, workflow

runner = CliRunner()


def _stub_registry() -> Registry:
    @workflow(name="alpha", description="the alpha workflow")
    async def a(ctx, args):
        return None

    @workflow(name="beta", description="the beta workflow")
    async def b(ctx, args):
        return None

    reg = Registry()
    reg.register(a.__workflow_spec__)
    reg.register(b.__workflow_spec__)
    return reg


def test_workflow_list_prints_names_and_descriptions(monkeypatch):
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: _stub_registry())

    result = runner.invoke(workflow_cli.app, ["list"])

    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "the alpha workflow" in result.stdout
    assert "beta" in result.stdout
    assert "the beta workflow" in result.stdout


def test_workflow_run_prints_result_as_json(monkeypatch):
    @workflow(name="echo", description="echoes its args")
    async def echo(ctx, args):
        return {"received": args}

    reg = Registry()
    reg.register(echo.__workflow_spec__)
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: reg)
    monkeypatch.setattr(workflow_cli, "missing_api_key_for", lambda *a, **k: False)

    captured: dict[str, Any] = {}

    async def fake_run_workflow(spec_or_fn, args, **kwargs):
        captured["spec_or_fn"] = spec_or_fn
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Call the workflow fn directly with a stub ctx to mimic execution.
        return await spec_or_fn.fn(object(), args)

    monkeypatch.setattr(workflow_cli, "run_workflow", fake_run_workflow)

    result = runner.invoke(
        workflow_cli.app,
        ["run", "echo", "--args", '{"name": "bob"}', "--budget", "50000", "--concurrency", "2"],
    )

    assert result.exit_code == 0
    payload = json.loads(_last_json_block(result.stdout))
    assert payload == {"received": {"name": "bob"}}
    assert captured["args"] == {"name": "bob"}
    assert captured["kwargs"]["budget"] == 50000
    assert captured["kwargs"]["max_concurrency"] == 2


def test_workflow_run_unknown_name_exits_nonzero(monkeypatch):
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: _stub_registry())

    result = runner.invoke(workflow_cli.app, ["run", "nope", "--args", "{}"])

    assert result.exit_code != 0


def test_workflow_run_invalid_json_args_exits_nonzero(monkeypatch):
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: _stub_registry())

    result = runner.invoke(workflow_cli.app, ["run", "alpha", "--args", "{not-json"])

    assert result.exit_code != 0


def test_workflow_run_emits_progress_lines(monkeypatch):
    @workflow(name="phased", description="d")
    async def phased(ctx, args):
        await ctx.phase("scanning")
        await ctx.log("found something")
        return "ok"

    reg = Registry()
    reg.register(phased.__workflow_spec__)
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: reg)
    monkeypatch.setattr(workflow_cli, "missing_api_key_for", lambda *a, **k: False)

    async def fake_run_workflow(spec_or_fn, args, *, event_sink=None, **kwargs):
        ctx_stub = _RecordingCtx(event_sink)
        return await spec_or_fn.fn(ctx_stub, args)

    monkeypatch.setattr(workflow_cli, "run_workflow", fake_run_workflow)

    result = runner.invoke(workflow_cli.app, ["run", "phased", "--args", "{}"])

    assert result.exit_code == 0
    assert "scanning" in result.stdout
    assert "found something" in result.stdout


class _RecordingCtx:
    """Minimal ctx stub that forwards phase/log to the provided event sink."""

    def __init__(self, event_sink: Any) -> None:
        self._sink = event_sink

    async def phase(self, title: str) -> None:
        if self._sink is not None:
            await self._sink.emit(_Event("phase", title))

    async def log(self, message: str) -> None:
        if self._sink is not None:
            await self._sink.emit(_Event("log", message))


class _Event:
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message


def _last_json_block(text: str) -> str:
    """Extract the trailing JSON object/array printed by the run command.

    The result is printed as a JSON block at the end of stdout; progress lines
    precede it. Find the last line that begins a JSON value.
    """
    stripped = text.strip()
    candidates = [i for i in (stripped.find("{"), stripped.find("[")) if i != -1]
    start = min(candidates)
    return stripped[start:]

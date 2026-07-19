"""Tests for the ``workflow`` CLI subcommands (list + run).

The registry and runtime are stubbed so no real LLM session is ever built. We
drive the Typer commands through ``CliRunner`` and assert on stdout.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab.adapters.cli import workflow as workflow_cli
from opencollab.application.workflow_registry import Registry, workflow
from typer.testing import CliRunner

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


def test_workflow_run_default_budget_raised_to_1m(monkeypatch):
    """With no --budget, run_cmd raises the workflow default to 1M.

    Workflows fan out many one-shot sessions (mirroring main.py's spawn-aware
    default), so the config fallback budget is lifted to at least 1M and that
    raised value reaches the context: ``--budget`` is None, so run_workflow falls
    back to ``cfg['budget']``.
    """
    @workflow(name="echo", description="echoes its args")
    async def echo(ctx, args):
        return {"ok": True}

    reg = Registry()
    reg.register(echo.__workflow_spec__)
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: reg)
    monkeypatch.setattr(workflow_cli, "missing_api_key_for", lambda *a, **k: False)
    # A small config fallback budget so the 1M floor is the value that wins.
    monkeypatch.setattr(
        workflow_cli,
        "resolve_config",
        lambda *a, **k: {
            "model": "m",
            "provider": "anthropic",
            "api_key": "k",
            "base_url": None,
            "budget": 200_000,
        },
    )

    captured: dict[str, Any] = {}

    async def fake_run_workflow(spec_or_fn, args, *, cfg, budget=None, **kwargs):
        captured["cfg_budget"] = cfg["budget"]
        captured["budget_arg"] = budget
        return await spec_or_fn.fn(object(), args)

    monkeypatch.setattr(workflow_cli, "run_workflow", fake_run_workflow)

    result = runner.invoke(workflow_cli.app, ["run", "echo", "--args", "{}"])

    assert result.exit_code == 0
    # --budget was not given, so the explicit budget arg stays None and the
    # effective budget flows from the raised cfg fallback.
    assert captured["budget_arg"] is None
    assert captured["cfg_budget"] == 1_000_000


def test_workflow_run_explicit_budget_not_raised(monkeypatch):
    """An explicit --budget below 1M is honored verbatim, not floored up."""
    @workflow(name="echo", description="echoes its args")
    async def echo(ctx, args):
        return {"ok": True}

    reg = Registry()
    reg.register(echo.__workflow_spec__)
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: reg)
    monkeypatch.setattr(workflow_cli, "missing_api_key_for", lambda *a, **k: False)

    captured: dict[str, Any] = {}

    async def fake_run_workflow(spec_or_fn, args, *, cfg, budget=None, **kwargs):
        captured["cfg_budget"] = cfg["budget"]
        captured["budget_arg"] = budget
        return await spec_or_fn.fn(object(), args)

    monkeypatch.setattr(workflow_cli, "run_workflow", fake_run_workflow)

    result = runner.invoke(
        workflow_cli.app, ["run", "echo", "--args", "{}", "--budget", "12345"]
    )

    assert result.exit_code == 0
    # Explicit budget is passed through and the cfg budget equals it (resolve_config
    # uses the given value); the 1M floor only applies when --budget is omitted.
    assert captured["budget_arg"] == 12345
    assert captured["cfg_budget"] == 12345


def test_workflow_run_unknown_name_exits_nonzero(monkeypatch):
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: _stub_registry())

    result = runner.invoke(workflow_cli.app, ["run", "nope", "--args", "{}"])

    assert result.exit_code != 0


def test_workflow_run_invalid_json_args_exits_nonzero(monkeypatch):
    monkeypatch.setattr(workflow_cli, "load_registry", lambda: _stub_registry())

    result = runner.invoke(workflow_cli.app, ["run", "alpha", "--args", "{not-json"])

    assert result.exit_code != 0


def _last_json_block(text: str) -> str:
    """Extract the trailing JSON object/array printed by the run command.

    The result is printed as a JSON block at the end of stdout; progress lines
    precede it. Find the last line that begins a JSON value.
    """
    stripped = text.strip()
    candidates = [i for i in (stripped.find("{"), stripped.find("[")) if i != -1]
    start = min(candidates)
    return stripped[start:]

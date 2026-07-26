"""Shared fakes and configuration for workflow runtime tests."""

from __future__ import annotations

from typing import Any

from opencollab.bootstrap import (
    _workflow_runtime_session as workflow_session,
)


class _InjectedEnvironment:
    def __init__(self, *, abort_fails: bool = False) -> None:
        self.revoked = False
        self.abort_fails = abort_fails
        self.abort_calls = 0

    def revoke(self) -> None:
        self.revoked = True

    async def abort(self) -> None:
        self.abort_calls += 1
        if self.abort_fails:
            raise RuntimeError("abort failed")

class _FakeSession:
    def __init__(self, agent: Any, tools: Any) -> None:
        self.agent = agent
        self.tools = tools
        self.used_tokens = 0
        self.step_count = 0
        self.markup_recovered = 0
        self.prompt: str | None = None

    async def add_user_message(self, content: str) -> None:
        self.prompt = content

    async def run_loop(self) -> str:
        return "fake-reply"

def _patch_build_session(monkeypatch) -> list[dict[str, Any]]:
    """Capture every build_session call's kwargs; return fake sessions."""
    calls: list[dict[str, Any]] = []

    def fake_build_session(*, agent, **kwargs):
        calls.append({"agent": agent, **kwargs})
        return _FakeSession(agent, agent.tools)

    monkeypatch.setattr(workflow_session, "build_session", fake_build_session)
    return calls

def _cfg(**overrides) -> dict[str, Any]:
    base = {
        "model": "test-model",
        "provider": "anthropic",
        "api_key": "resolved-key",  # pragma: allowlist secret
        "base_url": "https://example.test",
        "budget": 100_000,
    }
    base.update(overrides)
    return base

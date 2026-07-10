import asyncio

import pytest
from opencollab.adapters.tui import TuiAskUserPolicy, TuiPermissionPolicy


def run(coro):
    return asyncio.run(coro)


class FakeRender:
    def __init__(self):
        self.events = []

    def suspend_live(self) -> bool:
        self.events.append("suspend")
        return True

    def resume_live(self, was_suspended: bool) -> None:
        self.events.append(("resume", was_suspended))


def test_tui_permission_policy_suspends_resumes_and_parses_yes():
    render = FakeRender()
    answers = iter(["yes\n"])

    async def fake_read(prompt: str) -> str:
        assert prompt.endswith("[y/N] ")
        return next(answers)

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is True
    assert render.events == ["suspend", ("resume", True)]


def test_tui_permission_policy_parses_no():
    render = FakeRender()

    async def fake_read(prompt: str) -> str:
        return "n"

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is False
    assert render.events == ["suspend", ("resume", True)]


def test_tui_permission_policy_resumes_on_eof():
    render = FakeRender()

    async def fake_read(prompt: str) -> str:
        raise EOFError

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert run(policy.confirm("Allow?")) is False
    assert ("resume", True) in render.events


def test_tui_ask_user_policy_suspends_resumes_and_returns_answer():
    render = FakeRender()
    prompts = []

    async def fake_read(prompt: str) -> str:
        prompts.append(prompt)
        return "use the smaller patch\n"

    policy = TuiAskUserPolicy(render=render, read_line=fake_read)
    assert run(policy.ask("Which patch?")) == "use the smaller patch\n"
    assert "Which patch?" in prompts[0]
    assert render.events == ["suspend", ("resume", True)]


def test_tui_ask_user_policy_resumes_then_propagates_eof():
    render = FakeRender()

    async def fake_read(prompt: str) -> str:
        raise EOFError

    policy = TuiAskUserPolicy(render=render, read_line=fake_read)
    with pytest.raises(EOFError):
        run(policy.ask("Which patch?"))
    assert ("resume", True) in render.events

import asyncio

import pytest

from opencollab.adapters.tui import TuiAskUserPolicy, TuiPermissionPolicy


def run(coro):
    return asyncio.run(coro)


def test_tui_permission_policy_parses_yes():
    async def fake_read(prompt: str) -> str:
        assert prompt.endswith("[y/N] ")
        return "yes\n"

    assert run(TuiPermissionPolicy(read_line=fake_read).confirm("Allow?")) is True


def test_tui_permission_policy_parses_no():
    async def fake_read(prompt: str) -> str:
        return "n"

    assert run(TuiPermissionPolicy(read_line=fake_read).confirm("Allow?")) is False


def test_tui_permission_policy_denies_on_eof():
    """A closed input is not consent."""

    async def fake_read(prompt: str) -> str:
        raise EOFError

    assert run(TuiPermissionPolicy(read_line=fake_read).confirm("Allow?")) is False


def test_tui_ask_user_policy_returns_the_answer_to_its_question():
    prompts = []

    async def fake_read(prompt: str) -> str:
        prompts.append(prompt)
        return "use the smaller patch\n"

    policy = TuiAskUserPolicy(read_line=fake_read)
    assert run(policy.ask("Which patch?")) == "use the smaller patch\n"
    assert "Which patch?" in prompts[0]


def test_tui_ask_user_policy_propagates_eof_so_the_tool_reports_a_decline():
    async def fake_read(prompt: str) -> str:
        raise EOFError

    with pytest.raises(EOFError):
        run(TuiAskUserPolicy(read_line=fake_read).ask("Which patch?"))

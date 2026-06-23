"""Tests for the workflow mode of ``run_eval_task`` (phase 4).

Three concerns:

* When ``workflow=`` is given, ``run_eval_task`` builds a ``WorkflowContext``
  whose factory creates sessions bound to the task env / budget, runs the
  workflow with the task args, and aggregates tokens (and steps) across *all*
  sessions the workflow created. Patch extraction / timeout / EvalResult shape
  stay unchanged.
* ``workflow=None`` is the unchanged single-session path (reuses the existing
  evaluator fakes).
* ``generate_review_fix`` skips its apply stage when the review verdict says no
  changes are needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.adapters.env import Environment, ExecResult
from opencollab.harness.evaluator import EvalResult, EvalTask, run_eval_task
from opencollab.harness.workflows import generate_review_fix


def run(coro):
    return asyncio.run(coro)


class FakeEnv(Environment):
    def __init__(self, diff="diff --git a/x b/x\n+new\n"):
        self.diff = diff
        self.cleaned_up = False
        self.cmds = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


class FakeSession:
    """Duck-typed workflow session that records a fixed token count."""

    def __init__(self, *, env: Any, tokens: int, reply: str = "ok") -> None:
        self.env = env
        self.used_tokens = tokens
        self.step_count = 1
        self.reply = reply
        self.messages: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.messages.append(content)

    async def run_loop(self) -> str:
        return self.reply


# --------------------------------------------------------------------------- #
# workflow mode: invocation + aggregation
# --------------------------------------------------------------------------- #


def test_workflow_mode_invoked_with_task_args(tmp_path):
    seen: dict[str, Any] = {}
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        seen["args"] = args
        seen["ctx"] = ctx
        return "done"

    result = run(
        run_eval_task(
            EvalTask(task_id="t1", description="fix the bug"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert isinstance(result, EvalResult)
    assert result.task_id == "t1"
    assert seen["args"]["task_id"] == "t1"
    assert seen["args"]["description"] == "fix the bug"
    # Patch extraction is unchanged: the env diff still becomes the patch.
    assert result.patch == env.diff
    assert result.patch_produced is True
    assert result.error is None
    assert env.cleaned_up is True


def test_workflow_mode_aggregates_tokens_across_sessions(tmp_path):
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        # Two agent calls -> two sessions; tokens must sum across both.
        await ctx.agent("first")
        await ctx.agent("second")
        return "done"

    # Each fake session reports 7 tokens; the factory builds real sessions, so
    # we patch the factory's session builder to return token-bearing fakes.
    import opencollab.harness.evaluator as evaluator_mod

    original = evaluator_mod._build_eval_session_factory

    def patched_factory(*args, **kwargs):
        factory = original(*args, **kwargs)

        def build(
            *, prompt, budget, tools=None, isolation=False, label=None,
            tool_choice=None, thinking=None,
        ):
            return FakeSession(env=env, tokens=7)

        factory.build_workflow_session = build  # type: ignore[attr-defined]
        return factory

    evaluator_mod._build_eval_session_factory = patched_factory
    try:
        result = run(
            run_eval_task(
                EvalTask(task_id="t2", description="x"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                workflow=wf,
            )
        )
    finally:
        evaluator_mod._build_eval_session_factory = original

    assert result.tokens_used == 14
    assert result.steps == 2


# --------------------------------------------------------------------------- #
# workflow=None: unchanged single-session path
# --------------------------------------------------------------------------- #


def test_workflow_none_path_unchanged(monkeypatch, tmp_path):
    from opencollab.adapters.llm import LLMResponse, Usage
    from opencollab.bootstrap import container

    class FakeLLMClient:
        def __init__(self, *a, **k):
            pass

        async def complete(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="done",
                tool_calls=[],
                usage=Usage(input_tokens=3, output_tokens=2),
                finish_reason="stop",
            )

    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="t3", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None


# --------------------------------------------------------------------------- #
# generate_review_fix stage skipping
# --------------------------------------------------------------------------- #


class ScriptedCtx:
    """A minimal WorkflowContext stand-in scripting agent() replies."""

    def __init__(self, env: Any, replies: list[Any]) -> None:
        self.env = env
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label}
        )
        return self._replies.pop(0)

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        pass


def test_generate_review_fix_skips_apply_when_ok(tmp_path):
    env = FakeEnv()
    # Stage 1 implement -> text; stage 2 review verdict -> needs_changes False.
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": False, "feedback": "looks good"},
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Only two agent calls — the apply stage was skipped.
    assert len(ctx.agent_calls) == 2
    # The review call used a schema (structured verdict).
    assert ctx.agent_calls[1]["schema"] is not None
    assert result["needs_changes"] is False


def test_generate_review_fix_runs_apply_when_changes_requested(tmp_path):
    env = FakeEnv()
    ctx = ScriptedCtx(
        env,
        replies=[
            "implemented the fix",
            {"needs_changes": True, "feedback": "rename foo to bar"},
            "applied the feedback",
        ],
    )

    result = run(generate_review_fix(ctx, {"description": "fix the bug"}))

    # Three agent calls — implement, review, apply.
    assert len(ctx.agent_calls) == 3
    assert result["needs_changes"] is True
    # The apply-stage prompt carried the review feedback.
    assert "rename foo to bar" in ctx.agent_calls[2]["prompt"]

import asyncio
import json

from opencollab.adapters.env import Environment, ExecResult
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.harness import evaluator
from opencollab.harness.evaluator import (
    EvalResult,
    EvalTask,
    run_eval_task,
    save_results,
)


def run(coro):
    return asyncio.run(coro)


class FakeLLMClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


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


def test_run_eval_task_produces_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(run_eval_task(
        EvalTask(task_id="t1", description="fix the bug"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert isinstance(result, EvalResult)
    assert result.task_id == "t1"
    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None
    assert env.cleaned_up is True


def test_run_eval_task_empty_diff_not_produced(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    async def env_factory(task):
        return FakeEnv(diff="")

    result = run(run_eval_task(
        EvalTask(task_id="t2", description="noop"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert result.patch_produced is False
    assert result.patch == ""


def test_run_eval_task_honors_injected_params(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    captured = {}
    sentinel_tool = object()

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["prompt"] = agent.system_prompt
        captured["tools"] = list(agent.tools)
        captured["max_steps"] = max_steps
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="t3", description="task"),
        output_dir=str(tmp_path),
        prompt="CUSTOM PROMPT",
        tools_factory=lambda: [sentinel_tool],
        env_factory=env_factory,
        max_steps=7,
    ))

    assert captured["prompt"] == "CUSTOM PROMPT"
    assert captured["tools"] == [sentinel_tool]
    assert captured["max_steps"] == 7


def test_save_results_writes_patch_produced_key(tmp_path):
    results = [
        EvalResult(
            task_id="t1",
            patch="diff --git\n+x\n",
            patch_produced=True,
            tokens_used=5,
            steps=1,
            duration=0.123,
        ),
    ]
    out = tmp_path / "results.jsonl"
    save_results(results, str(out))

    record = json.loads(out.read_text().strip())
    assert record["patch_produced"] is True
    assert "success" not in record
    assert record["task_id"] == "t1"
    assert record["patch_lines"] == 2

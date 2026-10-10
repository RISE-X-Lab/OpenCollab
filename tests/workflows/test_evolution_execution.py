"""Evolution performs normal file work, verified repair and managed continuation."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import _workflow_runtime_session as wiring
from opencollab.builtin_workflows import (
    EvolutionAdapter,
    EvolutionConfig,
    EvolutionGroup,
    EvolutionState,
    run_evolution,
)
from opencollab.tools import builtin_tools
from tests.support.installed_evolution_smoke import exercise_evolution


class FileModel:
    def __init__(self, initial="wrong\n", repaired="correct\n"):
        self.initial = initial
        self.repaired = repaired
        self.calls = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        if any(message["role"] == "tool" for message in messages):
            return LLMResponse(content="file saved", usage=Usage(4, 2), finish_reason="stop")
        prompt = next(message["content"] for message in messages if message["role"] == "user")
        content = self.repaired if "repair-stage" in prompt else self.initial
        return LLMResponse(tool_calls=[{
            "id": f"write-{len(self.calls)}", "type": "function", "function": {
                "name": "file_write", "arguments": json.dumps({
                    "path": "result.txt", "mode": "create", "overwrite": True, "content": content,
                }),
            },
        }], usage=Usage(4, 2), finish_reason="tool_calls")


class FileChecks(EvolutionAdapter):
    def __init__(self, workspace):
        (workspace / "check_result.py").write_text(
            "from pathlib import Path\n"
            "with Path('checks-ran.txt').open('a') as stream:\n"
            "    stream.write('executed\\n')\n"
            "assert Path('result.txt').read_text() == 'correct\\n'\n"
            "print('RESULT_OK')\n",
            encoding="utf-8",
        )
        super().__init__(check_commands=[{
            "command": f"{shlex.quote(sys.executable)} check_result.py", "expected_output": "RESULT_OK",
        }], tools=list(builtin_tools("file_read", "file_write", headless=False)))
        self.workspace = workspace
        self.saved = []
        self.updates = []
        self.checks = []

    def repair_prompt(self, check, state):
        return "repair-stage writes the corrected result.txt"

    def source_snapshot(self):
        path = self.workspace / "result.txt"
        return {"result.txt": path.read_bytes()} if path.exists() else {}

    def save_state(self, data):
        self.saved.append(copy.deepcopy(data))

    def on_update(self, kind, state, data):
        self.updates.append((kind, copy.deepcopy(data)))

    async def verify(self, ctx, targets, seconds):
        check = await super().verify(ctx, targets, seconds)
        self.checks.append(check)
        return check


def _client(workspace):
    return OpenCollab(workspace, model="local-file-model", config={"max_output_tokens": 128})


def _config(**overrides):
    values = {"main_budget": 10_000, "max_steps": 12, "max_output_tokens": 128,
              "max_repair_rounds": 2, "repair_reserve": 2_000, "cleanup_seconds": .2}
    values.update(overrides)
    return EvolutionConfig(**values)


async def test_sdk_named_evolution_writes_shared_files_in_independent_sessions(tmp_path):
    report = await exercise_evolution(tmp_path)
    assert report["model_calls"] == 5
    assert report["tokens"] == 30
    assert report["checks"] >= 2


@pytest.mark.parametrize("commands", [
    [], [""], ["true"], ["python --help"], ["python -m pytest --collect-only"],
    [{"command": "python --help", "expected_output": "usage"}],
    [{"command": "python -m pytest --collect-only", "expected_output": "collected"}],
])
async def test_missing_or_nonexecuting_check_never_verifies_delivery(tmp_path, commands):
    result = await _client(tmp_path).workflow(
        "evolution", {
            "groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
            "check_commands": commands,
            "config": {"max_steps": 4, "max_repair_rounds": 0},
        }, llm=FileModel(initial="correct\n"), budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.ok, (result.reason, result.output)
    assert (tmp_path / "result.txt").read_text() == "correct\n"
    assert result.output["status"] == "unverified"
    assert result.output["delivery_ok"] is False
    assert result.output["verification"]["executed"] is False


async def test_successful_command_without_behavior_evidence_is_unverified(tmp_path):
    command = f"{shlex.quote(sys.executable)} -c 'print(123)'"
    result = await _client(tmp_path).workflow(
        "evolution", {
            "groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
            "check_commands": [command], "config": {"max_repair_rounds": 0},
        }, llm=FileModel(initial="correct\n"), budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.ok
    assert result.output["delivery_ok"] is False
    checks = result.output["verification"]["report"]["checks"]
    assert checks[0]["executed"] is True and checks[0]["exit_code"] == 0
    assert checks[0]["ok"] is False
    assert "123" in checks[0]["output"]


async def test_zero_collected_tests_with_expected_text_never_verifies_delivery(tmp_path):
    (tmp_path / "empty_tests").mkdir()
    command = f"{shlex.quote(sys.executable)} -m pytest -q empty_tests"
    result = await _client(tmp_path).workflow(
        "evolution", {
            "groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
            "check_commands": [{"command": command, "expected_output": "no tests ran"}],
            "config": {"max_repair_rounds": 0},
        }, llm=FileModel(initial="correct\n"), budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.ok
    assert result.output["delivery_ok"] is False
    checks = result.output["verification"]["report"]["checks"]
    assert checks[0]["executed"] is True
    assert checks[0]["exit_code"] == 5
    assert "no tests ran" in checks[0]["output"]


@pytest.mark.parametrize("unbounded", [False, True])
async def test_real_sessions_preserve_weighted_finite_and_unbounded_allowances(tmp_path, monkeypatch, unbounded):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", str(unbounded).lower())
    adapter = FileChecks(tmp_path)
    model = FileModel(initial="correct\n")
    config = _config(main_budget=None, budget=None if unbounded else 10_000)

    async def flow(ctx, _args):
        return await run_evolution(ctx, [
            EvolutionGroup("first", "first-stage", weight=1),
            EvolutionGroup("second", "second-stage", weight=3),
        ], config=config, adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="environment" if unbounded else "explicit", trace=False,
    )
    assert result.ok and result.output["delivery_ok"] is True
    assert result.tokens == 24
    groups = result.output["groups"]
    if unbounded:
        assert result.output["state"]["budget_total"] is None
        assert all(group["result"]["hard_budget_tokens"] is None for group in groups.values())
        assert all(group["result"]["soft_budget_tokens"] is None for group in groups.values())
    else:
        assert result.output["state"]["budget_total"] == 10_000
        assert groups["1"]["result"]["hard_budget_tokens"] == 2_000
        assert groups["2"]["result"]["hard_budget_tokens"] == 7_988
        assert result.output["total_tokens"] == 24


async def test_failed_executable_check_is_repaired_in_a_new_session(tmp_path, monkeypatch):
    adapter = FileChecks(tmp_path)
    model = FileModel()
    sessions = []
    build = wiring.build_session

    def capture(**kwargs):
        session = build(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(wiring, "build_session", capture)

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage writes result.txt")],
                                   config=_config(), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok, (result.reason, result.output)
    assert result.output["delivery_ok"] is True, result.output
    assert (tmp_path / "result.txt").read_text() == "correct\n"
    assert adapter.checks[0].executed and not adapter.checks[0].ok
    assert adapter.checks[-1].executed and adapter.checks[-1].ok
    assert len([kind for kind, _data in adapter.updates if kind == "repair_started"]) == 1
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert result.output["groups"]["1"]["result"]["session_id"] != result.output["repair_rounds"][0]["session_id"]
    assert len(model.calls) == 4 and result.tokens == 24
    assert "write-stage" not in str(model.calls[2])


async def test_edit_to_ancestor_rechecks_transitive_dependent_targets(tmp_path):
    (tmp_path / "check_targets.py").write_text(
        "from pathlib import Path\n"
        "assert Path('a.txt').read_text() in ('original\\n', 'changed\\n')\n"
        "for name in ('b', 'c'):\n"
        "    path = Path(name + '.txt')\n"
        "    if path.exists():\n"
        "        assert path.read_text() == name + '\\n'\n"
        "print('FILES_CHECKED')\n",
        encoding="utf-8",
    )

    class TargetChecks(EvolutionAdapter):
        def __init__(self):
            super().__init__(check_commands=[{
                "command": f"{shlex.quote(sys.executable)} check_targets.py", "expected_output": "FILES_CHECKED",
            }], tools=list(builtin_tools("file_write", headless=False)))
            self.targets = []

        def source_snapshot(self):
            return {path.name: path.read_bytes() for path in tmp_path.glob("*.txt")}

        async def verify(self, ctx, targets, seconds):
            self.targets.append(targets)
            return await super().verify(ctx, targets, seconds)

    class TargetModel(FileModel):
        async def complete(self, messages, tools=None, **kwargs):
            self.calls.append(copy.deepcopy(messages))
            if any(message["role"] == "tool" for message in messages):
                return LLMResponse(content="saved", usage=Usage(4, 2), finish_reason="stop")
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            stage = prompt.splitlines()[0]
            path, content = {
                "stage-A": ("a.txt", "original\n"), "stage-B": ("b.txt", "b\n"),
                "stage-C": ("c.txt", "c\n"), "stage-D": ("a.txt", "changed\n"),
            }[stage]
            return LLMResponse(tool_calls=[{
                "id": f"write-{len(self.calls)}", "type": "function", "function": {
                    "name": "file_write", "arguments": json.dumps({
                        "path": path, "mode": "create", "overwrite": True, "content": content,
                    }),
                },
            }], usage=Usage(4, 2), finish_reason="tool_calls")

    adapter, model = TargetChecks(), TargetModel()

    async def flow(ctx, _args):
        return await run_evolution(ctx, [
            EvolutionGroup("A", "stage-A", targets=("a",), resources=("a.txt",)),
            EvolutionGroup("B", "stage-B", targets=("b",), resources=("b.txt",), dependencies=("A",)),
            EvolutionGroup("C", "stage-C", targets=("c",), resources=("c.txt",), dependencies=("B",)),
            EvolutionGroup("D", "stage-D", targets=("d",), resources=("a.txt",)),
        ], config=_config(max_steps=16), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=30_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok and result.output["delivery_ok"] is True, result.output
    assert adapter.targets[3] == ("a", "b", "c", "d")
    assert adapter.targets[-1] is None
    assert (tmp_path / "a.txt").read_text() == "changed\n"
    assert len(result.output["groups"]) == 4
    assert result.tokens == len(model.calls) * 6 > 0


@pytest.mark.parametrize("failure_hook", ["save_state", "on_update"])
async def test_state_and_update_callback_failures_retain_execution_failure(tmp_path, failure_hook):
    class BrokenPersistence(FileChecks):
        def save_state(self, data):
            if failure_hook == "save_state":
                raise OSError("controlled persistence failure")
            super().save_state(data)

        def on_update(self, kind, state, data):
            if failure_hook == "on_update":
                raise RuntimeError("controlled update failure")
            super().on_update(kind, state, data)

    adapter = BrokenPersistence(tmp_path)
    model = FileModel(initial="correct\n")

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(), adapter=adapter)

    result = await _client(tmp_path).workflow(flow, llm=model, budget=20_000, trace=False)
    assert result.status == "failed", (result.status, result.output)
    assert result.error is not None
    assert "controlled" in str(result.error)
    assert not (tmp_path / "result.txt").exists()
    assert not adapter.checks


@pytest.mark.parametrize("failure_hook", ["on_event", "group_generated"])
async def test_observation_failure_after_real_generation_prevents_verified_handoff(tmp_path, failure_hook):
    class BrokenObservation(FileChecks):
        def on_event(self, phase, event):
            if failure_hook == "on_event" and event.type == "usage":
                raise OSError("controlled event observer failure")

        def on_update(self, kind, state, data):
            if failure_hook == "group_generated" and kind == "group_generated":
                raise OSError("controlled generated observer failure")
            super().on_update(kind, state, data)

    adapter = BrokenObservation(tmp_path)
    model = FileModel(initial="correct\n")

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.status == "failed", (result.reason, result.output)
    assert (tmp_path / "result.txt").read_text() == "correct\n"
    assert len(model.calls) == 2
    assert not adapter.checks
    assert adapter.saved[-1]["status"] == "failed"
    assert adapter.saved[-1]["delivery_ok"] is False
    assert EvolutionState(adapter.saved[-1]).tokens == 12


async def test_cancelled_generated_group_resumes_at_executed_check(tmp_path):
    checking, settled = asyncio.Event(), asyncio.Event()

    class InterruptedCheck(FileChecks):
        async def verify(self, ctx, targets, seconds):
            checking.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()

    interrupted = InterruptedCheck(tmp_path)
    model = FileModel(initial="correct\n")

    async def first(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(), adapter=interrupted)

    running = asyncio.create_task(_client(tmp_path).workflow(
        first, llm=model, budget=20_000, limit_mode="explicit", run_id="resume-file-task", trace=False,
    ))
    await asyncio.wait_for(checking.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert settled.is_set()
    assert (tmp_path / "result.txt").read_text() == "correct\n"
    assert len(model.calls) == 2
    state = EvolutionState(interrupted.saved[-1])
    original_tokens = state.tokens
    resumed = FileChecks(tmp_path)

    async def second(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(), adapter=resumed, state=state)

    result = await _client(tmp_path).workflow(
        second, llm=model, budget=20_000, limit_mode="explicit", run_id="resume-file-task", trace=False,
    )
    assert result.ok and result.output["delivery_ok"] is True, (result.reason, result.output)
    assert len(model.calls) == 2
    assert result.tokens == 0
    assert state.tokens == original_tokens == 12
    assert resumed.checks and all(check.executed and check.ok for check in resumed.checks)


async def test_cancelled_writer_settles_before_return_without_later_group_or_check(tmp_path):
    writing, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class LateWriter:
        name, description = "late_write", "Write a file during cancellation cleanup"
        parameters = {"type": "object", "properties": {}}

        def to_openai_schema(self):
            return {"type": "function", "function": {
                "name": self.name, "description": self.description, "parameters": self.parameters,
            }}

        async def execute_with_runtime(self, params, runtime):
            writing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                (tmp_path / "result.txt").write_text("late but settled\n")
                return "settled"

    class LateModel(FileModel):
        async def complete(self, messages, tools=None, **kwargs):
            self.calls.append(copy.deepcopy(messages))
            return LLMResponse(tool_calls=[{
                "id": "late", "type": "function", "function": {"name": "late_write", "arguments": "{}"},
            }], usage=Usage(4, 2), finish_reason="tool_calls")

    adapter = FileChecks(tmp_path)
    adapter.tools = lambda _phase: [LateWriter()]
    model = LateModel()

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("first", "write-stage"),
                                        EvolutionGroup("later", "later-stage")],
                                   config=_config(cleanup_seconds=1), adapter=adapter)

    running = asyncio.create_task(_client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="explicit", trace=False, cleanup_timeout=1,
    ))
    try:
        await asyncio.wait_for(writing.wait(), 3)
        running.cancel()
        await asyncio.wait_for(cancelled.wait(), 3)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert (tmp_path / "result.txt").read_text() == "late but settled\n"
        assert len(model.calls) == 1
        assert not adapter.checks
        assert len([kind for kind, _data in adapter.updates if kind == "group_started"]) == 1
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

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
    plan_evolution_groups,
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
    values = {"main_budget": 10_000, "max_steps": 12, "output_reserve_tokens": 128,
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
    model = FileModel(initial="correct\n")
    result = await _client(tmp_path).workflow(
        "evolution", {
            "groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
            "check_commands": [command],
        }, llm=model, budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.ok
    assert result.output["delivery_ok"] is False
    checks = result.output["verification"]["report"]["checks"]
    assert checks[0]["executed"] is True and checks[0]["exit_code"] == 0
    assert checks[0]["ok"] is False
    assert "123" in checks[0]["output"]
    assert not result.output["repair_rounds"]
    assert len(model.calls) == 2


async def test_zero_collected_tests_with_expected_text_never_verifies_delivery(tmp_path):
    (tmp_path / "empty_tests").mkdir()
    command = f"{shlex.quote(sys.executable)} -m pytest -q empty_tests"
    model = FileModel(initial="correct\n")
    result = await _client(tmp_path).workflow(
        "evolution", {
            "groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
            "check_commands": [{"command": command, "expected_output": "no tests ran"}],
        }, llm=model, budget=20_000, limit_mode="explicit", trace=False,
    )
    assert result.ok
    assert result.output["delivery_ok"] is False
    checks = result.output["verification"]["report"]["checks"]
    assert checks[0]["executed"] is True
    assert checks[0]["exit_code"] == 5
    assert "no tests ran" in checks[0]["output"]
    assert not result.output["repair_rounds"]
    assert len(model.calls) == 2


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


@pytest.mark.parametrize("changed", [False, True])
async def test_completed_native_edit_extends_soft_allowance_within_the_same_session(tmp_path, changed):
    if not changed:
        (tmp_path / "result.txt").write_text("correct\n")

    class ProgressChecks(EvolutionAdapter):
        def __init__(self):
            super().__init__(check_commands=FileChecks(tmp_path).check_commands)
            self.extensions = []
            self.session_ids = set()

        def on_update(self, kind, state, data):
            if kind == "budget_extended":
                self.extensions.append(copy.deepcopy(data))

        def on_event(self, phase, event):
            if event.session_id:
                self.session_ids.add(event.session_id)

    class ExpensiveFileModel(FileModel):
        async def complete(self, messages, tools=None, **kwargs):
            response = await super().complete(messages, tools, **kwargs)
            response.usage = Usage(8_000, 500)
            return response

    adapter, model = ProgressChecks(), ExpensiveFileModel(initial="correct\n")

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(main_budget=10_000, extension_tokens=8_000), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok and result.output["delivery_ok"] is True, (result.reason, result.output)
    assert (tmp_path / "result.txt").read_text() == "correct\n"
    assert len(model.calls) == (2 if changed else 1)
    assert result.tokens == (17_000 if changed else 8_500)
    assert len(adapter.session_ids) == 1, (result.output, adapter.extensions)
    assert not result.output["repair_rounds"]
    if changed:
        assert adapter.extensions and all(data["same_session"] is True for data in adapter.extensions)
        assert all(data["old_cap"] < data["new_cap"] <= data["hard_cap"] for data in adapter.extensions)
        assert result.output["groups"]["1"]["result"]["soft_budget_tokens"] > 10_000
    else:
        assert not adapter.extensions
        assert result.output["groups"]["1"]["result"]["soft_budget_tokens"] == 10_000
        assert result.output["groups"]["1"]["result"]["status"] == "stopped"


async def test_executed_failures_stop_finite_repairs_after_measured_stagnation(tmp_path):
    adapter, model = FileChecks(tmp_path), FileModel(repaired="wrong\n")

    async def flow(ctx, _args):
        return await run_evolution(ctx, [EvolutionGroup("result", "write-stage")],
                                   config=_config(max_repair_rounds=5, stagnant_round_limit=2), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=20_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok
    assert result.output["status"] == "failed" and result.output["delivery_ok"] is False
    assert result.output["repair_stop_reason"] == "rounds_without_observable_progress"
    rounds = result.output["repair_rounds"]
    assert len(rounds) == 2 and rounds[-1]["stagnant_rounds"] == 2
    assert len({row["session_id"] for row in rounds}) == 2
    assert len(model.calls) == 6 and result.tokens == 36
    assert len(adapter.checks) == 4 and all(check.executed and not check.ok for check in adapter.checks)
    assert (tmp_path / "checks-ran.txt").read_text().splitlines() == ["executed"] * 4
    assert (tmp_path / "result.txt").read_text() == "wrong\n"


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


async def test_planned_dependency_cycle_executes_one_shared_session_then_its_dependent(tmp_path):
    (tmp_path / "check_cycle.py").write_text(
        "from pathlib import Path\n"
        "assert Path('a.txt').read_text() == 'a\\n'\n"
        "assert Path('b.txt').read_text() == 'b\\n'\n"
        "if Path('c.txt').exists():\n"
        "    assert Path('c.txt').read_text() == 'a+b\\n'\n"
        "with Path('checks-ran.txt').open('a') as stream:\n"
        "    stream.write('executed\\n')\n"
        "print('CYCLE_FILES_OK')\n",
        encoding="utf-8",
    )

    class CycleChecks(EvolutionAdapter):
        def __init__(self):
            super().__init__(check_commands=[{
                "command": f"{shlex.quote(sys.executable)} check_cycle.py", "expected_output": "CYCLE_FILES_OK",
            }], tools=list(builtin_tools("file_read", "file_write", headless=False)))
            self.targets, self.prompts = [], []

        def group_prompt(self, group, state):
            prompt = super().group_prompt(group, state)
            self.prompts.append(prompt)
            return prompt

        def source_snapshot(self):
            return {path.name: path.read_bytes() for path in tmp_path.glob("[abc].txt")}

        async def verify(self, ctx, targets, seconds):
            self.targets.append(targets)
            return await super().verify(ctx, targets, seconds)

    class CycleModel(FileModel):
        async def complete(self, messages, tools=None, **kwargs):
            self.calls.append(copy.deepcopy(messages))
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            results = [message for message in messages if message["role"] == "tool"]
            if "cycle-A" in prompt:
                assert "cycle-B" in prompt
                calls = [] if results else [
                    ("file_write", {"path": "a.txt", "mode": "create", "content": "a\n"}),
                    ("file_write", {"path": "b.txt", "mode": "create", "content": "b\n"}),
                ]
            elif not results:
                calls = [("file_read", {"path": "a.txt"}), ("file_read", {"path": "b.txt"})]
            elif len(results) == 2:
                assert "a" in str(results[0]["content"]) and "b" in str(results[1]["content"])
                calls = [("file_write", {"path": "c.txt", "mode": "create", "content": "a+b\n"})]
            else:
                calls = []
            if not calls:
                return LLMResponse(content="files saved", usage=Usage(4, 2), finish_reason="stop")
            return LLMResponse(tool_calls=[{
                "id": f"call-{len(self.calls)}-{number}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                },
            } for number, (name, arguments) in enumerate(calls)], usage=Usage(4, 2), finish_reason="tool_calls")

    groups = [
        EvolutionGroup("A", "cycle-A creates a.txt", targets=("target-a",), dependencies=("B",)),
        EvolutionGroup("B", "cycle-B creates b.txt", targets=("target-b",), dependencies=("A",)),
        EvolutionGroup("C", "cycle-C reads both files and creates c.txt", targets=("target-c",), dependencies=("B",)),
    ]
    planned = plan_evolution_groups(groups)
    assert len(planned) == 2
    assert planned[0].targets == ("target-a", "target-b")
    assert planned[1].dependencies == ("target-b",)
    adapter, model = CycleChecks(), CycleModel()

    async def flow(ctx, _args):
        return await run_evolution(ctx, planned, config=_config(), adapter=adapter)

    result = await _client(tmp_path).workflow(
        flow, llm=model, budget=30_000, limit_mode="explicit", agent_profile="single2", trace=False,
    )
    assert result.ok and result.output["delivery_ok"] is True, (result.reason, result.output)
    assert len(result.output["groups"]) == 2
    assert len({row["result"]["session_id"] for row in result.output["groups"].values()}) == 2
    assert "cycle-A" in adapter.prompts[0] and "cycle-B" in adapter.prompts[0]
    assert "cycle-C" in adapter.prompts[1]
    assert adapter.targets[:2] == [("target-a", "target-b"), ("target-a", "target-b", "target-c")]
    assert (tmp_path / "c.txt").read_text() == "a+b\n"
    assert (tmp_path / "checks-ran.txt").read_text().splitlines() == ["executed"] * 3
    assert len(model.calls) == 5 and result.tokens == 30


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

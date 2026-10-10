"""Real OC sessions and platform records with local generation/check substitutes."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml
from arc_light import delivery
from arc_light.completion import scenario_coverage
from arc_light.reports import write_json
from arcbench_r6.prompts import SYSTEM_PROMPT
from arcbench_r6.settings import Settings

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage

flow_module = importlib.import_module("arcbench_r6.workflow")
runner_module = importlib.import_module("arcbench_r6.runner")


def application(root):
    for name in ("frontend", "backend"):
        directory = root / name
        (directory / "src").mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"name": "fixture-" + name}))
    with sqlite3.connect(root / "backend/app.sqlite") as database:
        database.execute("CREATE TABLE original(id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO original VALUES(1, 'preserve')")
    document = {
        "name": "Local fixture",
        "requirements": [
            {
                "id": key,
                "name": name,
                "description": "New feature",
                "scenarios": [
                    {
                        "steps": [
                            {"keyword": "GIVEN", "content": "an existing application"},
                            {"keyword": "WHEN", "content": "the user invokes this action"},
                            {"keyword": "THEN", "content": "the application preserves the result"},
                        ]
                    }
                ],
            }
            for key, name in (("REQ-A", "Manage Active Browser Sessions"), ("REQ-B", "View Organization Audit Log"))
        ],
    }
    path = root / "task.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def checks(monkeypatch, *, repair=False):
    monkeypatch.setattr(flow_module, "preflight", lambda *a, **kw: {"ok": True, "fatal": False, "checks": []})

    def verify(workspace, *, scope="all", requirement_ids=(), output_dir=None, **kwargs):
        output = Path(output_dir) if output_dir is not None else workspace / ".arc/checks"
        contract = json.loads((workspace / ".arc/checks/public-prerequisites.json").read_text())
        expected = contract["required_scenarios"]
        if scope == "requirements":
            expected = [row for row in expected if row["requirement_id"] in requirement_ids]
        good = not repair or (workspace / "backend/src/feature.js").read_text() == "fixed"
        rows = [
            {
                "name": f"evolution:{row['name']}:scenario_{row['scenario_index'] + 1}",
                "requirement_id": row["requirement_id"],
                "scenario_index": row["scenario_index"],
                "scenario_id": row["scenario_id"],
                "source_ids": row["source_ids"],
                "status": "passed" if good else "failed",
                "ok": good,
                "detail": "controlled behavior check",
            }
            for row in expected
        ]
        report = {
            "ok": good,
            "scope": scope,
            "requirement_ids": list(requirement_ids),
            "checks": [{"step": "browser_smoke", "ok": good}],
            "browser": {"prerequisites": rows},
            "scenario_coverage": scenario_coverage(expected, rows),
        }
        write_json(output / "verification.json", report)
        return report

    monkeypatch.setattr(delivery, "verify", verify)
    monkeypatch.setattr(runner_module, "verify", verify)


class LocalModel:
    def __init__(self, *, repair=False):
        self.calls = []
        self.closed = 0
        self.repair = repair
        self.repaired = False

    async def complete(self, messages, tools=None, **options):
        self.calls.append(copy.deepcopy(messages))
        prompt = next((row["content"] for row in messages if row["role"] == "user"), "")
        repair_now = "Repair the next dependency frontier" in prompt and not self.repaired
        if len(self.calls) == 1 or repair_now:
            self.repaired |= repair_now
            return LLMResponse(
                tool_calls=[
                    {
                        "id": f"edit-{len(self.calls)}",
                        "type": "function",
                        "function": {
                            "name": "file_write",
                            "arguments": json.dumps(
                                {
                                    "path": "backend/src/feature.js",
                                    "mode": "create",
                                    "overwrite": repair_now,
                                    "content": "fixed" if repair_now else "initial",
                                }
                            ),
                        },
                    }
                ],
                usage=Usage(4, 2),
                finish_reason="tool_calls",
            )
        return LLMResponse(content="Unfinished: none", usage=Usage(4, 2), finish_reason="stop")

    def close(self):
        self.closed += 1


async def execute(root, task, model, **inputs):
    client = OpenCollab(root, model="test-model", config={"max_output_tokens": 128})
    return await client.workflow(
        flow_module.evolution,
        {"requirements": str(task), "settings": asdict(Settings()), **inputs},
        agent_profile="single2",
        budget=16_000_000,
        concurrency=1,
        limit_mode="explicit",
        llm=model,
        run_id="fixture-run",
        trace=False,
    )


@pytest.mark.asyncio
async def test_two_instances_share_files_but_not_history_and_resume_without_generation(tmp_path, monkeypatch):
    task = application(tmp_path)
    inherited = tmp_path / "requirements/by-id/REQ-A.yaml"
    inherited.parent.mkdir(parents=True)
    inherited.write_text("Original inherited application requirement")
    checks(monkeypatch)
    model = LocalModel()
    result = await execute(tmp_path, task, model)
    assert result.ok and result.output["delivery_ok"], result
    assert result.tokens == result.output["total_tokens"] == 18
    assert len(model.calls) == 3
    assert model.calls[0][0]["content"] == SYSTEM_PROMPT
    assert model.calls[2][0]["content"] == SYSTEM_PROMPT
    for key in ("REQ-A", "REQ-B"):
        card = tmp_path / ".arc/evolution-spec/by-id" / f"{key}.yaml"
        assert yaml.safe_load(card.read_text())["id"] == key
    assert inherited.read_text() == "Original inherited application requirement"
    assert not any(row.get("role") == "tool" for row in model.calls[2])
    assert (tmp_path / "backend/src/feature.js").read_text() == "initial"
    state = json.loads((tmp_path / ".arc/checks/run-state.json").read_text())
    assert len(state["sessions"]) == 2
    original = (tmp_path / ".arc/checks/evolution-baseline.json").read_bytes()
    resumed = await execute(tmp_path, task, model, resume=True)
    assert resumed.ok and resumed.output["delivery_ok"]
    assert resumed.tokens == 0 and resumed.output["total_tokens"] == 18
    assert len(model.calls) == 3
    assert (tmp_path / ".arc/checks/evolution-baseline.json").read_bytes() == original
    assert model.closed == 0


@pytest.mark.asyncio
async def test_repair_is_a_new_instance_with_real_failure_feedback(tmp_path, monkeypatch):
    task = application(tmp_path)
    checks(monkeypatch, repair=True)
    model = LocalModel(repair=True)
    result = await execute(tmp_path, task, model)
    assert result.ok and result.output["delivery_ok"], result
    assert (tmp_path / "backend/src/feature.js").read_text() == "fixed"
    state = json.loads((tmp_path / ".arc/checks/run-state.json").read_text())
    assert len(state["sessions"]) == 3
    assert len(state["repair_rounds"]) == 1
    assert state["repair_rounds"][0]["observable_progress"]
    assert result.tokens == result.output["total_tokens"] == 30


@pytest.mark.asyncio
async def test_old_success_is_replaced_when_new_inputs_fail(tmp_path):
    output = tmp_path / ".arc/checks/outcome.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"delivery_ok":true,"run_id":"old"}')
    model = LocalModel()
    result = await execute(tmp_path, tmp_path / "missing.yaml", model)
    assert result.ok  # The workflow returned a task failure report.
    assert result.output["status"] == "failed"
    current = json.loads(output.read_text())
    assert current["run_id"] == "fixture-run" and not current["delivery_ok"]
    assert current["error_type"] == "FileNotFoundError"
    assert not model.calls
    assert list((tmp_path / ".arc/history").glob("*/outcome.json"))


@pytest.mark.asyncio
async def test_preparation_cancellation_waits_for_the_writer(tmp_path, monkeypatch):
    task = application(tmp_path)
    started = threading.Event()
    cleaned = threading.Event()

    def prepare(workspace, *, cancel_event, **kwargs):
        started.set()
        assert cancel_event.wait(5)
        (workspace / "preflight-cleaned.txt").write_text("cleaned")
        cleaned.set()
        return {"fatal": True, "cancelled": True, "checks": []}

    monkeypatch.setattr(flow_module, "preflight", prepare)
    owner = asyncio.create_task(execute(tmp_path, task, LocalModel()))
    assert await asyncio.to_thread(started.wait, 5)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cleaned.is_set()
    assert json.loads((tmp_path / ".arc/checks/outcome.json").read_text())["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_request_without_usage_retains_its_step_on_resume(tmp_path, monkeypatch):
    task = application(tmp_path)
    checks(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    class InterruptedModel:
        async def complete(self, *args, **kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("Controlled provider ended without usage")

    owner = asyncio.create_task(execute(tmp_path, task, InterruptedModel()))
    await asyncio.wait_for(started.wait(), 10)
    owner.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    state = json.loads((tmp_path / ".arc/checks/run-state.json").read_text())
    assert state["status"] == "cancelled"
    assert sum(row["steps"] for row in state["sessions"].values()) == 1
    assert sum(row["tokens"] for row in state["sessions"].values()) == 0
    model = LocalModel()
    resumed = await execute(tmp_path, task, model, resume=True)
    assert resumed.ok and resumed.output["delivery_ok"], resumed
    assert resumed.output["main_steps"] == 1 + len(model.calls)


def test_cli_configuration_failure_cannot_reuse_old_success(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("OPENCOLLAB_MODEL", raising=False)
    output = tmp_path / ".arc/checks/outcome.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"delivery_ok":true,"run_id":"old"}')
    entry = Path(__file__).resolve().parents[1] / "main.py"
    process = subprocess.run(
        [sys.executable, str(entry), "missing.yaml", "--output-dir", str(tmp_path)], capture_output=True, text=True
    )
    assert process.returncode == 1
    assert not json.loads(output.read_text())["delivery_ok"]


@pytest.mark.parametrize("kwargs", [{"budget": True}, {"max_steps": 0}, {"wall_seconds": float("inf")}])
def test_invalid_run_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)

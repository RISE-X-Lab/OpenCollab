"""Phase 1 — structural toolset whitelist for analyst-solve (enforcement-gated).

Off (the default) is reference byte-for-byte: ``_planner_tools`` / ``_coder_tools``
return the exact current lists, and the plan/coder call sites carry no enforced
prompt suffix. On (``needs-enforcement``) swaps to the read-only planner toolset
and the str_replace-only coder toolset (bash dropped, file_write create disabled),
and appends the enforced prompt suffixes — while keeping the forced-write
``tool_choice="required"`` contract intact.

Covers spec 1A (FileWriteTool allow_create) + 1B (builders) + 1C/1D (call-site
threading + suffixes) + the off==reference parity guarantee.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.workflow_runtime import discover_workflows

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"

OFF = "off"
ON = "needs-enforcement"
F2P = ["tests/test_widget.py::test_empty"]


# --------------------------------------------------------------------------- #
# Access the exact module the workflow runs in (its globals carry the helpers).
# --------------------------------------------------------------------------- #
def _wf_fn():
    return discover_workflows(str(_WF_DIR)).get("analyst-solve").fn


def _g(name: str):
    return _wf_fn().__globals__[name]


def _names(tools) -> list[str]:
    return [type(t).__name__ for t in tools]


# --------------------------------------------------------------------------- #
# 1B — enforcement-aware builders
# --------------------------------------------------------------------------- #
def test_planner_tools_off_is_reference():
    planner, read = _g("_planner_tools"), _g("_read_tools")
    # off == reference: same tool types in the same order as _read_tools().
    assert _names(planner(OFF)) == _names(read())
    assert _names(planner()) == _names(read())  # default arg is off


def test_planner_tools_on_is_read_only_no_bash():
    planner = _g("_planner_tools")
    names = _names(planner(ON))
    assert "BashTool" not in names
    assert names == ["FileReadTool", "GrepTool"]


def test_coder_tools_off_is_exact_reference_list_and_order():
    coder = _g("_coder_tools")
    assert _names(coder(OFF)) == [
        "BashTool",
        "FileReadTool",
        "FileWriteTool",
        "ApplyPatchTool",
        "RunTestsTool",
        "GrepTool",
    ]
    assert _names(coder()) == _names(coder(OFF))  # default arg is off
    # off file_write keeps reference create behavior.
    off_fw = [t for t in coder(OFF) if type(t).__name__ == "FileWriteTool"][0]
    assert off_fw.allow_create is True


def test_coder_tools_on_drops_bash_and_disables_create():
    coder = _g("_coder_tools")
    tools = coder(ON)
    names = _names(tools)
    assert "BashTool" not in names
    # Keeps the sanctioned edit/test/read path.
    for required in ("FileReadTool", "GrepTool", "FileWriteTool", "ApplyPatchTool", "RunTestsTool"):
        assert required in names, required
    on_fw = [t for t in tools if type(t).__name__ == "FileWriteTool"][0]
    assert on_fw.allow_create is False


# --------------------------------------------------------------------------- #
# 1A — FileWriteTool allow_create behavior
# --------------------------------------------------------------------------- #
def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


def test_allow_create_false_rejects_create_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = FileWriteTool(allow_create=False)

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "new.py", "mode": "create", "content": "x = 1\n"},
            _runtime(ws),
        )
    )

    assert result.startswith("Error:")
    assert "file creation disabled" in result
    # Nothing was written.
    assert not (ws / "new.py").exists()


def test_allow_create_false_still_allows_str_replace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")
    tool = FileWriteTool(allow_create=False)

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "f.py", "mode": "str_replace", "old_str": "world", "new_str": "there"},
            _runtime(ws),
        )
    )

    assert "content changed" in result
    assert target.read_text(encoding="utf-8") == "hello there\n"


def test_allow_create_true_default_creates_file(tmp_path):
    # Reference behavior unchanged: the default tool still creates files.
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = FileWriteTool()  # allow_create defaults True

    result = asyncio.run(
        tool.execute_with_runtime(
            {"path": "new.py", "mode": "create", "content": "x = 1\n"},
            _runtime(ws),
        )
    )

    assert "Created/wrote" in result
    assert (ws / "new.py").read_text(encoding="utf-8") == "x = 1\n"


# --------------------------------------------------------------------------- #
# Scripted workflow harness (capture tools=/prompt per call site)
# --------------------------------------------------------------------------- #
class _FakeBudget:
    total = None

    def remaining(self) -> float:
        return float("inf")

    def spent(self) -> int:
        return 0


class ScriptedCtx:
    def __init__(self, replies: list[Any], *, tree: bool | None = True, time_low: bool = False) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()
        self._tree = tree
        self._time_low = time_low

    async def agent(self, prompt, *, schema=None, label=None, tools=None, **kw):
        self.agent_calls.append(
            {"prompt": prompt, "label": label, "schema": schema, "tools": tools, **kw}
        )
        return self._replies.pop(0) if self._replies else None

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def phase(self, title):
        pass

    async def log(self, message):
        self.logs.append(message)

    async def tree_changed(self):
        return self._tree

    async def source_changed(self, exclude_paths=()) -> bool | None:
        return self._tree

    def time_low(self) -> bool:
        return self._time_low

    def seconds_left(self) -> float:
        return 90.0 if self._time_low else float("inf")


DIMS = {"dimensions": [{"aspect": "bug", "question": "where?", "hints": []}]}
PLAN = {
    "root_cause": "rc",
    "approach": "ap",
    "phases": [{"goal": "g", "files": ["f.py"], "done": "behaves"}],
}


def _pass(*, tests_run=F2P, failed_count=0) -> dict[str, Any]:
    return {"verdict": "PASS", "findings": "", "tests_run": list(tests_run), "failed_count": failed_count}


def _clean_pass_replies() -> list[Any]:
    # scope -> scout -> plan -> coder -> phase PASS -> final verify PASS
    return [DIMS, "scout", PLAN, "coded", _pass(), _pass()]


def _call(ctx, label_prefix: str) -> dict[str, Any]:
    for c in ctx.agent_calls:
        if (c["label"] or "").startswith(label_prefix):
            return c
    raise AssertionError(f"no agent call with label prefix {label_prefix!r}")


def _run(args):
    ctx = ScriptedCtx(_clean_pass_replies())
    asyncio.run(_wf_fn()(ctx, args))
    return ctx


# --------------------------------------------------------------------------- #
# Parity (off == reference) at the live call sites
# --------------------------------------------------------------------------- #
def test_off_plan_and_coder_call_sites_use_reference_tools():
    ctx = _run({"description": "fix the widget", "fail_to_pass": F2P})  # enforcement defaults off
    read, coder = _g("_read_tools"), _g("_coder_tools")

    assert _names(_call(ctx, "analyst:plan")["tools"]) == _names(read())
    assert _names(_call(ctx, "coder:p0")["tools"]) == _names(coder(OFF))


def test_off_call_sites_have_no_enforced_suffix():
    ctx = _run({"description": "fix the widget", "fail_to_pass": F2P})
    plan_prompt = _call(ctx, "analyst:plan")["prompt"]
    coder_prompt = _call(ctx, "coder:p0")["prompt"]
    assert "You have ONLY file_read + grep" not in plan_prompt
    assert "You have NO shell/bash" not in coder_prompt


def test_off_scope_and_tester_untouched():
    ctx = _run({"description": "fix the widget", "fail_to_pass": F2P})
    read = _g("_read_tools")
    # scope is explicitly NOT threaded — stays on _read_tools().
    assert _names(_call(ctx, "analyst:scope")["tools"]) == _names(read())
    # tester keeps its own toolset (includes RunTests, no file_write).
    tester_names = _names(_call(ctx, "tester:p0")["tools"])
    assert "RunTestsTool" in tester_names
    assert "FileWriteTool" not in tester_names


# --------------------------------------------------------------------------- #
# On-mode restrictions at the live call sites
# --------------------------------------------------------------------------- #
def test_on_plan_call_site_is_read_only_with_suffix():
    ctx = ScriptedCtx(_clean_pass_replies())
    asyncio.run(_wf_fn()(ctx, {"description": "fix the widget", "fail_to_pass": F2P, "enforcement_strength": ON}))
    plan = _call(ctx, "analyst:plan")
    assert _names(plan["tools"]) == ["FileReadTool", "GrepTool"]
    assert "You have ONLY file_read + grep" in plan["prompt"]


def test_on_coder_call_site_drops_bash_disables_create_with_suffix():
    ctx = ScriptedCtx(_clean_pass_replies())
    asyncio.run(_wf_fn()(ctx, {"description": "fix the widget", "fail_to_pass": F2P, "enforcement_strength": ON}))
    coder = _call(ctx, "coder:p0")
    names = _names(coder["tools"])
    assert "BashTool" not in names
    fw = [t for t in coder["tools"] if type(t).__name__ == "FileWriteTool"][0]
    assert fw.allow_create is False
    assert "You have NO shell/bash and CANNOT create new files" in coder["prompt"]


# --------------------------------------------------------------------------- #
# Forced write keeps tool_choice="required" under both modes
# --------------------------------------------------------------------------- #
def _forced_call(ctx) -> dict[str, Any]:
    return _call(ctx, "coder:forced-write")


def test_forced_write_required_holds_off():
    # time_low -> bail to forced write on the first phase round.
    ctx = ScriptedCtx([DIMS, "scout", PLAN, "forced patch"], tree=True, time_low=True)
    asyncio.run(_wf_fn()(ctx, {"description": "fix the widget"}))  # off
    forced = _forced_call(ctx)
    assert forced["tool_choice"] == "required"
    assert _names(forced["tools"]) == _names(_g("_coder_tools")(OFF))


def test_forced_write_required_holds_on_with_restricted_tools():
    ctx = ScriptedCtx([DIMS, "scout", PLAN, "forced patch"], tree=True, time_low=True)
    asyncio.run(_wf_fn()(ctx, {"description": "fix the widget", "enforcement_strength": ON}))
    forced = _forced_call(ctx)
    # The required contract is preserved even with the restricted toolset.
    assert forced["tool_choice"] == "required"
    names = _names(forced["tools"])
    assert "BashTool" not in names
    fw = [t for t in forced["tools"] if type(t).__name__ == "FileWriteTool"][0]
    assert fw.allow_create is False
    assert "You have NO shell/bash and CANNOT create new files" in forced["prompt"]

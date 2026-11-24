"""Native tool observations retain actual execution and file-write outcomes."""
from __future__ import annotations

import asyncio
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest

from opencollab.adapters import _env_local
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.evidence.bash_evidence import BashEvidence
from opencollab.adapters.tools.evidence.command_evidence import parse_test_command
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.domain.tool_facts import ToolFactsCollector
from opencollab.tools import builtin_tools, evidence_tools
from tests.support.tool_runtime_test_support import FakeRemoteEnv


def test_closed_observations_keep_only_paths_that_actually_changed():
    observations = ToolFactsCollector()
    observations.record_write(completed=True, changed=True, path="changed.py")
    observations.record_write(completed=True, changed=False, path="unchanged.py")
    facts = observations.close()
    observations.record_write(completed=True, changed=True, path="late.py")
    assert facts.content_changed is True
    assert facts.changed_paths == ("changed.py",)
    assert observations.close() == facts

def runtime(env, observations):
    return SimpleNamespace(environment=env, safety_policy=None, confirm_fn=lambda: None, observations=observations)

@pytest.mark.parametrize(("command", "timeout", "code", "timed_out"), [
    ("cp missing-source target", 2, 1, False),
    ("printf changed > partial; false", 2, 1, False),
    ("exit 124", 2, 124, False),
    ("printf 'Command timed out after 1s'", 2, 0, False),
    ("printf changed > partial; sleep 2", 0.05, -1, True),
])
async def test_bash_records_real_exit_and_timeout(tmp_path, command, timeout, code, timed_out):
    env = LocalEnvironment(str(tmp_path))
    observations = ToolFactsCollector()
    try:
        output = await BashTool().execute_with_runtime(
            {"command": command, "timeout": timeout}, runtime(env, observations),
        )
        facts = observations.close()
        assert output.startswith(f"Exit code: {code}")
        assert facts.exit_code == code and facts.timed_out is timed_out
        assert facts.content_changed is None
        if "partial" in command:
            assert (tmp_path / "partial").read_text() == "changed"
        else:
            assert not (tmp_path / "target").exists()
    finally:
        await env.cleanup()

async def test_bash_evidence_preserves_observations_and_output(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    observations = ToolFactsCollector()
    try:
        output = await BashEvidence(BashTool()).execute_with_runtime(
            {"command": "exit 7"}, runtime(env, observations),
        )
        assert output == "Exit code: 7"
        assert observations.close().exit_code == 7
    finally:
        await env.cleanup()

async def test_shared_bash_instance_keeps_concurrent_call_facts_separate(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    first, second = ToolFactsCollector(), ToolFactsCollector()
    tool = BashTool()
    try:
        outputs = await asyncio.gather(
            tool.execute_with_runtime({"command": "sleep 0.02; exit 3"}, runtime(env, first)),
            tool.execute_with_runtime({"command": "exit 7"}, runtime(env, second)),
        )
        assert outputs == ["Exit code: 3", "Exit code: 7"]
        assert first.close().exit_code == 3
        assert second.close().exit_code == 7
    finally:
        await env.cleanup()

@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize(("before", "content", "changed"), [
    ("same\n", "same\n", False), ("", "", False), (None, "", True), ("old\n", "new\n", True),
])
async def test_create_reports_content_effect_without_skipping_physical_write(
    tmp_path, overwrite, before, content, changed,
):
    target = tmp_path / "f.txt"
    if before is not None:
        target.write_text(before)
    previous_inode = target.stat().st_ino if target.exists() else None
    env = LocalEnvironment(str(tmp_path))
    observations = ToolFactsCollector()
    try:
        output = await FileWriteTool().execute_with_runtime(
            {"path": "f.txt", "mode": "create", "content": content, "overwrite": overwrite},
            runtime(env, observations),
        )
        facts = observations.close()
        assert output.startswith("Created/wrote")
        assert facts.write_completed is True and facts.content_changed is changed
        assert facts.changed_paths == (("f.txt",) if changed else ())
        assert target.read_text() == content
        if previous_inode is not None:
            assert target.stat().st_ino != previous_inode
        if not changed:
            assert "content unchanged" in output
    finally:
        await env.cleanup()

async def test_existing_before_is_reused_and_legacy_overwrite_stays_unknown():
    class CountingEnvironment(FakeRemoteEnv):
        def __init__(self):
            super().__init__({"f.txt": "same"})
            self.reads = 0
            self.writes = 0

        async def read_file(self, path):
            self.reads += 1
            return await super().read_file(path)

        async def write_file(self, path, content):
            self.writes += 1
            await super().write_file(path, content)

    env = CountingEnvironment()
    observations = ToolFactsCollector()
    await FileWriteTool().execute_with_runtime(
        {"path": "f.txt", "mode": "create", "content": "same"}, runtime(env, observations),
    )
    assert observations.close().content_changed is False
    assert (env.reads, env.writes) == (1, 1)
    observations = ToolFactsCollector()
    await FileWriteTool().execute_with_runtime(
        {"path": "f.txt", "mode": "create", "content": "new", "overwrite": True}, runtime(env, observations),
    )
    facts = observations.close()
    assert facts.write_completed is True and facts.content_changed is None
    assert (env.reads, env.writes) == (1, 2)

@pytest.mark.parametrize(("tool", "params"), [
    (FileWriteTool(), {"mode": "str_replace", "old_str": "old", "new_str": "new"}),
    (ApplyPatchTool(), {"mode": "line_replace", "start_line": 1, "end_line": 1, "new_str": "new\n"}),
])
async def test_targeted_edit_reports_success_after_write(tool, params):
    env = FakeRemoteEnv({"f.txt": "old\n"})
    observations = ToolFactsCollector()
    output = await tool.execute_with_runtime({"path": "f.txt", **params}, runtime(env, observations))
    facts = observations.close()
    assert not output.startswith("Error")
    assert facts.write_completed is True and facts.content_changed is True
    assert facts.changed_paths == ("f.txt",)
    assert env.files["f.txt"] == "new\n"

async def test_write_failure_after_side_effect_leaves_effect_unknown():
    class PartialEnvironment(FakeRemoteEnv):
        async def write_file(self, path, content):
            self.files[path] = content
            raise OSError("durability failed after publication")

    env = PartialEnvironment({"f.txt": "old"})
    observations = ToolFactsCollector()
    with pytest.raises(OSError):
        await FileWriteTool().execute_with_runtime(
            {"path": "f.txt", "mode": "create", "content": "new"}, runtime(env, observations),
        )
    facts = observations.close()
    assert env.files["f.txt"] == "new"
    assert facts.write_completed is False and facts.content_changed is None

async def test_noop_replacement_keeps_existing_rejection_and_known_no_write():
    observations = ToolFactsCollector()
    output = await FileWriteTool().execute_with_runtime(
        {"path": "f.txt", "mode": "str_replace", "old_str": "same", "new_str": "same"},
        runtime(FakeRemoteEnv({"f.txt": "same"}), observations),
    )
    facts = observations.close()
    assert output.startswith("Error:")
    assert facts.write_completed is False and facts.content_changed is False

@pytest.mark.parametrize("name,params", [
    ("file_write", {"path": "module.py", "mode": "str_replace", "old_str": "old", "new_str": "new"}),
    ("apply_patch", {"path": "module.py", "mode": "line_replace", "start_line": 1, "end_line": 1,
                     "new_str": "new\n"}),
])
async def test_native_edit_evidence_preserves_tool_api_facts_and_execution_history(name, params):
    bash, edit = evidence_tools("bash", name, headless=False)
    native, = builtin_tools(name, headless=False)
    assert edit.to_openai_schema() == native.to_openai_schema()
    command = "python -m pytest -rA test_module.py"
    spec = parse_test_command(command, "/work")
    passed = SimpleNamespace(returncode=0, stdout="PASSED test_module.py::test_case\n1 passed in 0.01s", stderr="")
    bash.record(command, spec, passed)
    env, reference = FakeRemoteEnv({"module.py": "old\n"}), FakeRemoteEnv({"module.py": "old\n"})
    observed, native_facts = ToolFactsCollector(), ToolFactsCollector()
    result = await edit.execute_with_runtime(params, runtime(env, observed))
    expected = await native.execute_with_runtime(params, runtime(reference, native_facts))
    assert result == expected
    assert observed.close() == native_facts.close()
    assert env.files == reference.files == {"module.py": "new\n"}
    history, = bash.verification_records
    assert (history["exit_code"], history["verified"]) == (0, True)
    assert history["applicability"] == "unknown" and history["post_test_edits"] == ["module.py"]
    assert not bash.verified_targets
    history["post_test_edits"].append("caller.py")
    assert bash.verification_records[0]["post_test_edits"] == ["module.py"]
    bash.record(command, spec, passed)
    latest = bash.verification_records[-1]
    assert latest["applicability"] == "current" and latest["post_test_edits"] == []
    assert bash.verified_targets == {"test_module.py", "test_module.py::test_case"}
    await edit.execute_with_runtime({**params, **({"old_str": "new", "new_str": "next"} if name == "file_write"
                                                else {"new_str": "next\n"})}, runtime(env, ToolFactsCollector()))
    first, second = bash.verification_records
    assert first["post_test_edits"] == ["module.py", "module.py"]
    assert second["post_test_edits"] == ["module.py"]

@pytest.mark.parametrize(("name", "params"), [
    ("file_write", {"mode": "create", "content": "def test_case():\n    assert False\n"}),
    ("file_write", {"mode": "create", "content": "def test_case():\n    assert False\n", "overwrite": True}),
    ("file_write", {"mode": "str_replace", "old_str": "assert True", "new_str": "assert False"}),
    ("apply_patch", {"mode": "line_replace", "start_line": 2, "end_line": 2, "new_str": "    assert False\n"}),
])
async def test_cancelled_native_write_invalidates_evidence_after_publication(tmp_path, monkeypatch, name, params):
    path = "test_example.py"
    target = tmp_path / path
    target.write_text("def test_case():\n    assert True\n")
    command = f"{shlex.quote(sys.executable)} -B -m pytest -q -rA -p no:cacheprovider {path}::test_case"
    bash, edit = evidence_tools("bash", name, headless=False)
    env = LocalEnvironment(str(tmp_path))
    observations = ToolFactsCollector()
    started, release = threading.Event(), threading.Event()
    native_write = _env_local.write_regular_bytes_atomic
    task = None

    def delayed_write(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return native_write(*args, **kwargs)

    try:
        await bash.execute_with_runtime({"command": command}, runtime(env, None))
        assert bash.verified_targets == {f"{path}::test_case"}
        monkeypatch.setattr(_env_local, "write_regular_bytes_atomic", delayed_write)
        task = asyncio.create_task(edit.execute_with_runtime({"path": path, **params}, runtime(env, observations)))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert target.read_text() == "def test_case():\n    assert False\n"
        rerun = await env.exec_cmd(command)
        assert rerun.returncode == 1 and "1 failed" in rerun.stdout
        facts = observations.close()
        assert facts.write_completed is False and facts.content_changed is None
        history, = bash.verification_records
        assert (history["exit_code"], history["verified"]) == (0, True)
        assert history["applicability"] == "unknown"
        assert history["post_test_edits"] == [path]
        assert bash.verified_targets == set()
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await env.cleanup()

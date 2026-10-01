"""Native tool observations retain actual execution and file-write outcomes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.evidence.bash_evidence import BashEvidence
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.domain.tool_facts import ToolFactsCollector
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

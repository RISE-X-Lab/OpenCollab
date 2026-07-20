import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tool_execution_test_support import AlwaysAllowPermissionPolicy as FakePermissionPolicy

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools import base, human
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.human import AskUserTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")

    async def read_file(self, path: str) -> str:
        raise AssertionError("read_file was not expected")

    async def write_file(self, path: str, content: str) -> None:
        raise AssertionError("write_file was not expected")


class SpySafetyPolicy:
    def __init__(self):
        self.cmd_calls = []
        self.path_calls = []

    def check_path(self, target_path: str) -> str:
        self.path_calls.append(target_path)
        return target_path

    def check_cmd(self, cmd: str) -> None:
        pass

    def is_risky(self, cmd: str) -> bool:
        return False

    async def check_cmd_interactive(self, cmd: str, confirm_fn=None) -> None:
        self.cmd_calls.append((cmd, confirm_fn))


class FakeRemoteEnv:
    """Environment that does I/O somewhere other than the host filesystem."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content


class SpyLock:
    instances: list["SpyLock"] = []

    def __init__(self, *args, **kwargs):
        SpyLock.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# ToolRuntime wiring
# ---------------------------------------------------------------------------


def test_tool_runtime_constructs_directly_with_env_safety_and_permission():
    env = FakeEnv()
    safety = SpySafetyPolicy()
    permission_policy = FakePermissionPolicy()

    runtime = ToolRuntime(
        environment=env,
        safety_policy=safety,
        permission_policy=permission_policy,
    )

    assert runtime.environment is env
    assert runtime.safety_policy is safety
    assert runtime.permission_policy is permission_policy
    assert runtime.confirm_fn() == permission_policy.confirm


def test_tool_runtime_confirm_fn_returns_none_without_permission_policy():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)
    assert runtime.confirm_fn() is None


# ---------------------------------------------------------------------------
# BashTool
# ---------------------------------------------------------------------------


def test_bash_tool_without_env_returns_existing_error():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(BashTool().execute_with_runtime({"command": "pwd"}, runtime))

    assert result == "Error: no execution environment available."


def test_bash_tool_passes_permission_confirm_fn_to_safety_check():
    env = FakeEnv(stdout="ok\n")
    safety = SpySafetyPolicy()
    permission_policy = FakePermissionPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=permission_policy)

    result = run(BashTool().execute_with_runtime({"command": "echo ok"}, runtime))

    assert safety.cmd_calls == [("echo ok", permission_policy.confirm)]
    assert env.exec_calls == [("echo ok", 120.0)]
    assert "stdout:\nok" in result


def test_bash_tool_respects_custom_timeout():
    env = FakeEnv(stdout="ok\n")
    safety = SpySafetyPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=None)

    run(BashTool().execute_with_runtime({"command": "echo ok", "timeout": 7}, runtime))

    assert env.exec_calls == [("echo ok", 7)]


def test_bash_tool_preserves_blocked_command_behavior(tmp_path):
    env = FakeEnv()
    sandbox = SandboxInterceptor(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(BashTool().execute_with_runtime({"command": "rm -rf /"}, runtime))

    assert env.exec_calls == []


# ---------------------------------------------------------------------------
# FileReadTool
# ---------------------------------------------------------------------------


def test_file_read_reads_through_runtime(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    result = run(
        FileReadTool().execute_with_runtime(
            {"path": "note.txt", "offset": 2, "limit": 1},
            runtime,
        )
    )

    assert "File: note.txt (3 lines total, showing 2-2)" in result
    assert "2\tbeta" in result
    assert "1\talpha" not in result


def test_file_read_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(FileReadTool().execute_with_runtime({"path": "/etc/passwd"}, runtime))


def test_file_read_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "missing.txt"}, runtime))

    assert result == "Error: no execution environment available."


def test_file_read_preserves_file_not_found(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "missing.txt"}, runtime))

    assert result == "Error: file not found: missing.txt"


def test_file_read_preserves_permission_error_string(monkeypatch, tmp_path):
    async def raise_permission_error(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(LocalEnvironment, "read_file", raise_permission_error)
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "secret.txt"}, runtime))

    assert result == "Error: denied"


# ---------------------------------------------------------------------------
# FileWriteTool
# ---------------------------------------------------------------------------


def test_file_write_create_and_str_replace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    create_result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "alpha\n"},
            runtime,
        )
    )
    replace_result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert "Created/wrote" in create_result
    assert "Replaced in" in replace_result
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "beta\n"


def test_file_write_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(
            FileWriteTool().execute_with_runtime(
                {"path": "/tmp/outside.txt", "mode": "create", "content": "nope"},
                runtime,
            )
        )


def test_file_write_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "x"},
            runtime,
        )
    )

    assert result == "Error: no execution environment available."


def test_file_write_preserves_missing_old_str(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("alpha\n", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "new_str": "beta"},
            runtime,
        )
    )

    assert result == "Error: old_str is required for str_replace mode."


def test_file_write_preserves_duplicate_old_str_error(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert (
        result
        == "Error: old_str found 2 times in note.txt. Provide more context to make it unique."
    )


# ---------------------------------------------------------------------------
# Host write lock — only taken when I/O hits the host filesystem
# ---------------------------------------------------------------------------


def test_file_write_remote_env_takes_no_host_lock(monkeypatch):
    SpyLock.instances = []
    monkeypatch.setattr(base, "FileLock", SpyLock)
    env = FakeRemoteEnv({"note.txt": "alpha\n"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert "Replaced in" in result
    assert env.files["note.txt"] == "beta\n"
    assert SpyLock.instances == []


def test_apply_patch_remote_env_takes_no_host_lock(monkeypatch):
    SpyLock.instances = []
    monkeypatch.setattr(base, "FileLock", SpyLock)
    env = FakeRemoteEnv({"f.py": "a\nb\nc\nd\n"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "B",
            },
            runtime,
        )
    )

    assert "Applied line_replace" in result
    assert env.files["f.py"] == "a\nB\nc\nd\n"
    assert SpyLock.instances == []


def test_host_write_lock_files_live_outside_target_workspace(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    lock_path = Path(base._host_lock_path("x.txt", env))

    async def exercise():
        async with base.host_write_lock("x.txt", env) as lock:
            assert isinstance(lock, base.FileLock)
            assert lock_path.exists()

    run(exercise())

    assert tmp_path not in lock_path.parents
    assert list(tmp_path.rglob("*.lock")) == []


class SlowLocalWriteEnv(FakeRemoteEnv):
    local_filesystem = True
    workspace = "/virtual/workspace"

    async def read_file(self, path: str) -> str:
        await asyncio.sleep(0.05)
        return await super().read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        await asyncio.sleep(0.05)
        await super().write_file(path, content)


def test_concurrent_host_writes_do_not_block_event_loop_on_lock():
    env = SlowLocalWriteEnv({"note.txt": "initial"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    tool = FileWriteTool()

    async def exercise():
        await asyncio.wait_for(
            asyncio.gather(
                tool.execute_with_runtime(
                    {
                        "path": "note.txt",
                        "mode": "create",
                        "content": "first",
                        "overwrite": True,
                    },
                    runtime,
                ),
                tool.execute_with_runtime(
                    {
                        "path": "note.txt",
                        "mode": "create",
                        "content": "second",
                        "overwrite": True,
                    },
                    runtime,
                ),
            ),
            timeout=1.0,
        )

    started = time.monotonic()
    run(exercise())

    assert time.monotonic() - started < 1.0
    assert env.files["note.txt"] in {"first", "second"}


def test_host_write_lock_never_pollutes_git_diff(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "hello\n"},
            runtime,
        )
    )
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert result.startswith("Created/wrote")
    assert ".lock" not in status
    assert list(tmp_path.rglob("*.lock")) == []


# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------


def test_grep_tool_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(GrepTool().execute_with_runtime({"pattern": "needle"}, runtime))

    assert result == "Error: no execution environment available."


def test_grep_tool_routes_search_path_through_safety_policy():
    env = FakeEnv(stdout="src/app.py:1:needle\n")
    safety = SpySafetyPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=None)

    result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "needle", "path": "../outside", "glob": "*.py", "max_results": 3},
            runtime,
        )
    )

    assert result == "src/app.py:1:needle"
    assert safety.path_calls == ["../outside"]
    assert len(env.exec_calls) == 1
    cmd, timeout = env.exec_calls[0]
    assert "needle" in cmd
    assert "../outside" in cmd
    assert timeout == 30


def test_grep_tool_rejects_non_integer_max_results_before_exec():
    env = FakeEnv(stdout="src/app.py:1:needle\n")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "needle", "max_results": "1; touch pwned"},
            runtime,
        )
    )

    assert result == "Error: max_results must be an integer."
    assert env.exec_calls == []


def test_grep_tool_terminates_options_before_pattern_and_path():
    env = FakeEnv(stdout="")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(
        GrepTool().execute_with_runtime(
            {"pattern": "--pre=touch pwned", "path": "--debug"},
            runtime,
        )
    )

    cmd, _ = env.exec_calls[0]
    assert "rg -n --max-count 50 -- '--pre=touch pwned' --debug" in cmd
    assert (
        "grep -rEn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.opencollab "
        "-- '--pre=touch pwned' --debug"
    ) in cmd


def test_file_read_description_teaches_distill_and_forbids_reread():
    # The file_read description is the UNIVERSAL carrier of the anti-thrash rule:
    # every workflow/team that has the tool sees it, regardless of its prompt. Pins
    # the rule (distill into notes; trust the cleared stub; don't re-read to
    # reconfirm) motivated by a 90-read/0-write/step-cap stall.
    desc = FileReadTool.description
    assert "Distill as you read" in desc
    assert "empty reply" in desc  # forbids bare tool-call turns
    assert "re-read" in desc      # trust the stub instead


def test_grep_tool_fallback_uses_ere_and_skips_session_dir():
    # When rg is missing from PATH the command falls back to grep. That fallback
    # MUST use -E (ERE) so `a|b|c` alternation works — plain `grep -rn` is BRE,
    # where `|` is a literal char and an alternation pattern silently matches
    # nothing. It must also skip .opencollab so it can't "match" its own logged
    # pattern strings instead of real source. (Regression: a 100-step run stalled
    # because every alternation grep returned "No matches found".)
    env = FakeEnv(stdout="")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(
        GrepTool().execute_with_runtime(
            {"pattern": "Scheduler|RowStatus"}, runtime
        )
    )

    cmd, _ = env.exec_calls[0]
    assert "|| grep -rEn" in cmd
    assert " grep -rn " not in cmd  # the buggy BRE fallback must be gone
    assert "--exclude-dir=.opencollab" in cmd


# ---------------------------------------------------------------------------
# AskUserTool
# ---------------------------------------------------------------------------


class FakeAskPolicy:
    """Records the question and returns a canned answer (no terminal I/O)."""

    def __init__(self, answer: str = "Use the smaller patch."):
        self.answer = answer
        self.questions: list[str] = []

    async def ask(self, question: str) -> str:
        self.questions.append(question)
        return self.answer


def test_ask_user_tool_non_interactive_fallback():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(AskUserTool().execute_with_runtime({"question": "Proceed?"}, runtime))

    assert result == "Running in non-interactive mode. Make your own best judgment and proceed."


def test_ask_user_tool_routes_through_ask_port():
    ask_policy = FakeAskPolicy()
    runtime = ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=None,
        ask_policy=ask_policy,
    )

    result = run(AskUserTool().execute_with_runtime({"question": "Proceed?"}, runtime))

    assert ask_policy.questions == ["Proceed?"]
    assert result == "Use the smaller patch."


def test_ask_user_tool_ask_port_used_even_in_yolo_without_confirm():
    # --yolo wires no permission (confirm) policy but still wires an ask policy:
    # the tool must stay interactive instead of falsely reporting non-interactive.
    ask_policy = FakeAskPolicy(answer="keep going")
    runtime = ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=None,
        ask_policy=ask_policy,
    )

    result = run(AskUserTool().execute_with_runtime({"question": "Ship it?"}, runtime))

    assert result == "keep going"


def test_ask_user_tool_declines_on_ask_port_eof():
    class EofAskPolicy:
        async def ask(self, question: str) -> str:
            raise EOFError

    runtime = ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=None,
        ask_policy=EofAskPolicy(),
    )

    result = run(AskUserTool().execute_with_runtime({"question": "Proceed?"}, runtime))

    assert result == "User declined to answer."


def test_ask_user_tool_uses_prompt_when_no_ask_port_but_permission_exists(monkeypatch):
    runtime = ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=FakePermissionPolicy(),
    )
    prompts = []

    def fake_prompt(question: str) -> str:
        prompts.append(question)
        return "Use the smaller patch."

    monkeypatch.setattr(human, "_prompt_user", fake_prompt)

    result = run(AskUserTool().execute_with_runtime({"question": "Proceed?"}, runtime))

    assert prompts == ["Proceed?"]
    assert result == "Use the smaller patch."


# ---------------------------------------------------------------------------
# Tool contract guards
# ---------------------------------------------------------------------------


def test_built_in_tools_have_native_execute_with_runtime_methods():
    assert BashTool.execute_with_runtime is not Tool.execute_with_runtime
    assert FileReadTool.execute_with_runtime is not Tool.execute_with_runtime
    assert FileWriteTool.execute_with_runtime is not Tool.execute_with_runtime
    assert GrepTool.execute_with_runtime is not Tool.execute_with_runtime
    assert AskUserTool.execute_with_runtime is not Tool.execute_with_runtime
    assert ApplyPatchTool.execute_with_runtime is not Tool.execute_with_runtime
    assert GitDiffTool.execute_with_runtime is not Tool.execute_with_runtime
    assert RunTestsTool.execute_with_runtime is not Tool.execute_with_runtime


def test_base_tool_default_execute_with_runtime_raises_not_implemented():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    with pytest.raises(NotImplementedError):
        run(Tool().execute_with_runtime({}, runtime))


def test_no_concrete_tool_defines_legacy_execute():
    for name in ("bash", "apply_patch", "fs", "git_diff", "human", "run_tests"):
        mod = __import__(f"opencollab.adapters.tools.{name}", fromlist=["_"])
        for cls in vars(mod).values():
            if isinstance(cls, type) and issubclass(cls, Tool) and cls is not Tool:
                assert "execute" not in cls.__dict__, (
                    f"{cls.__name__} still overrides legacy execute"
                )


def test_base_tool_does_not_define_legacy_execute():
    assert "execute" not in Tool.__dict__


def test_tool_modules_do_not_import_inner_layers_or_concrete_sandbox():
    package_root = Path(__file__).resolve().parents[1]
    tool_files = [
        package_root / "opencollab/adapters/tools/base.py",
        package_root / "opencollab/adapters/tools/bash.py",
        package_root / "opencollab/adapters/tools/apply_patch.py",
        package_root / "opencollab/adapters/tools/fs.py",
        package_root / "opencollab/adapters/tools/git_diff.py",
        package_root / "opencollab/adapters/tools/human.py",
        package_root / "opencollab/adapters/tools/run_tests.py",
    ]

    for path in tool_files:
        source = path.read_text(encoding="utf-8")
        assert "opencollab.core.session" not in source
        assert "opencollab.bootstrap" not in source
        assert "opencollab.adapters.safety" not in source
        assert "SandboxInterceptor" not in source

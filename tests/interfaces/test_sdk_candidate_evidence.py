"""Candidate evidence belongs to one public SDK call, including shared tools."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters import _env_local
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application import tool_execution_runtime
from opencollab.tools import evidence_tools

TEST_COMMAND = f"{shlex.quote(sys.executable)} -B -m pytest -q -rA -p no:cacheprovider test_ok.py"
PASSING_SOURCE = "def ok():\n    return True\n"
FAILING_SOURCE = "def ok():\n    return False\n"

def _files(root: Path) -> None:
    (root / "app.py").write_text(FAILING_SOURCE)
    (root / "test_ok.py").write_text("from app import ok\ndef test_ok():\n    assert ok()\n")

def _response(name: str | None = None, arguments: dict | None = None) -> LLMResponse:
    calls = [] if name is None else [{
        "id": "controlled-call", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments),
        },
    }]
    return LLMResponse(
        content=None if calls else "finished", tool_calls=calls,
        usage=Usage(4, 2), finish_reason="tool_calls" if calls else "stop",
    )

def _write(content: str) -> LLMResponse:
    return _response("file_write", {"path": "app.py", "mode": "create", "content": content})

async def test_public_candidates_reusing_tools_keep_only_their_executed_tests(tmp_path, monkeypatch):
    _files(tmp_path)
    for args in (
        ("init", "-q"), ("add", "app.py", "test_ok.py"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    models = []

    class ControlledModel:
        def __init__(self, **kwargs):
            self.index = len(models)
            self.calls = 0
            models.append(self)

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.index == 0 and self.calls == 1:
                return _write(PASSING_SOURCE)
            if self.index == 0 and self.calls == 2:
                return _response("bash", {"command": TEST_COMMAND})
            return _response()

        async def aclose(self):
            pass

    monkeypatch.setattr("opencollab.bootstrap.container.LLMClient", ControlledModel)
    shared = evidence_tools("bash", "file_write", headless=False, limits={"bash": {"max_output_chars": 2000}})

    async def flow(ctx, _inputs):
        first = await ctx.candidate_agent("repair and test", label="first", tools=shared)
        second = await ctx.candidate_agent("finish immediately", label="second", tools=shared)
        return first, second

    result = await OpenCollab(
        tmp_path, model="controlled", provider="openai",
        api_key="candidate-evidence-fixture",  # pragma: allowlist secret
    ).workflow(flow, budget=100_000, trace=False)
    assert result.ok, result.reason
    first, second = result.output
    assert first.diff and first.verified_targets == ("test_ok.py", "test_ok.py::test_ok")
    assert first.test_records[0]["verified"] is True
    assert second.diff == ""
    assert second.test_records == ()
    assert second.verified_targets == ()
    assert models[1].calls == 1
    assert shared[0].max_output_chars == 2000
    assert not shared[0].verification_records
    source = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert source.returncode == 1
    assert "1 failed" in source.stdout

async def test_public_candidate_reports_evidence_as_uncertain_after_file_write_timeout(tmp_path, monkeypatch):
    _files(tmp_path)
    (tmp_path / "app.py").write_text(PASSING_SOURCE)
    for args in (
        ("init", "-q"), ("add", "app.py", "test_ok.py"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    class ControlledModel:
        calls = 0
        write_result = ""

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _response("bash", {"command": TEST_COMMAND})
            if self.calls == 2:
                return _write(FAILING_SOURCE)
            self.write_result = next(message["content"] for message in reversed(messages) if message["role"] == "tool")
            return _response()

        async def aclose(self):
            pass

    model = ControlledModel()
    monkeypatch.setattr("opencollab.bootstrap.container.LLMClient", lambda **kwargs: model)
    started, release = threading.Event(), threading.Event()
    native_write = _env_local.write_regular_bytes_atomic
    cleanup = tool_execution_runtime.ToolExecutionRuntimeMixin._cleanup_timed_out_execution

    def delayed_write(root_fd, workspace, path, payload, **kwargs):
        if path == "app.py":
            started.set()
            assert release.wait(5)
        return native_write(root_fd, workspace, path, payload, **kwargs)

    async def release_write_after_timeout(executor, task):
        assert started.is_set()
        release.set()
        return await cleanup(executor, task)

    monkeypatch.setattr(_env_local, "write_regular_bytes_atomic", delayed_write)
    monkeypatch.setattr(tool_execution_runtime, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.0)
    monkeypatch.setattr(
        tool_execution_runtime.ToolExecutionRuntimeMixin, "_cleanup_timed_out_execution", release_write_after_timeout,
    )

    async def flow(ctx, _inputs):
        bash, = evidence_tools("bash", headless=False)
        edit = FileWriteTool()
        edit.default_timeout = 0.5
        return await ctx.candidate_agent("test then edit", label="repair", tools=(bash, bash.observe_edits(edit)))

    try:
        result = await OpenCollab(
            tmp_path, model="controlled", provider="openai",
            api_key="candidate-evidence-fixture",  # pragma: allowlist secret
        ).workflow(flow, budget=100_000, trace=False)
    finally:
        release.set()
    assert result.ok, result.reason
    assert "Tool execution timed out" in model.write_result
    candidate = result.output
    assert "+    return False" in candidate.diff
    assert (tmp_path / "app.py").read_text() == PASSING_SOURCE
    subprocess.run(
        ["git", "apply", "-"], cwd=tmp_path, input=candidate.diff, text=True, check=True, capture_output=True,
    )
    rerun = subprocess.run(shlex.split(TEST_COMMAND), cwd=tmp_path, capture_output=True, text=True)
    assert rerun.returncode == 1 and "1 failed" in rerun.stdout
    record, = candidate.test_records
    assert (record["exit_code"], record["verified"]) == (0, True)
    assert record["applicability"] == "unknown"
    assert record["post_test_edits"] == ["app.py"]
    assert candidate.verified_targets == ()

@pytest.mark.parametrize(("outcome", "receipt"), [
    ("os_error", "Tool execution error: OSError: publication result unavailable"),
    ("permission_error", "Error: publication result unavailable"),
    ("unknown_comparison", "Created/wrote app.py"),
    ("unchanged", "content unchanged"),
])
async def test_public_candidate_requires_a_confirmed_unchanged_write_to_keep_pass_evidence(
    tmp_path, monkeypatch, outcome, receipt,
):
    _files(tmp_path)
    (tmp_path / "app.py").write_text(PASSING_SOURCE)
    for args in (
        ("init", "-q"), ("add", "app.py", "test_ok.py"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    command = f"{TEST_COMMAND} --color=yes"
    unchanged = outcome == "unchanged"
    next_responses = iter((
        _response("bash", {"command": command}),
        _response("file_write", {
            "path": "app.py", "mode": "create", "overwrite": True,
            "content": PASSING_SOURCE if unchanged else FAILING_SOURCE,
        }),
    ))
    write_receipts = []

    class ControlledModel:
        async def complete(self, messages, **kwargs):
            response = next(next_responses, None)
            if response is not None:
                return response
            write_receipts.append(next(
                message["content"] for message in reversed(messages) if message["role"] == "tool"
            ))
            return _response()

        async def aclose(self):
            pass

    native_write = _env_local.write_regular_bytes_atomic

    def publish_then_report(root_fd, workspace, path, payload, **kwargs):
        changed = native_write(root_fd, workspace, path, payload, **kwargs)
        if path != "app.py":
            return changed
        assert changed is (not unchanged)
        if outcome == "os_error":
            raise OSError("publication result unavailable")
        if outcome == "permission_error":
            raise PermissionError("publication result unavailable")
        return None if outcome == "unknown_comparison" else changed

    monkeypatch.setattr("opencollab.bootstrap.container.LLMClient", lambda **kwargs: ControlledModel())
    monkeypatch.setattr(_env_local, "write_regular_bytes_atomic", publish_then_report)

    async def flow(ctx, _inputs):
        tools = evidence_tools("bash", "file_write", headless=False)
        return await ctx.candidate_agent("test then edit", label="repair", tools=tools)

    result = await OpenCollab(
        tmp_path, model="controlled", provider="openai",
        api_key="candidate-evidence-fixture",  # pragma: allowlist secret
    ).workflow(flow, budget=100_000, trace=False)
    assert result.ok, result.reason
    assert len(write_receipts) == 1 and receipt in write_receipts[0]
    candidate = result.output
    record, = candidate.test_records
    assert (record["exit_code"], record["verified"]) == (0, True)
    assert (tmp_path / "app.py").read_text() == PASSING_SOURCE
    if unchanged:
        assert candidate.diff == ""
        assert record["applicability"] == "current"
        assert record["post_test_edits"] == []
        assert candidate.verified_targets == ("test_ok.py", "test_ok.py::test_ok")
    else:
        subprocess.run(
            ["git", "apply", "-"], cwd=tmp_path, input=candidate.diff, text=True, check=True, capture_output=True,
        )
        assert record["applicability"] == "unknown"
        assert record["post_test_edits"] == ["app.py"]
        assert candidate.verified_targets == ()
    rerun = subprocess.run(shlex.split(command), cwd=tmp_path, capture_output=True, text=True)
    assert rerun.returncode == (0 if unchanged else 1)

@pytest.mark.parametrize("reverse", [False, True])
async def test_public_candidate_rerun_from_another_directory_clears_the_same_test(tmp_path, monkeypatch, reverse):
    _files(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "test_ok.py").unlink()
    (tests / "test_ok.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from app import ok\ndef test_ok():\n"
        "    assert ok() and os.environ.get('RUN_MODE') != 'fail'\n"
    )
    for args in (
        ("init", "-q"), ("add", "app.py", "tests/test_ok.py"),
        ("-c", "user.name=OpenCollab Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm",
         "test: \u521d\u59cb\u5316\u5019\u9009\u6d4b\u8bd5\u9879\u76ee"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    base = f"{shlex.quote(sys.executable)} -B -m pytest -rA -p no:cacheprovider"
    root_command, nested_command = f"{base} tests/test_ok.py", f"cd tests && {base} test_ok.py"
    first = nested_command if reverse else root_command
    second = f"RUN_MODE=fail {root_command}" if reverse else f"cd tests && RUN_MODE=fail {base} test_ok.py"

    class ControlledModel:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _write(PASSING_SOURCE)
            if self.calls in {2, 3}:
                return _response("bash", {"command": first if self.calls == 2 else second})
            return _response()

        async def aclose(self):
            pass

    monkeypatch.setattr("opencollab.bootstrap.container.LLMClient", lambda **kwargs: ControlledModel())

    async def flow(ctx, _inputs):
        return await ctx.candidate_agent("repair and test from both directories", label="repair", tools=evidence_tools(
            "bash", "file_write", headless=False,
        ))

    result = await OpenCollab(
        tmp_path, model="controlled", provider="openai",
        api_key="candidate-evidence-fixture",  # pragma: allowlist secret
    ).workflow(flow, budget=100_000, trace=False)
    assert result.ok, result.reason
    candidate = result.output
    assert candidate.diff and "return True" in candidate.diff
    assert candidate.verified_targets == ()
    assert [record["exit_code"] for record in candidate.test_records] == [0, 1]
    assert [record["verified"] for record in candidate.test_records] == [True, False]
    assert [record["command"] for record in candidate.test_records] == [first, second]
    assert candidate.test_records[0]["target"] == ("test_ok.py" if reverse else "tests/test_ok.py")
    assert (tmp_path / "app.py").read_text() == FAILING_SOURCE

class _AliasEnvironment:
    """Distinct environments can expose the same container-side workspace."""

    workspace = "/app"
    local_filesystem = False
    process_isolated = False

    def __init__(self, root: Path):
        self.delegate = LocalEnvironment(str(root))

    @property
    def revoked(self):
        return self.delegate.revoked

    async def setup(self, mount_dir=None):
        await self.delegate.setup()
        return self.workspace

    async def exec_cmd(self, command, timeout=120):
        return await self.delegate.exec_cmd(command, timeout)

    async def read_file(self, path):
        return await self.delegate.read_file(path.removeprefix("/app/"))

    async def write_file(self, path, content):
        return await self.delegate.write_file(path.removeprefix("/app/"), content)

    async def cleanup(self):
        await self.delegate.cleanup()

    async def abort(self):
        await self.delegate.abort()

    def revoke(self):
        self.delegate.revoke()

class _AliasLease:
    def __init__(self, environment):
        self.environment = environment

    async def diff(self):
        return await self.environment.read_file("app.py")

    async def cleanup(self):
        await self.environment.cleanup()

class _AliasCandidates:
    def __init__(self, root):
        self.root = root
        self.environments = []

    async def source_diff(self, exclude_paths=()):
        return ""

    async def acquire(self, label):
        directory = self.root / label
        directory.mkdir()
        _files(directory)
        environment = _AliasEnvironment(directory)
        self.environments.append(environment)
        return _AliasLease(environment)

@pytest.mark.parametrize("parallel", [False, True])
async def test_shared_tools_scope_edits_to_candidate_with_same_workspace(tmp_path, monkeypatch, parallel):
    both_tested = asyncio.Event()
    edited = asyncio.Event()
    tested = 0

    class ControlledModel:
        def __init__(self, **kwargs):
            self.calls = 0

        async def complete(self, messages, **kwargs):
            nonlocal tested
            self.calls += 1
            label = "edited" if any(
                "edited" in item.get("content", "") for item in messages if item["role"] == "user"
            ) else "passing"
            if self.calls == 1:
                return _write(PASSING_SOURCE)
            if self.calls == 2:
                return _response("bash", {"command": TEST_COMMAND})
            if self.calls == 3:
                tested += 1
                if tested == 2:
                    both_tested.set()
                if parallel:
                    await both_tested.wait()
                if label == "edited":
                    return _write(FAILING_SOURCE)
                if parallel:
                    await edited.wait()
            elif label == "edited":
                edited.set()
            return _response()

        async def aclose(self):
            pass

    monkeypatch.setattr("opencollab.bootstrap.container.LLMClient", ControlledModel)
    shared = evidence_tools("file_write", "bash", headless=False)
    backend = _AliasCandidates(tmp_path)

    async def flow(ctx, _inputs):
        async def run(label):
            return await ctx.candidate_agent(label, label=label, tools=shared, budget=40_000)
        if parallel:
            return await asyncio.gather(run("edited"), run("passing"))
        return await run("edited"), await run("passing")

    result = await OpenCollab(
        tmp_path, model="controlled", provider="openai",
        api_key="candidate-evidence-fixture",  # pragma: allowlist secret
    ).workflow(flow, budget=100_000, trace=False, candidate_workspace=backend, timeout=20)
    assert result.ok, result.reason
    changed, passing = result.output
    assert changed.verified_targets == ()
    assert len(changed.test_records) == 1
    assert changed.test_records[0]["verified"] is True
    assert changed.test_records[0]["applicability"] == "unknown"
    assert changed.test_records[0]["post_test_edits"] == ["app.py"]
    assert passing.verified_targets == ("test_ok.py", "test_ok.py::test_ok")
    assert len(passing.test_records) == 1
    assert passing.test_records[0]["applicability"] == "current"
    assert passing.test_records[0]["post_test_edits"] == []
    assert passing.test_records[0]["workspace"] == changed.test_records[0]["workspace"] == "/app"
    assert backend.environments[0] is not backend.environments[1]

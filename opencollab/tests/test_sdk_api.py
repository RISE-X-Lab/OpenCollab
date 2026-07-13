from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

import opencollab.sdk as sdk
import pytest
from opencollab.adapters.env import DockerWorkspaceEnvironment
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool


def test_public_sdk_exports_versioned_authoring_and_runtime_surface() -> None:
    assert sdk.SDK_API_VERSION == 1
    expected = {
        "SDK_API_VERSION",
        "ApplyPatchTool",
        "BashTool",
        "ExecResult",
        "ExecutionEnvironment",
        "FileReadTool",
        "FileWriteTool",
        "GitDiffTool",
        "GrepTool",
        "InvalidSDKRequestError",
        "OpenCollabRuntime",
        "OpenCollabSDKError",
        "Registry",
        "RunTestsTool",
        "ExecutionEnvironment",
        "RunBudget",
        "RuntimeConfig",
        "Tool",
        "WorkflowContext",
        "WorkflowFn",
        "WorkflowManifestError",
        "WorkflowRunRequest",
        "WorkflowRunResult",
        "WorkflowRunTimeoutError",
        "WorkflowSpec",
        "attach_workspace",
        "coding_toolset",
        "discover_workflows",
        "model_context_window",
        "verification_run_tests_tool",
        "workflow",
    }
    assert set(sdk.__all__) == expected
    assert sdk.model_context_window("gpt-4o") == 128_000


def test_public_sdk_function_signatures_and_value_fields_are_stable() -> None:
    signatures = {
        "OpenCollabRuntime.run_workflow": "(self, request: 'WorkflowRunRequest') -> 'WorkflowRunResult'",
        "RuntimeConfig.from_mapping": "(config: 'Mapping[str, Any]') -> 'RuntimeConfig'",
        "attach_workspace": (
            "(*, container_id: 'str', repo_root: 'str', command_prefix: "
            "'Callable[[str], str] | str | None' = None, timeout_returncode: 'int' = -1) -> 'Environment'"
        ),
        "coding_toolset": (
            "(*, require_process_isolation: 'bool' = False, allow_test_command_overrides: 'bool' = True, "
            "allow_file_creation: 'bool' = True) -> 'tuple[Tool, ...]'"
        ),
        "discover_workflows": "(directory: 'str') -> 'Registry'",
        "model_context_window": "(model: 'str | None') -> 'int | None'",
        "verification_run_tests_tool": "() -> 'RunTestsTool'",
        "workflow": (
            "(*, name: 'str', description: 'str', phases: 'Sequence[str] | None' = None) -> "
            "'Callable[[WorkflowFn], WorkflowFn]'"
        ),
    }
    actual_signatures = {
        "OpenCollabRuntime.run_workflow": str(inspect.signature(sdk.OpenCollabRuntime.run_workflow)),
        "RuntimeConfig.from_mapping": str(inspect.signature(sdk.RuntimeConfig.from_mapping)),
        **{
            name: str(inspect.signature(getattr(sdk, name)))
            for name in signatures
            if "." not in name
        },
    }
    assert actual_signatures == signatures
    assert tuple(field.name for field in fields(sdk.RunBudget)) == (
        "max_tokens",
        "timeout_seconds",
        "max_concurrency",
        "cleanup_timeout_seconds",
    )
    assert tuple(field.name for field in fields(sdk.RuntimeConfig)) == (
        "model",
        "provider",
        "api_key",
        "base_url",
        "llm_timeout_seconds",
        "temperature",
        "top_p",
        "max_output_tokens",
        "thinking",
        "thinking_params",
    )
    assert tuple(field.name for field in fields(sdk.WorkflowRunRequest)) == (
        "workflow",
        "config",
        "inputs",
        "budget",
        "environment",
        "workspace",
        "artifact_dir",
        "trace",
    )
    assert tuple(field.name for field in fields(sdk.WorkflowRunResult)) == (
        "output",
        "workflow_name",
        "tokens_spent",
        "session_count",
        "artifact_dir",
        "manifest_path",
        "sdk_api_version",
    )


def test_runtime_config_is_validated_and_does_not_repr_secret() -> None:
    config = sdk.RuntimeConfig(model="model", provider="provider", api_key="secret")
    assert "secret" not in repr(config)
    assert config.as_runtime_dict()["api_key"] == "secret"

    with pytest.raises(sdk.InvalidSDKRequestError, match="model"):
        sdk.RuntimeConfig(model=" ", provider="provider")
    with pytest.raises(sdk.InvalidSDKRequestError, match="max_output_tokens"):
        sdk.RuntimeConfig(model="model", provider="provider", max_output_tokens=True)
    with pytest.raises(sdk.InvalidSDKRequestError, match="timeout_seconds"):
        sdk.RunBudget(timeout_seconds=float("inf"))

    mapped = sdk.RuntimeConfig.from_mapping(
        {"model": "model", "provider": "provider", "max_output_tokens": 4096}
    )
    assert mapped.max_output_tokens == 4096


def test_attach_workspace_builds_non_owning_container_environment() -> None:
    environment = sdk.attach_workspace(container_id="container-1", repo_root="/testbed")
    assert isinstance(environment, DockerWorkspaceEnvironment)
    assert environment.workspace == "/testbed"

    with pytest.raises(sdk.InvalidSDKRequestError, match="absolute"):
        sdk.attach_workspace(container_id="container-1", repo_root="testbed")
    with pytest.raises(sdk.InvalidSDKRequestError, match="normalized"):
        sdk.attach_workspace(container_id="container-1", repo_root="/tmp/../testbed")


def test_coding_toolset_returns_fresh_ordered_tools_with_eval_guards() -> None:
    tools = sdk.coding_toolset(
        require_process_isolation=True,
        allow_test_command_overrides=False,
        allow_file_creation=False,
    )
    assert tuple(type(tool) for tool in tools) == (
        BashTool,
        FileReadTool,
        FileWriteTool,
        ApplyPatchTool,
        RunTestsTool,
        GitDiffTool,
        GrepTool,
    )
    assert tools[0].require_process_isolation is True
    assert tools[2].allow_create is False
    assert tools[4].allow_runner_override is False
    assert tools[4].allow_extra_args is False
    assert tools[4].require_process_isolation is True
    assert sdk.coding_toolset()[0] is not sdk.coding_toolset()[0]


def test_workflow_request_normalizes_artifact_path() -> None:
    @sdk.workflow(name="sample", description="sample")
    async def sample(ctx, args):
        return None

    request = sdk.WorkflowRunRequest(
        workflow=sample,
        config=sdk.RuntimeConfig(model="model", provider="provider"),
        artifact_dir=Path("artifacts"),
    )
    assert request.artifact_dir == Path("artifacts")

    with pytest.raises(sdk.InvalidSDKRequestError, match="inputs keys"):
        sdk.WorkflowRunRequest(
            workflow=sample,
            config=sdk.RuntimeConfig(model="model", provider="provider"),
            inputs={1: "invalid"},
        )

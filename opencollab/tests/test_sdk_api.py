from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

import opencollab.sdk as sdk
import pytest
from opencollab.adapters.env import DockerWorkspaceEnvironment, Environment
from opencollab.adapters.env import ExecResult as AdapterExecResult
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool


def test_public_sdk_exports_versioned_authoring_and_runtime_surface() -> None:
    assert sdk.SDK_API_VERSION == 2
    expected = {
        "SDK_API_VERSION",
        "AgentRunBudget",
        "AgentRunLifecycleError",
        "AgentRunRequest",
        "AgentRunResult",
        "AgentRunTimeoutError",
        "ApplyPatchTool",
        "BashTool",
        "CommandResult",
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
        "WorkflowRunLifecycleError",
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
        "OpenCollabRuntime.run_agent": "(self, request: 'AgentRunRequest') -> 'AgentRunResult'",
        "OpenCollabRuntime.run_workflow": "(self, request: 'WorkflowRunRequest') -> 'WorkflowRunResult'",
        "RuntimeConfig.from_mapping": "(config: 'Mapping[str, Any]') -> 'RuntimeConfig'",
        "attach_workspace": (
            "(*, container_id: 'str', repo_root: 'str', command_prefix: "
            "'Callable[[str], str] | str | None' = None, timeout_returncode: 'int' = -1) -> "
            "'ExecutionEnvironment'"
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
        "OpenCollabRuntime.run_agent": str(inspect.signature(sdk.OpenCollabRuntime.run_agent)),
        "OpenCollabRuntime.run_workflow": str(inspect.signature(sdk.OpenCollabRuntime.run_workflow)),
        "RuntimeConfig.from_mapping": str(inspect.signature(sdk.RuntimeConfig.from_mapping)),
        **{name: str(inspect.signature(getattr(sdk, name))) for name in signatures if "." not in name},
    }
    assert actual_signatures == signatures
    assert tuple(field.name for field in fields(sdk.RunBudget)) == (
        "max_tokens",
        "timeout_seconds",
        "max_concurrency",
        "cleanup_timeout_seconds",
        "deadline_margin_seconds",
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
        "environment_workdir",
        "source_root",
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
    assert tuple(field.name for field in fields(sdk.AgentRunBudget)) == (
        "max_tokens",
        "max_steps",
        "timeout_seconds",
        "cleanup_timeout_seconds",
    )
    assert tuple(field.name for field in fields(sdk.AgentRunRequest)) == (
        "prompt",
        "config",
        "budget",
        "name",
        "system_prompt",
        "tools",
        "environment",
        "environment_workdir",
        "source_root",
        "workspace",
        "artifact_dir",
        "trace",
        "failure_mode",
        "llm",
    )
    assert tuple(field.name for field in fields(sdk.AgentRunResult)) == (
        "output",
        "outcome",
        "phase",
        "terminal_reason",
        "error_type",
        "error_message",
        "tokens_spent",
        "step_count",
        "artifact_dir",
        "transcript_path",
        "trace_path",
        "cleanup_quiesced",
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

    mapped = sdk.RuntimeConfig.from_mapping({"model": "model", "provider": "provider", "max_output_tokens": 4096})
    assert mapped.max_output_tokens == 4096

    with pytest.raises(sdk.InvalidSDKRequestError, match="failure_mode"):
        sdk.AgentRunRequest(prompt="task", config=config, failure_mode="ignore")


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


def test_request_validation_preserves_public_error_priority() -> None:
    @sdk.workflow(name="sample", description="sample")
    async def sample(ctx, args):
        return None

    config = sdk.RuntimeConfig(model="model", provider="provider")
    with pytest.raises(sdk.InvalidSDKRequestError, match="config"):
        sdk.WorkflowRunRequest(workflow=sample, config=object(), inputs=object())
    with pytest.raises(sdk.InvalidSDKRequestError, match="config"):
        sdk.AgentRunRequest(prompt="task", config=object(), budget=object(), tools=object())
    with pytest.raises(sdk.InvalidSDKRequestError, match="budget"):
        sdk.AgentRunRequest(prompt="task", config=config, budget=object(), tools=object())


def test_command_result_protocol_accepts_sdk_and_adapter_values() -> None:
    sdk_result = sdk.ExecResult(returncode=0, stdout="ok", stderr="")
    adapter_result = AdapterExecResult(returncode=0, stdout="ok", stderr="")

    assert isinstance(sdk_result, sdk.CommandResult)
    assert isinstance(adapter_result, sdk.CommandResult)


def test_environment_protocol_rejects_incomplete_nominal_subclasses() -> None:
    class IncompleteEnvironment(sdk.ExecutionEnvironment):
        pass

    with pytest.raises(TypeError, match="abstract"):
        IncompleteEnvironment()


def test_adapter_environment_revocation_is_synchronous_and_idempotent() -> None:
    environment = Environment()

    environment.revoke()
    environment.revoke()

    assert environment.revoked


def test_stable_capability_modules_have_only_public_exports() -> None:
    from opencollab.sdk import usage, workflows

    modules = (usage, workflows)
    assert all(name and not name.startswith("_") for module in modules for name in module.__all__)
    assert workflows.load_workflow_specs

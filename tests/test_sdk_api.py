"""Contract tests for the compact public Python API."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

import opencollab
import opencollab.sdk as sdk
from opencollab.environments import (
    Environment,
    attach_container,
    docker_environment,
    local_environment,
    worktree_environment,
)
from opencollab.tools import Tool, VerificationTool, builtin_tools
from opencollab.workflows import WorkflowContext


def test_root_and_sdk_export_one_small_surface() -> None:
    expected = ["OpenCollab", "RunError", "RunResult", "workflow"]
    assert opencollab.__version__ == "0.5.0"
    assert opencollab.__all__ == expected
    assert sdk.__all__ == expected
    assert all(getattr(opencollab, name) is getattr(sdk, name) for name in expected)

    retired = (
        "SDK_API_VERSION",
        "AgentRunRequest",
        "WorkflowRunRequest",
        "OpenCollabRuntime",
        "RuntimeConfig",
    )
    assert all(not hasattr(sdk, name) for name in retired)


def test_public_class_and_method_shapes_stay_lean() -> None:
    expected_parameters = {
        sdk.OpenCollab: (
            "workspace",
            "model",
            "provider",
            "api_key",
            "base_url",
            "config",
            "environment",
        ),
        sdk.OpenCollab.agent: (
            "self",
            "prompt",
            "tools",
            "budget",
            "max_steps",
            "steps",
            "timeout",
            "cleanup_timeout",
            "artifacts",
            "trace",
            "name",
            "system_prompt",
            "llm",
        ),
        sdk.OpenCollab.team: (
            "self",
            "prompt",
            "config",
            "budget",
            "timeout",
            "cleanup_timeout",
            "artifacts",
            "trace",
            "use_worktrees",
        ),
        sdk.OpenCollab.workflow: (
            "self",
            "flow",
            "inputs",
            "budget",
            "concurrency",
            "task_concurrency",
            "timeout",
            "max_steps",
            "system_prompt",
            "cleanup_timeout",
            "artifacts",
            "trace",
        ),
    }
    for target, expected in expected_parameters.items():
        assert tuple(inspect.signature(target).parameters) == expected

    assert tuple(field.name for field in fields(sdk.RunResult)) == (
        "output",
        "status",
        "reason",
        "tokens",
        "artifacts",
        "error",
        "metrics",
        "agent_failures",
    )


def test_run_result_has_one_status_and_one_error_model(tmp_path: Path) -> None:
    completed = sdk.RunResult(output="done", status="completed", tokens=4)
    assert completed.ok
    assert completed.raise_for_status() is completed

    failed = sdk.RunResult(
        output=None,
        status="failed",
        reason="provider failed",
        artifacts=tmp_path,
        error=ValueError("secret detail"),
    )
    assert not failed.ok
    assert "secret detail" not in repr(failed)
    with pytest.raises(sdk.RunError, match="provider failed") as captured:
        failed.raise_for_status()
    assert captured.value.result is failed


def test_workflow_decorator_is_minimal_but_keeps_explicit_metadata() -> None:
    @sdk.workflow
    async def draft(_ctx, inputs):
        """Draft the answer."""
        return inputs

    assert draft.__workflow_spec__.name == "draft"
    assert draft.__workflow_spec__.description == "Draft the answer."
    assert draft.__workflow_spec__.phases == ()

    @sdk.workflow(name="review", description="Review it", phases=["read"])
    async def review(_ctx, inputs):
        return inputs

    assert review.__workflow_spec__.name == "review"
    assert review.__workflow_spec__.description == "Review it"
    assert review.__workflow_spec__.phases == ("read",)


def test_advanced_capabilities_live_in_small_opt_in_modules() -> None:
    import opencollab.environments as environments
    import opencollab.tools as tools
    import opencollab.workflows as workflows

    assert tools.__all__ == [
        "BuiltinToolName",
        "Tool",
        "VerificationTool",
        "builtin_tools",
    ]
    assert environments.__all__ == [
        "Environment",
        "attach_container",
        "docker_environment",
        "local_environment",
        "worktree_environment",
    ]
    assert workflows.__all__ == ["WorkflowContext", "workflow"]
    assert Tool is not None
    assert VerificationTool is not None
    assert builtin_tools is not None
    assert Environment is not None
    assert WorkflowContext is not None
    assert not hasattr(environments, "LocalEnvironment")
    assert not hasattr(environments, "WorktreeEnvironment")
    assert not hasattr(environments, "DockerEnvironment")


def test_public_environment_contract_exposes_workspace_mapping_metadata() -> None:
    assert {
        "workspace",
        "host_workspace",
        "source_workspace",
        "local_filesystem",
    } <= set(Environment.__annotations__)
    assert callable(getattr(Environment, "setup"))


def test_public_workflow_context_keeps_research_strategy_private() -> None:
    parameters = inspect.signature(WorkflowContext.agent).parameters
    assert "enforcement_strength" not in parameters
    assert "commit_reserve" not in parameters
    assert "harvest_fallback" not in parameters


def test_environment_factories_preserve_caller_owned_lifecycle(tmp_path: Path) -> None:
    local = local_environment(tmp_path)
    worktree = worktree_environment(tmp_path)
    container = docker_environment("python:3.11-slim", worktree)

    assert local.workspace == str(tmp_path.resolve())
    assert worktree.source_workspace == str(tmp_path.resolve())
    assert container.source_workspace == str(tmp_path.resolve())
    assert container.process_isolated is True
    assert not local.revoked
    assert not worktree.revoked
    assert not container.revoked


async def test_public_environment_setup_has_one_shared_contract(tmp_path: Path) -> None:
    local = local_environment(tmp_path)
    worktree = worktree_environment(tmp_path)

    assert await local.setup() == str(tmp_path.resolve())
    with pytest.raises(ValueError, match="mount_dir"):
        await local.setup(str(tmp_path))
    with pytest.raises(ValueError, match="mount_dir"):
        await worktree.setup(str(tmp_path))


def test_attach_container_validates_non_owning_workspace() -> None:
    environment = attach_container(
        container_id="container-1",
        workspace="/testbed",
    )
    assert environment.workspace == "/testbed"

    with pytest.raises(ValueError, match="absolute"):
        attach_container(container_id="container-1", workspace="testbed")
    with pytest.raises(ValueError, match="normalized"):
        attach_container(container_id="container-1", workspace="/tmp/../testbed")
    with pytest.raises(ValueError, match="container_id"):
        attach_container(container_id=" container-1 ", workspace="/testbed")


def test_builtin_tools_are_fresh_ordered_and_headless_safe() -> None:
    first = builtin_tools(
        "bash",
        "file_write",
        "run_tests",
        allow_file_creation=False,
    )
    second = builtin_tools("bash", "file_write", "run_tests")

    assert tuple(tool.name for tool in first) == (
        "bash",
        "file_write",
        "run_tests",
    )
    assert all(left is not right for left, right in zip(first, second, strict=True))
    assert first[0].require_process_isolation is True
    assert first[1].allow_create is False
    assert first[2].require_process_isolation is True
    assert first[2].allow_runner_override is False
    assert first[2].allow_extra_args is False
    assert isinstance(first[2], VerificationTool)
    assert first[2].verified_targets == frozenset()


def test_builtin_tools_reject_unsupported_or_ambiguous_requests() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        builtin_tools("spawn_agent")
    with pytest.raises(ValueError, match="unique"):
        builtin_tools("grep", "grep")
    with pytest.raises(ValueError, match="headless"):
        builtin_tools("grep", headless="yes")
    with pytest.raises(ValueError, match="allow_file_creation"):
        builtin_tools("file_write", allow_file_creation=1)
    with pytest.raises(ValueError, match="mapping"):
        builtin_tools("grep", limits=[])
    with pytest.raises(ValueError, match="unselected"):
        builtin_tools("grep", limits={"bash": {"max_output_chars": 10}})
    with pytest.raises(ValueError, match="mapping"):
        builtin_tools("grep", limits={"grep": []})
    with pytest.raises(ValueError, match="unsupported keys"):
        builtin_tools("grep", limits={"grep": {"unknown": 10}})


def test_client_rejects_invalid_workspace_or_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        sdk.OpenCollab(tmp_path / "missing")
    with pytest.raises(TypeError, match="mapping"):
        sdk.OpenCollab(tmp_path, config=object())


def test_client_exposes_immutable_non_secret_configuration(tmp_path: Path) -> None:
    client = sdk.OpenCollab(
        tmp_path,
        model="public-model",
        provider="openai",
        api_key="private-key",  # pragma: allowlist secret
        base_url="https://private.example",
        config={
            "temperature": 0.7,
            "top_p": 0.8,
            "max_output_tokens": 4096,
            "thinking": True,
            "thinking_params": {
                "thinking": {
                    "type": "enabled",
                    "keep": ["all"],
                }
            },
        },
    )

    assert client.configuration["model"] == "public-model"
    assert client.configuration["provider"] == "openai"
    assert client.configuration["temperature"] == 0.7
    assert client.configuration["top_p"] == 0.8
    assert client.configuration["max_output_tokens"] == 4096
    assert client.configuration["thinking"] is True
    assert client.configuration["thinking_params"] == {
        "thinking": {
            "type": "enabled",
            "keep": ["all"],
        }
    }
    assert client.configuration["base_url_sha256"] == hashlib.sha256(
        b"https://private.example"
    ).hexdigest()
    assert "api_key" not in client.configuration
    assert "base_url" not in client.configuration
    assert "private-key" not in repr(client.configuration)
    assert "https://private.example" not in repr(client.configuration)
    copied_params = client.configuration["thinking_params"]
    copied_params["thinking"]["keep"].append("mutated")
    assert client.configuration["thinking_params"]["thinking"]["keep"] == ["all"]
    with pytest.raises(TypeError):
        client.configuration["model"] = "changed"

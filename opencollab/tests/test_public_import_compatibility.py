"""Regression tests for public names retained after module extraction."""

from __future__ import annotations

import importlib

from opencollab.application.async_timeout import abandon_on_timeout
from opencollab.application.scheduler_types import QueuedTeammateMessage
from opencollab.application.session import SessionRuntime
from opencollab.application.tool_execution import (
    abandon_on_timeout as tool_execution_abandon_on_timeout,
)
from opencollab.application.workflow import (
    abandon_on_timeout as workflow_abandon_on_timeout,
)
from opencollab.harness import swe_eval_records, swe_v1_remote_records
from opencollab.harness.evaluator import EvalResult, EvalTask
from opencollab.harness.evaluator_models import (
    EvalResult as ExtractedEvalResult,
)
from opencollab.harness.evaluator_models import EvalTask as ExtractedEvalTask
from opencollab.harness.swe_checkpoint import ENV_RECOVERY_PATCH_PREFIX
from opencollab.harness.swe_checkpoint_recovery import (
    ENV_RECOVERY_PATCH_PREFIX as EXTRACTED_ENV_RECOVERY_PATCH_PREFIX,
)
from opencollab.harness.swe_eval_decision import (
    row_patch_sha as decision_row_patch_sha,
)
from opencollab.harness.swe_eval_discovery import (
    row_patch_sha as discovery_row_patch_sha,
)
from opencollab.harness.swe_eval_records import row_patch_sha

from scripts import swe_v1_prolite_runner


def test_application_facades_reexport_timeout_helper() -> None:
    assert tool_execution_abandon_on_timeout is abandon_on_timeout
    assert workflow_abandon_on_timeout is abandon_on_timeout


def test_swe_eval_facades_reexport_patch_digest_helper() -> None:
    assert decision_row_patch_sha is row_patch_sha
    assert discovery_row_patch_sha is row_patch_sha


def test_remote_runner_reuses_shared_record_identity_helpers() -> None:
    names = (
        "prediction_patch",
        "row_task_id",
        "row_record_id",
        "row_explicit_patch_sha",
        "patch_sha",
        "row_patch_sha",
        "embedded_workflow_metric",
        "patch_sha_matches",
    )
    for name in names:
        assert getattr(swe_v1_remote_records, name) is getattr(swe_eval_records, name)


def test_checkpoint_facade_reexports_recovery_prefix() -> None:
    assert ENV_RECOVERY_PATCH_PREFIX == EXTRACTED_ENV_RECOVERY_PATCH_PREFIX
    assert ENV_RECOVERY_PATCH_PREFIX == "/tmp/opencollab-checkpoint-recovery-"


def test_evaluator_facade_reexports_extracted_models() -> None:
    assert EvalResult is ExtractedEvalResult
    assert EvalTask is ExtractedEvalTask


def test_added_runtime_fields_preserve_legacy_construction() -> None:
    message = QueuedTeammateMessage(
        from_aid=1,
        to_aid=2,
        summary="summary",
        content="content",
        xml="<message />",
    )
    runtime = SessionRuntime(
        state=object(),
        event_bus=object(),
        llm=object(),
        store=object(),
        tool_execution=object(),
        runner=object(),
        auto_save_path=None,
    )

    assert message.sent_at == ""
    assert runtime.auto_save_subscriber is None


def test_split_facades_retain_every_legacy_public_binding() -> None:
    expected = {
        "opencollab.harness.evaluator": {
            "Any",
            "LocalEnvironment",
            "dataclass",
            "shlex",
            "time",
        },
        "opencollab.harness.swe_checkpoint": {"dataclass", "hashlib"},
        "opencollab.harness.swe_eval_discovery": {"json"},
        "opencollab.bootstrap.workflow_runtime": {"importlib"},
        "scripts.swe_v1_prolite_runner": {"base64"},
    }

    for module_name, names in expected.items():
        module = importlib.import_module(module_name)
        assert not (names - set(vars(module))), module_name


def test_prolite_runner_retains_legacy_public_launcher_names() -> None:
    functions = {
        "run_checked",
        "load_shell_env",
        "token_from_values",
        "token_from_env_file",
        "proxy_env_file_from_ps",
        "get_proxy_token",
        "url_with_healthz",
        "local_http_ok",
        "remote_http_ok",
        "loopback_port",
        "loopback_url_with_port",
        "remote_forward_port_conflict",
        "stop_remote_proxy_tunnel",
        "cleanup_remote_proxy_tunnels",
        "start_remote_proxy_tunnel",
        "ensure_remote_proxy",
        "sync_runtime",
        "configure_run_paths",
        "terminate_remote_run",
        "terminate_local_process_group",
        "run_remote",
        "write_local_report",
        "main",
    }
    constants = {
        "DEFAULT_HOST",
        "DEFAULT_REMOTE_ROOT",
        "DEFAULT_BASE_RUN_DIR_PREFIX",
        "DEFAULT_MODEL_NAME",
        "DEFAULT_REPORT_JSON",
        "DEFAULT_REPORT_MD",
        "DEFAULT_PROXY_ENV_FILE",
        "DEFAULT_LOCAL_PROXY_BASE_URL",
        "REMOTE_HEALTH_SSH_TIMEOUT_FLOOR",
        "REMOTE_PROXY_TUNNELS",
        "REMOTE_RUNNER",
    }

    for name in functions | constants:
        assert hasattr(swe_v1_prolite_runner, name), name
    assert swe_v1_prolite_runner.loopback_port(
        "http://127.0.0.1",
        default=18788,
    ) == 18788
    assert "swe_v1_remote_runner" in swe_v1_prolite_runner.REMOTE_RUNNER
    compile(swe_v1_prolite_runner.REMOTE_RUNNER, "<REMOTE_RUNNER>", "exec")

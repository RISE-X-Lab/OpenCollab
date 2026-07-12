"""Regression tests for public names retained after module extraction."""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap

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


def test_safe_file_facade_retains_public_operations() -> None:
    facade = importlib.import_module("opencollab.adapters.safe_files")
    atomic = importlib.import_module("opencollab.adapters._safe_file_atomic")
    read_append = importlib.import_module(
        "opencollab.adapters._safe_file_read_append"
    )
    support = importlib.import_module("opencollab.adapters._safe_file_support")
    direct_bindings = {
        "ensure_directory_no_symlinks": support.ensure_directory_no_symlinks,
        "open_regular_text_append": read_append.open_regular_text_append,
        "read_regular_bytes": read_append.read_regular_bytes,
        "regular_handle_matches_path": read_append.regular_handle_matches_path,
        "regular_path_identity": read_append.regular_path_identity,
        "unlink_regular_file_durable": atomic.unlink_regular_file_durable,
        "write_locked_text": read_append.write_locked_text,
    }
    facade_wrappers = {
        "append_regular_text",
        "create_regular_bytes_atomic",
        "read_regular_text",
        "write_regular_bytes_atomic",
        "write_regular_file_atomic",
    }

    assert set(facade.__all__) == facade_wrappers | direct_bindings.keys()
    assert all(callable(getattr(facade, name)) for name in facade_wrappers)
    for name, implementation in direct_bindings.items():
        assert getattr(facade, name) is implementation


def test_safe_file_facade_composed_operations_use_current_bindings(
    tmp_path,
    monkeypatch,
) -> None:
    facade = importlib.import_module("opencollab.adapters.safe_files")
    read_calls = []

    def fake_read(path, *, max_bytes):
        read_calls.append((path, max_bytes))
        return b"sentinel"

    monkeypatch.setattr(facade, "read_regular_bytes", fake_read)
    assert facade.read_regular_text(tmp_path / "unused", max_bytes=17) == "sentinel"
    assert read_calls == [(tmp_path / "unused", 17)]

    write_calls = []

    def fake_write(path, writer, **kwargs):
        write_calls.append((path, writer, kwargs))

    original_write = facade.write_regular_file_atomic
    monkeypatch.setattr(facade, "write_regular_file_atomic", fake_write)
    facade.write_regular_bytes_atomic(tmp_path / "replace", b"replace")
    facade.create_regular_bytes_atomic(tmp_path / "create", b"create")
    assert [call[2]["create_only"] for call in write_calls] == [False, True]

    monkeypatch.setattr(facade, "write_regular_file_atomic", original_write)
    ensured = []
    monkeypatch.setattr(facade, "ensure_directory_no_symlinks", ensured.append)
    facade.write_regular_file_atomic(
        tmp_path / "output.bin",
        lambda handle: handle.write(b"x"),
        max_bytes=1,
    )
    assert ensured == [tmp_path]


def test_environment_facade_forwards_worktree_helper_rebinding_in_subprocess() -> None:
    probe = textwrap.dedent(
        """
        from opencollab.adapters import env
        from opencollab.adapters import _env_worktree_directory as directory
        from opencollab.adapters import _env_worktree_lifecycle as lifecycle

        parent_sentinel = object()
        blocking_sentinel = object()
        env._open_parent_dirfd = parent_sentinel
        env._run_owned_blocking_io = blocking_sentinel
        assert directory._open_parent_dirfd is parent_sentinel
        assert lifecycle._run_owned_blocking_io is blocking_sentinel
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


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

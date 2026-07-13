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


def test_application_facades_reexport_timeout_helper() -> None:
    assert tool_execution_abandon_on_timeout is abandon_on_timeout
    assert workflow_abandon_on_timeout is abandon_on_timeout


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
    expected = {"opencollab.bootstrap.workflow_runtime": {"importlib"}}

    for module_name, names in expected.items():
        module = importlib.import_module(module_name)
        assert not (names - set(vars(module))), module_name

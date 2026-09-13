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


def test_application_facades_reexport_timeout_helper() -> None:
    assert tool_execution_abandon_on_timeout is abandon_on_timeout
    assert workflow_abandon_on_timeout is abandon_on_timeout


def test_safe_file_facade_retains_public_operations() -> None:
    facade = importlib.import_module("opencollab.adapters.safe_files")
    operations = {
        "append_regular_text",
        "create_regular_bytes_atomic",
        "create_regular_bytes_atomic_at",
        "ensure_directory_no_symlinks",
        "open_directory_anchor",
        "open_regular_text_append",
        "read_regular_bytes",
        "read_regular_bytes_at",
        "read_regular_text",
        "read_regular_text_range_at",
        "unlink_regular_file_durable",
        "unlink_regular_file_durable_at",
        "write_locked_text",
        "write_regular_bytes_atomic",
        "write_regular_bytes_atomic_at",
        "write_regular_file_atomic",
        "write_regular_file_atomic_at",
    }

    assert operations <= set(facade.__all__)
    assert all(callable(getattr(facade, name)) for name in operations)


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


def test_workflow_runtime_retains_its_declared_public_bindings() -> None:
    module = importlib.import_module("opencollab.bootstrap.workflow_runtime")
    assert set(module.__all__) <= set(vars(module))
    assert "importlib" not in module.__all__

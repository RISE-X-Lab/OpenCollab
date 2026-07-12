"""Local execution environment."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import threading

from opencollab.adapters._env_base import Environment, ExecResult
from opencollab.adapters._env_config import (
    LOCAL_FILE_READ_LIMIT_BYTES,
    LOCAL_FILE_WRITE_LIMIT_BYTES,
    PROCESS_IO_JOIN_TIMEOUT_SECONDS,
    PROCESS_KILL_REAP_TIMEOUT_SECONDS,
    PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
    PROCESS_TERM_GRACE_SECONDS,
)
from opencollab.adapters._env_file_io import (
    _positive_file_size_limit,
    _positive_finite_timeout,
    _run_owned_blocking_io,
    _sync_create_temp_file,
    _sync_read_regular_file,
    _sync_unlink_file,
    _sync_write_regular_file,
    _TemporaryFileOwnership,
    _TemporaryFileReplacedError,
)
from opencollab.adapters._env_process import (
    _await_owned_operation,
    _OwnedProcessNotQuiesced,
    _OwnedProcessTimeout,
    _run_thread_owned_process,
    _ThreadProcessOwner,
)
from opencollab.adapters.retirement_registry import (
    registered_retirement_paths,
)
from opencollab.application.exception_notes import add_exception_note


class _ActiveProcessOperations:
    """Revoke and observe every command owned by one local environment."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[object, _ThreadProcessOwner | None] = {}
        self._lingering_owners: set[_ThreadProcessOwner] = set()
        self._revoked_owners: set[_ThreadProcessOwner] = set()
        self._revoked = False

    def begin(self) -> object:
        with self._lock:
            if self._revoked:
                raise RuntimeError("process operations have been revoked")
            self._lingering_owners = {
                owner for owner in self._lingering_owners if not owner.finished.is_set()
            }
            token = object()
            self._operations[token] = None
            return token

    def attach(self, token: object, owner: _ThreadProcessOwner) -> None:
        with self._lock:
            if token not in self._operations:
                raise RuntimeError("process operation ownership is unavailable")
            if self._operations[token] is not None:
                raise RuntimeError("process operation already has an owner")
            self._operations[token] = owner
            revoked = self._revoked
            if revoked:
                self._revoked_owners.add(owner)
        if revoked:
            owner.cancel()

    def finish(self, token: object) -> None:
        with self._lock:
            owner = self._operations.pop(token, None)
            if owner is not None and not owner.finished.is_set():
                self._lingering_owners.add(owner)
            if self._revoked and owner is not None:
                self._revoked_owners.add(owner)

    async def abort(self) -> None:
        with self._lock:
            self._revoked = True
            owners = {
                owner for owner in self._operations.values() if owner is not None
            }
            owners.update(self._lingering_owners)
            self._revoked_owners.update(owners)
        for owner in owners:
            owner.cancel()

        wait_bound = (
            _positive_finite_timeout(
                PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS,
                name="PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_TERM_GRACE_SECONDS,
                name="PROCESS_TERM_GRACE_SECONDS",
            )
            + _positive_finite_timeout(
                PROCESS_KILL_REAP_TIMEOUT_SECONDS,
                name="PROCESS_KILL_REAP_TIMEOUT_SECONDS",
            )
            + PROCESS_IO_JOIN_TIMEOUT_SECONDS * 5
            + 1.0
        )

        async def wait_for_every_operation() -> bool:
            deadline = asyncio.get_running_loop().time() + wait_bound
            while True:
                with self._lock:
                    current_owners = {
                        owner
                        for owner in self._operations.values()
                        if owner is not None
                    }
                    current_owners.update(self._lingering_owners)
                    self._revoked_owners.update(current_owners)
                    operations_pending = bool(self._operations)
                for current_owner in current_owners:
                    current_owner.cancel()
                if not operations_pending and all(
                    owner.finished.is_set() for owner in current_owners
                ):
                    return True
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.01, remaining))

        completed = await _await_owned_operation(
            wait_for_every_operation(),
            propagate_cancellation=True,
        )
        if not completed:
            raise _OwnedProcessNotQuiesced(
                "environment commands did not finish within the abort bound",
                cleanup_quiesced=False,
            )
        with self._lock:
            revoked_owners = tuple(self._revoked_owners)
        if any(
            not owner.finished.is_set() or not owner.result.cleanup_quiesced
            for owner in revoked_owners
        ):
            raise _OwnedProcessNotQuiesced(
                "environment command cleanup did not quiesce",
                cleanup_quiesced=False,
            )


class LocalEnvironment(Environment):
    """Direct OS execution — for interactive CLI use."""

    local_filesystem = True

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        workspace_fd = os.open(self.workspace, flags)
        try:
            opened = os.fstat(workspace_fd)
            current = os.stat(self.workspace, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise OSError("local workspace changed while recording ownership")
        except BaseException:
            os.close(workspace_fd)
            raise
        self._workspace_fd = workspace_fd
        self._workspace_identity = (opened.st_dev, opened.st_ino)
        self._workspace_lock = threading.Lock()
        self._temp_file_identities: dict[str, _TemporaryFileOwnership] = {}
        self._temp_identity_lock = threading.Lock()
        self._process_operations = _ActiveProcessOperations()

    def _verify_workspace_identity_locked(self) -> None:
        if self._workspace_fd < 0:
            raise RuntimeError("Local workspace handle has been closed.")
        try:
            opened = os.fstat(self._workspace_fd)
            current = os.stat(self.workspace, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise OSError("local workspace identity changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != self._workspace_identity
            or (current.st_dev, current.st_ino) != self._workspace_identity
        ):
            raise OSError("local workspace identity changed")

    def _acquire_workspace_handle(self) -> int:
        with self._workspace_lock:
            self._verify_workspace_identity_locked()
            return os.dup(self._workspace_fd)

    def _finish_workspace_operation(
        self,
        operation_fd: int,
        original: BaseException | None = None,
    ) -> None:
        close_error: BaseException | None = None
        try:
            os.close(operation_fd)
        except BaseException as exc:
            close_error = exc
        verification_error: BaseException | None = None
        try:
            with self._workspace_lock:
                self._verify_workspace_identity_locked()
        except BaseException as exc:
            verification_error = exc
        if original is not None:
            if close_error is not None:
                add_exception_note(
                    original,
                    "local workspace operation descriptor close failed: "
                    f"{type(close_error).__name__}: {close_error}",
                )
            if verification_error is not None:
                add_exception_note(
                    original,
                    "local workspace identity verification failed after operation: "
                    f"{type(verification_error).__name__}: {verification_error}",
                )
            return
        if close_error is not None:
            raise close_error
        if verification_error is not None:
            raise verification_error

    def _sync_release_owned_resources(self) -> None:
        errors: list[str] = []
        with self._temp_identity_lock:
            owned_files = list(self._temp_file_identities.items())
        for path, ownership in owned_files:
            discard_ownership = False
            try:
                _sync_unlink_file(path, ownership)
                discard_ownership = True
            except _TemporaryFileReplacedError as exc:
                discard_ownership = True
                errors.append(f"temporary file cleanup failed for {path}: {exc}")
                with ownership.lock:
                    if not ownership.closed:
                        try:
                            os.close(ownership.fd)
                        except BaseException as close_exc:
                            errors.append(
                                f"temporary file descriptor close failed for {path}: {close_exc}"
                            )
                        ownership.closed = True
            except BaseException as exc:
                errors.append(f"temporary file cleanup failed for {path}: {exc}")
            finally:
                if discard_ownership:
                    with self._temp_identity_lock:
                        if self._temp_file_identities.get(path) is ownership:
                            self._temp_file_identities.pop(path, None)
        with self._temp_identity_lock:
            pending_temp_files = bool(self._temp_file_identities)
        if not pending_temp_files:
            with self._workspace_lock:
                if self._workspace_fd >= 0:
                    try:
                        os.close(self._workspace_fd)
                    except BaseException as exc:
                        errors.append(f"workspace descriptor close failed: {exc}")
                    else:
                        self._workspace_fd = -1
        if errors:
            raise OSError("; ".join(errors))

    def _full_local_path(self, path: str) -> str:
        if not isinstance(path, str) or "\0" in path:
            raise ValueError("local file path must be text without NUL bytes")
        if os.path.isabs(path):
            return os.path.normpath(path)
        normalized = os.path.normpath(path)
        if normalized == ".." or normalized.startswith(f"..{os.sep}"):
            raise PermissionError(f"relative path escapes local workspace: {path}")
        return os.path.normpath(os.path.join(self.workspace, normalized))

    def _local_io_target(self, path: str) -> tuple[str, bool]:
        full = self._full_local_path(path)
        if os.path.isabs(path):
            return full, False
        return os.path.normpath(path), True

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        timeout_seconds = _positive_finite_timeout(timeout, name="timeout")
        self._ensure_active()
        operation_token = self._process_operations.begin()
        try:
            workspace_fd = self._acquire_workspace_handle()
            try:
                try:
                    result = await _run_thread_owned_process(
                        cmd,
                        shell=True,
                        cwd=None,
                        cwd_fd=workspace_fd,
                        timeout=timeout_seconds,
                        timeout_name="timeout",
                        owner_started=lambda owner: self._process_operations.attach(
                            operation_token,
                            owner,
                        ),
                    )
                    outcome = ExecResult(
                        returncode=result.returncode or 0,
                        stdout=result.stdout.decode("utf-8", errors="replace"),
                        stderr=result.stderr.decode("utf-8", errors="replace"),
                        stdout_truncated=result.stdout_dropped_bytes > 0,
                        stderr_truncated=result.stderr_dropped_bytes > 0,
                        stdout_dropped_bytes=result.stdout_dropped_bytes,
                        stderr_dropped_bytes=result.stderr_dropped_bytes,
                    )
                except _OwnedProcessTimeout as exc:
                    if not exc.cleanup_quiesced:
                        self._aborted = True
                    outcome = ExecResult(
                        returncode=-1,
                        stdout="",
                        stderr=f"Command timed out after {timeout_seconds:g}s",
                    )
                except _OwnedProcessNotQuiesced:
                    self._aborted = True
                    raise
                except asyncio.CancelledError as exc:
                    if getattr(exc, "cleanup_quiesced", True) is False:
                        self._aborted = True
                    raise
            except BaseException as original:
                self._finish_workspace_operation(workspace_fd, original)
                raise
            self._finish_workspace_operation(workspace_fd)
            return outcome
        finally:
            self._process_operations.finish(operation_token)

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        target, relative = self._local_io_target(path)
        limit = _positive_file_size_limit(
            LOCAL_FILE_READ_LIMIT_BYTES,
            name="LOCAL_FILE_READ_LIMIT_BYTES",
        )
        workspace_fd = self._acquire_workspace_handle()
        try:
            data = await _run_owned_blocking_io(
                _sync_read_regular_file,
                target,
                limit,
                workspace_fd if relative else None,
            )
        except BaseException as original:
            self._finish_workspace_operation(workspace_fd, original)
            raise
        self._finish_workspace_operation(workspace_fd)
        assert isinstance(data, bytes)
        return data.decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        target, relative = self._local_io_target(path)
        limit = _positive_file_size_limit(
            LOCAL_FILE_WRITE_LIMIT_BYTES,
            name="LOCAL_FILE_WRITE_LIMIT_BYTES",
        )
        if len(content) > limit:
            raise OSError(f"local file exceeds write limit of {limit} bytes: {path}")
        payload = content.encode("utf-8")
        if len(payload) > limit:
            raise OSError(f"local file exceeds write limit of {limit} bytes: {path}")
        workspace_fd = self._acquire_workspace_handle()
        try:
            await _run_owned_blocking_io(
                _sync_write_regular_file,
                target,
                payload,
                workspace_fd if relative else None,
            )
        except BaseException as original:
            self._finish_workspace_operation(workspace_fd, original)
            raise
        self._finish_workspace_operation(workspace_fd)

    async def registered_retirement_paths(self) -> tuple[str, ...]:
        """Return only framework-recorded tombstones that remain unchanged."""
        self._ensure_active()
        workspace_fd = self._acquire_workspace_handle()
        try:
            paths = await _run_owned_blocking_io(
                registered_retirement_paths,
                self.workspace,
                workspace_fd,
            )
        except BaseException as original:
            self._finish_workspace_operation(workspace_fd, original)
            raise
        self._finish_workspace_operation(workspace_fd)
        assert isinstance(paths, tuple)
        return paths

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        if "/" in prefix or "/" in suffix or "\0" in prefix or "\0" in suffix:
            raise ValueError("temporary file prefix and suffix must be path components")
        limit = _positive_file_size_limit(
            LOCAL_FILE_WRITE_LIMIT_BYTES,
            name="LOCAL_FILE_WRITE_LIMIT_BYTES",
        )
        payload = content.encode("utf-8")
        if len(payload) > limit:
            raise OSError(f"local temporary file exceeds write limit of {limit} bytes")
        workspace_fd = self._acquire_workspace_handle()
        def create_and_record_owned_file() -> str:
            path, identity = _sync_create_temp_file(
                tempfile.gettempdir(),
                prefix,
                suffix,
                payload,
            )
            try:
                with self._temp_identity_lock:
                    self._temp_file_identities[path] = identity
            except BaseException:
                _sync_unlink_file(path, identity)
                raise
            return path

        try:
            path = await _run_owned_blocking_io(create_and_record_owned_file)
        except BaseException as original:
            self._finish_workspace_operation(workspace_fd, original)
            raise
        self._finish_workspace_operation(workspace_fd)
        assert isinstance(path, str)
        return path

    async def remove_file(self, path: str) -> None:
        self._ensure_active()
        full = self._full_local_path(path)
        target, relative = self._local_io_target(path)
        workspace_fd = self._acquire_workspace_handle()
        with self._temp_identity_lock:
            identity = self._temp_file_identities.get(full)
        if identity is None:
            self._finish_workspace_operation(workspace_fd)
            raise OSError(
                f"refusing to remove local file without temporary ownership proof: {path}"
            )
        try:
            await _run_owned_blocking_io(
                _sync_unlink_file,
                target,
                identity,
                workspace_fd if relative else None,
            )
        except BaseException as original:
            self._finish_workspace_operation(workspace_fd, original)
            raise
        self._finish_workspace_operation(workspace_fd)
        if identity is not None:
            with self._temp_identity_lock:
                if self._temp_file_identities.get(full) == identity:
                    self._temp_file_identities.pop(full, None)

    async def cleanup(self) -> None:
        await self._process_operations.abort()
        await _run_owned_blocking_io(self._sync_release_owned_resources)

    async def abort(self) -> None:
        await super().abort()
        await self.cleanup()

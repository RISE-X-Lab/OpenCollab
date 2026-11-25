"""Guest entry point for durable SSH operations using the installed package."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import fcntl
import json
import math
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opencollab import __version__
from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_process import ProcessCleanupError
from opencollab.adapters._vm_archive import pack, publish, unpack
from opencollab.adapters.safe_anchored_files import (
    create_regular_bytes_atomic_at,
    open_directory_anchor,
    read_regular_bytes_at,
    unlink_regular_file_durable_at,
    write_regular_bytes_atomic_at,
)
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.vm_config import validate_vm_path

_MAX_MESSAGE = 32 * 1024 * 1024
_FRAME = b"OCVM1\n"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _write(directory: Path, name: str, value: Any) -> None:
    fd = open_directory_anchor(directory)
    try:
        write_regular_bytes_atomic_at(fd, directory, name, json.dumps(value).encode(), max_bytes=_MAX_MESSAGE)
    finally:
        os.close(fd)


def _read(directory: Path, name: str) -> Any:
    fd = open_directory_anchor(directory)
    try:
        return json.loads(read_regular_bytes_at(fd, directory, name, max_bytes=_MAX_MESSAGE))
    finally:
        os.close(fd)


def _directory(value: str) -> Path:
    validate_vm_path(value)
    current = Path("/")
    for component in Path(value).parts[1:]:
        current /= component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        fd = open_directory_anchor(current)
        os.close(fd)
    if current.stat().st_uid != os.getuid() or current.stat().st_mode & 0o077:
        raise PermissionError("VM state directory must be private and owned by the SSH account")
    return current


def _identity(workspace: str) -> dict[str, Any]:
    fd = open_directory_anchor(workspace)
    try:
        info = os.fstat(fd)
        return {"workspace": os.path.realpath(workspace), "device": info.st_dev, "inode": info.st_ino,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    finally:
        os.close(fd)


def _process(pid: int) -> tuple[int, int, bytes]:
    value = Path(f"/proc/{pid}/stat").read_bytes()
    fields = value[value.rindex(b")") + 1:].split()
    return int(fields[1]), int(fields[19]), fields[0]


def _status(directory: Path) -> dict[str, Any]:
    try:
        return _read(directory, "result.json")
    except FileNotFoundError:
        pass
    try:
        started = _read(directory, "started.json")
    except FileNotFoundError:
        request = _read(directory, "request.json")
        if request["action"] == "import" and not (directory / "upload.json").exists():
            with (directory / "upload.lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"status": "uploading"}
            return {"status": "upload_incomplete"}
        return {"status": "preparing"}
    try:
        live = (_process(started["pid"])[1] == started["started"]
                and Path("/proc/sys/kernel/random/boot_id").read_text().strip() == started["boot_id"])
    except (FileNotFoundError, ProcessLookupError):
        live = False
    return {"status": "running" if live else "unknown", "owner": started,
            "lock_acquired": (directory / "lock.json").exists()}


def _descendants() -> dict[int, int]:
    processes = {}
    for entry in os.listdir("/proc"):
        if entry.isdecimal():
            try:
                processes[int(entry)] = _process(int(entry))
            except (FileNotFoundError, ProcessLookupError):
                continue
    owned = {os.getpid()}
    while True:
        children = {pid for pid, (parent, _started, _state) in processes.items() if parent in owned}
        if children <= owned:
            break
        owned |= children
    return {pid: processes[pid][1] for pid in owned if pid != os.getpid()}


async def _stop_descendants() -> None:
    deadline = time.monotonic() + 4.0
    while True:
        children = _descendants()
        for pid, started in children.items():
            try:
                fd = os.pidfd_open(pid)
                try:
                    if _process(pid)[1] == started:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except (FileNotFoundError, ProcessLookupError):
                pass
        while True:
            try:
                pid, _status_code = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        if not _descendants():
            return
        if time.monotonic() >= deadline:
            raise ProcessCleanupError("VM operation descendants did not quiesce")
        await asyncio.sleep(0.02)


async def _perform(request: dict[str, Any], directory: Path) -> Any:
    if _identity(request["workspace"]) != request["identity"]:
        raise OSError("VM workspace or guest boot identity changed")
    env = LocalEnvironment(request["workspace"])
    info = os.fstat(env._root_fd())
    if (info.st_dev, info.st_ino) != (request["identity"]["device"], request["identity"]["inode"]):
        await env.cleanup()
        raise OSError("VM workspace identity changed while opening it")
    action = request["action"]
    args = request.get("args", {})
    try:
        if action == "lock":
            lock_root = _directory(f"/tmp/opencollab-vm-locks-{os.getuid()}")
            identity = request["identity"]
            with (lock_root / f"{identity['device']}-{identity['inode']}.lock").open("a+b") as lock:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("timed out acquiring VM workspace edit lock")
                        await asyncio.sleep(0.02)
                _write(directory, "lock.json", True)
                await asyncio.Future()
        if action == "import":
            destination = Path(validate_vm_path(args["destination"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging_parent = destination if args["directory"] and destination.is_dir() else destination.parent
            staging = Path(tempfile.mkdtemp(prefix=".oc-vm-import-", dir=staging_parent))
            _write(directory, "staging.json", str(staging))
            payload = unpack(directory / "archive.tar", staging / "contents", directory=args["directory"])
            publish(payload, destination, directory=args["directory"])
            shutil.rmtree(staging)
            return None
        if action == "exec":
            env.workspace = f"/proc/{os.getpid()}/fd/{env._root_fd()}"
            result = asdict(await env.exec_cmd(args["cmd"], math.inf if args["timeout"] is None else args["timeout"],
                           input_bytes=base64.b64decode(args["input"]) if args["input"] is not None else None))
            result["stdout_bytes"] = (base64.b64encode(result["stdout_bytes"]).decode()
                                      if result["stdout_bytes"] is not None else None)
            return result
        if action == "read":
            return await env.read_file(args["path"])
        if action == "resolve_search_path":
            return SandboxInterceptor(env.workspace).check_path(args["path"])
        if action == "range":
            return asdict(await env.read_text_range(**args))
        if action == "write":
            return await env.write_file_with_change(args["path"], args["content"])
        if action == "mutable_temporary":
            if not re.fullmatch(r"[A-Za-z0-9._-]+", args["name"]):
                raise ValueError("invalid mutable temporary name")
            temporary = directory / "temporary"
            temporary.mkdir(mode=0o700)
            fd = open_directory_anchor(temporary)
            try:
                info = os.fstat(fd)
                _write(directory, "temporary.json", [info.st_dev, info.st_ino])
                create_regular_bytes_atomic_at(fd, temporary, args["name"], args["content"].encode(),
                                              max_bytes=ENV_FILE_WRITE_LIMIT_BYTES)
            finally:
                os.close(fd)
            return str(temporary / args["name"])
        if action == "remove_mutable_temporary":
            operation_id = args["operation_id"]
            if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
                raise ValueError("invalid mutable temporary operation")
            owner = directory.parent / operation_id
            parent = open_directory_anchor(owner)
            try:
                try:
                    expected = _read(owner, "temporary.json")
                    fd = open_directory_anchor(owner / "temporary")
                except FileNotFoundError:
                    if os.path.lexists(owner / "temporary"):
                        raise OSError("mutable temporary ownership is incomplete") from None
                    return None
                try:
                    info = os.fstat(fd)
                    if [info.st_dev, info.st_ino] != expected:
                        raise OSError("mutable temporary directory was replaced")
                    for name in os.listdir(fd):
                        mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                        if stat.S_ISDIR(mode):
                            shutil.rmtree(f"/proc/self/fd/{fd}/{name}")
                        else:
                            os.unlink(name, dir_fd=fd)
                    current = os.stat("temporary", dir_fd=parent, follow_symlinks=False)
                    if [current.st_dev, current.st_ino] != expected:
                        raise OSError("mutable temporary directory changed during cleanup")
                    os.rmdir("temporary", dir_fd=parent)
                    os.fsync(parent)
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
            return None
        if action in {"temporary", "remove"}:
            fd = open_directory_anchor(env.workspace)
            try:
                if action == "temporary":
                    create_regular_bytes_atomic_at(fd, env.workspace, args["path"], args["content"].encode(),
                                                   max_bytes=ENV_FILE_WRITE_LIMIT_BYTES)
                    info = os.stat(args["path"], dir_fd=fd, follow_symlinks=False)
                    return [info.st_dev, info.st_ino]
                unlink_regular_file_durable_at(fd, env.workspace, args["path"],
                                               expected_identity=tuple(args["identity"]))
                return None
            finally:
                os.close(fd)
        raise ValueError(f"unknown VM operation {action}")
    finally:
        await env.cleanup()


async def _work(directory: Path) -> None:
    request = _read(directory, "request.json")
    # Each worker is a Linux subreaper so double-forked children remain owned
    # after setsid or process-group changes. Its lock spans command and cleanup.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot establish VM child subreaper")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise OSError("VM execution requires Linux pidfd support")
    started = {"pid": os.getpid(), "started": _process(os.getpid())[1],
               "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    _write(directory, "started.json", started)
    task = asyncio.create_task(_perform(request, directory))
    cancelled = False
    try:
        while not task.done():
            if (directory / "cancel.json").exists():
                task.cancel()
                cancelled = True
                break
            await asyncio.sleep(0.05)
        value = await task
        result = {"status": "completed", "value": value}
    except asyncio.CancelledError:
        result = {"status": "cancelled"}
    except BaseException as exc:
        result = {"status": "failed", "error": type(exc).__name__, "message": str(exc)}
    try:
        await _stop_descendants()
    except BaseException as cleanup_error:
        failed_cancel = (directory / "cancel.json").stat().st_mtime_ns if (directory / "cancel.json").exists() else None
        while True:
            _write(directory, "result.json", {"status": "cleanup_failed", "error": type(cleanup_error).__name__,
                                              "message": str(cleanup_error), "descendants": _descendants()})
            while True:
                await asyncio.sleep(0.1)
                cancel_path = directory / "cancel.json"
                current = cancel_path.stat().st_mtime_ns if cancel_path.exists() else None
                if current != failed_cancel:
                    failed_cancel = current
                    break
            try:
                await _stop_descendants()
            except BaseException:
                continue
            result = {"status": "cancelled"}
            break
    if cancelled and result["status"] == "completed":
        result = {"status": "cancelled"}
    result["action"] = request["action"]
    result["args"] = {"path": request["args"]["path"]} if request["action"] in {"temporary", "remove"} else {}
    _write(directory, "result.json", result)


def _worker(directory: Path) -> None:
    with (directory / "worker.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # A missing terminal result after a claimed execution is ambiguous.
        # Keep that evidence for recovery instead of repeating side effects.
        if (directory / "started.json").exists() or (directory / "result.json").exists():
            return
        try:
            asyncio.run(_work(directory))
        except BaseException as exc:
            _write(directory, "result.json", {"status": "failed", "error": type(exc).__name__, "message": str(exc)})


def _prepare(root: Path, request: dict[str, Any]) -> Path:
    operation = request["operation_id"]
    if not isinstance(operation, str) or not _ID.fullmatch(operation):
        raise ValueError("invalid VM operation ID")
    directory = root / operation
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    fd = open_directory_anchor(directory)
    os.close(fd)
    with (directory / "submit.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = _read(directory, "request.json")
        except FileNotFoundError:
            _write(directory, "request.json", request["request"])
        else:
            if previous != request["request"]:
                raise ValueError("VM operation ID is already bound to another request")
    return directory


def _read_exact(length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = sys.stdin.buffer.read(length - len(result))
        if not chunk:
            raise EOFError("VM upload ended before its completion frame")
        result.extend(chunk)
    return bytes(result)


def _upload(root: Path, request: dict[str, Any]) -> Any:
    directory = _prepare(root, request)
    with (directory / "upload.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = _status(directory)
        receiving = status["status"] in {"preparing", "uploading", "upload_incomplete"}
        output = (directory / "archive.partial").open("wb") if receiving else None
        try:
            while True:
                length = struct.unpack("!I", _read_exact(4))[0]
                if length == 0:
                    break
                if length > 65536:
                    raise ValueError("VM upload frame exceeds transfer limit")
                chunk = _read_exact(length)
                if output is not None:
                    output.write(chunk)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
                output.close()
                output = None
                os.replace(directory / "archive.partial", directory / "archive.tar")
                _write(directory, "upload.json", True)
        finally:
            if output is not None:
                output.close()
    return _rpc(root, request)


class _ArchiveOutput:
    def write(self, value: bytes) -> int:
        for offset in range(0, len(value), 65536):
            chunk = value[offset:offset + 65536]
            sys.stdout.buffer.write(struct.pack("!I", len(chunk)))
            sys.stdout.buffer.write(chunk)
        return len(value)


def _response(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(_FRAME + json.dumps(value).encode() + b"\n")
    sys.stdout.buffer.flush()


def _rpc(root: Path, request: dict[str, Any]) -> Any:
    action = request["method"]
    if action == "setup":
        if sys.platform != "linux":
            raise OSError("VM environments require a Linux guest")
        identity = _identity(request["workspace"])
        try:
            previous = _read(root, "environment.json")
        except FileNotFoundError:
            _write(root, "environment.json", identity)
        else:
            if previous != identity:
                raise OSError("VM workspace or guest boot identity changed")
        return {"identity": identity, "version": __version__}
    operation = request["operation_id"]
    if not isinstance(operation, str) or not _ID.fullmatch(operation):
        raise ValueError("invalid VM operation ID")
    directory = root / operation
    if action == "start":
        directory = _prepare(root, request)
        if (request["request"]["action"] == "import" and not (directory / "upload.json").exists()
                and _status(directory)["status"] not in {"cancelled", "completed", "failed"}):
            raise OSError("VM upload has no completed archive")
        if _status(directory)["status"] == "preparing":
            subprocess.Popen([sys.executable, "-m", "opencollab.adapters._vm_remote", "worker", str(directory)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    fd = open_directory_anchor(directory)
    os.close(fd)
    if action == "cancel":
        _write(directory, "cancel.json", True)
        with (directory / "worker.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                if not (directory / "started.json").exists() and not (directory / "result.json").exists():
                    _write(directory, "result.json", {"status": "cancelled"})
    if action not in {"start", "query", "cancel"}:
        raise ValueError("unknown VM RPC method")
    return _status(directory)


def _main() -> None:
    if sys.argv[1] == "worker":
        _worker(Path(sys.argv[2]))
        return
    try:
        if len(sys.argv) != 4 or sys.argv[1] not in {"rpc", "upload", "download"} or not _ID.fullmatch(sys.argv[3]):
            raise ValueError("invalid VM helper arguments")
        root = _directory(f"{validate_vm_path(sys.argv[2])}/{sys.argv[3]}")
        if sys.argv[1] == "upload":
            payload = sys.stdin.buffer.readline(_MAX_MESSAGE + 1)
        else:
            payload = sys.stdin.buffer.read(_MAX_MESSAGE + 1)
        if len(payload) > _MAX_MESSAGE:
            raise ValueError("VM request exceeds message limit")
        request = json.loads(payload)
        if sys.argv[1] == "download":
            if _identity(request["workspace"]) != request["identity"]:
                raise OSError("VM workspace or guest boot identity changed")
            _response({"ok": True, "stream": True})
            pack(request["source"], _ArchiveOutput(), directory=request["directory"])
            sys.stdout.buffer.write(struct.pack("!I", 0))
            return
        value = _upload(root, request) if sys.argv[1] == "upload" else _rpc(root, request)
        response = {"ok": True, "value": value}
    except Exception as exc:
        response = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    _response(response)


if __name__ == "__main__":
    _main()

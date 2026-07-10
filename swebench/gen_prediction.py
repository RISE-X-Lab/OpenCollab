"""Generate a SWE-bench prediction with an OpenCollab agent.

Host-runnable bridge between the OpenCollab agent framework and the official
SWE-bench evaluation harness. For one SWE-bench instance it:

  1. starts the official ``sweb.eval`` image as a container (repo baked at
     /testbed, deps installed in the ``testbed`` conda env),
  2. runs a single OpenCollab agent inside it (edits + can run tests),
  3. captures ``git diff`` as the model patch,
  4. appends one ``{instance_id, model_name_or_path, model_patch}`` line to a
     predictions JSONL.

Grade the result with the official harness, e.g.::

    cd /home/xuzhenhua/swebench-eval
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-opencollab.jsonl -i sympy__sympy-20590 \
        -id oc-kimi --cache_level env --report_dir reports

Run with the OpenCollab venv (it must import ``opencollab``)::

    opencollab/.venv/bin/python swebench/gen_prediction.py \
        --instance-file /home/xuzhenhua/swebench-eval/instance_sympy-20590.json \
        --output /home/xuzhenhua/swebench-eval/predictions-opencollab.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import math
import operator
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

# Make the opencollab package importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "opencollab"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.adapters.tools.bash import BashTool  # noqa: E402
from opencollab.adapters.tools.fs import (  # noqa: E402
    FileReadTool,
    FileWriteTool,
    GrepTool,
)
from opencollab.adapters.trace import Tracer  # noqa: E402
from opencollab.application.async_timeout import (  # noqa: E402
    CallerTimeoutError,
    abandon_on_timeout,
    run_with_bounded_shutdown,
)
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.bootstrap.container import (  # noqa: E402
    agent_save_path,
    build_session,
    make_run_dir,
)
from opencollab.domain.agent import Agent  # noqa: E402
from opencollab.domain.session import SessionPhase  # noqa: E402
from opencollab.harness.swe_eval_records import (  # noqa: E402
    MAX_JSONL_SCAN_BYTES,
    open_regular_binary,
    read_bounded_json,
)

DOCKER_WORKDIR = "/testbed"
# Activate the testbed conda env so the agent's `python`/tests see the repo deps.
_ACTIVATE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true"
MAX_EXTRACTED_PATCH_BYTES = 8 * 1024 * 1024
MAX_STATUS_DIAGNOSTIC_BYTES = 64 * 1024
MAX_CAPTURED_STDERR_BYTES = 64 * 1024
CONTAINER_OWNER_SCHEMA_VERSION = 1
CONTAINER_OWNER_LABEL = "opencollab.harness.owner-token"
PENDING_OUTPUT_SCHEMA_VERSION = 1
MAX_PENDING_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_JSONL_SCAN_LINE_BYTES = 64 * 1024 * 1024
MAX_COMPATIBILITY_MARKER_BYTES = 4096
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_OWNER_RECORD_BYTES = 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = MAX_JSONL_SCAN_BYTES
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_CANCELLATION_GRACE_SECONDS = 2.0
_MISSING_CONTAINER_RE = re.compile(
    r"(?:no such (?:container|object)|not found)", re.IGNORECASE
)

_BOUNDED_CAPTURE_SCRIPT = r"""
import os
import selectors
import signal
import subprocess
import sys
import time


def process_group_exists(pgid):
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def wait_for_group_exit(pgid, timeout):
    deadline = time.monotonic() + timeout
    while process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def terminate_owned_group(proc):
    try:
        leader_reaped = proc.poll() is not None
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        if not leader_reaped:
            try:
                proc.wait(timeout=0.25)
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
        group_gone = wait_for_group_exit(proc.pid, 0.25)
        if leader_reaped and group_gone:
            return True

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        if not leader_reaped:
            try:
                proc.wait(timeout=1.0)
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
        group_gone = wait_for_group_exit(proc.pid, 1.0)
        return leader_reaped and group_gone
    except BaseException:
        return False

stdout_limit = int(sys.argv[1])
stderr_limit = int(sys.argv[2])
label = sys.argv[3]
proc = subprocess.Popen(
    sys.argv[4:],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)
selector = selectors.DefaultSelector()
selector.register(proc.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
selector.register(proc.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
captured = {"stdout": bytearray(), "stderr": bytearray()}
overflow = ""
while selector.get_map() and not overflow:
    for key, _events in selector.select(timeout=0.1):
        stream_name, limit = key.data
        chunk = os.read(key.fileobj.fileno(), 65536)
        if not chunk:
            selector.unregister(key.fileobj)
            continue
        target = captured[stream_name]
        remaining = max(0, limit - len(target))
        target.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflow = stream_name
            break
    if not overflow and proc.poll() is not None and process_group_exists(proc.pid):
        if not terminate_owned_group(proc):
            sys.stderr.buffer.write(captured["stderr"])
            sys.stderr.write("bounded capture process group did not quiesce\n")
            raise SystemExit(125)

if overflow:
    cleanup_quiesced = terminate_owned_group(proc)
    sys.stderr.buffer.write(captured["stderr"])
    sys.stderr.write(f"{label} {overflow} exceeded its byte limit\n")
    if not cleanup_quiesced:
        sys.stderr.write("bounded capture process group did not quiesce\n")
        raise SystemExit(125)
    raise SystemExit(86)

returncode = proc.wait()
if process_group_exists(proc.pid) and not terminate_owned_group(proc):
    sys.stderr.buffer.write(captured["stderr"])
    sys.stderr.write("bounded capture process group did not quiesce\n")
    raise SystemExit(125)
sys.stderr.buffer.write(captured["stderr"])
if returncode != 0:
    raise SystemExit(returncode)
sys.stdout.buffer.write(captured["stdout"])
""".strip()

AGENT_PROMPT = """\
You are an autonomous software engineer fixing a real bug in a Python repository.
The repository is checked out at /testbed and all dependencies are installed.

Rules:
- Explore briefly to find the root cause (a few grep/file_read calls), then ACT.
- As soon as you know the fix, APPLY it with the file_write tool (str_replace
  mode is best for a targeted edit). Diagnosing is not enough — you MUST edit
  the source file. Do not keep exploring once the cause is clear.
- Make the smallest correct change to the SOURCE code that fixes the issue.
- Do NOT edit test files — your fix is graded against the project's own tests.
- After editing, verify with a quick Python snippet that the reported behavior
  is fixed, then stop.
- Do NOT run `git commit`. Just leave your edits in the working tree.
"""


def unique_container_name(prefix: str, instance_id: str) -> str:
    """Return an ASCII Docker name without embedding attacker-controlled text."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix) is None:
        raise ValueError("container name prefix is unsafe")
    validated = validate_instance_id(instance_id)
    digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()[:12]
    suffix = uuid.uuid4().hex[:16]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", validated).strip(".-")
    slug = slug or "instance"
    max_slug_chars = max(1, 63 - len(prefix) - len(digest) - len(suffix) - 2)
    return f"{prefix}{slug[:max_slug_chars]}-{digest}-{suffix}"


def validate_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows_path = PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows_path.is_absolute()
        or "/" in value
        or "\\" in value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise ValueError("instance_id must be one safe path component")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_INSTANCE_ID_BYTES:
        raise ValueError("instance_id exceeds its UTF-8 byte limit")
    return value


def _stable_docker_component(value: str, *, max_chars: int = 96) -> str:
    """Map arbitrary valid text to a stable lowercase Docker name component."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip(".-_")
    slug = slug or "instance"
    safe_unchanged = slug == value and len(value) <= max_chars
    if safe_unchanged:
        return value
    slug = slug[: max(1, max_chars - len(digest) - 1)].rstrip(".-_") or "instance"
    return f"{slug}-{digest}"


def default_container_image(arch: str, instance_id: str) -> str:
    validated = validate_instance_id(instance_id)
    arch_component = _stable_docker_component(str(arch), max_chars=32)
    instance_component = _stable_docker_component(validated)
    return f"sweb.eval.{arch_component}.{instance_component}:latest"


def _docker_timeout_from_env() -> float:
    raw = os.environ.get("OPENCOLLAB_DOCKER_TIMEOUT", "60").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"OPENCOLLAB_DOCKER_TIMEOUT must be a positive number, got {raw!r}"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"OPENCOLLAB_DOCKER_TIMEOUT must be a positive number, got {raw!r}"
        )
    return timeout


def validate_generation_limits(
    *,
    max_steps: object,
    budget: object,
    timeout: object,
) -> tuple[int, int, float]:
    values: dict[str, int] = {}
    for name, value in (("--max-steps", max_steps), ("--budget", budget)):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive integer")
        try:
            integer = operator.index(value)
        except TypeError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if integer <= 0:
            raise ValueError(f"{name} must be a positive integer")
        values[name] = integer
    if isinstance(timeout, bool):
        raise ValueError("--timeout must be a positive finite number")
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("--timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("--timeout must be a positive finite number")
    return values["--max-steps"], values["--budget"], timeout_seconds


def _docker(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = _docker_timeout_from_env()
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _check_docker(res: subprocess.CompletedProcess, action: str) -> None:
    if res.returncode == 0:
        return
    detail = (res.stderr or res.stdout).strip()
    raise RuntimeError(f"{action} failed (exit {res.returncode}): {detail}")


def _container_owner_label_state(reference: str, owner_token: str) -> str:
    try:
        result = _docker(
            "inspect",
            "--type",
            "container",
            "--format",
            f'{{{{ index .Config.Labels "{CONTAINER_OWNER_LABEL}" }}}}',
            reference,
            timeout=30,
        )
    except BaseException:
        return "unknown"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if _MISSING_CONTAINER_RE.search(detail) is not None:
            return "absent"
        return "unknown"
    label = result.stdout.strip()
    if not label or "\n" in label or "\r" in label:
        return "foreign"
    return "matching" if label == owner_token else "foreign"


def _remove_labeled_container(
    reference: str,
    owner_token: str,
    *,
    foreign_proves_absence: bool,
) -> bool:
    state = _container_owner_label_state(reference, owner_token)
    if state == "absent":
        return True
    if state == "foreign":
        return foreign_proves_absence
    if state != "matching":
        return False
    return remove_container(reference)


def _require_creation_cleanup(
    reference: str,
    owner_token: str,
    cause: BaseException,
    *,
    foreign_proves_absence: bool,
) -> None:
    if _remove_labeled_container(
        reference,
        owner_token,
        foreign_proves_absence=foreign_proves_absence,
    ):
        return
    raise RuntimeError(
        "container creation failed and owner-label cleanup could not be proven; "
        "no unverified container was removed"
    ) from cause


def start_container(
    image: str,
    name: str,
    owner_token: str | None = None,
) -> str:
    owner_token = owner_token or uuid.uuid4().hex
    if re.fullmatch(r"[0-9a-f]{32}", owner_token) is None:
        raise ValueError("container owner token must be 32 lowercase hex characters")
    try:
        res = _docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{CONTAINER_OWNER_LABEL}={owner_token}",
            "--entrypoint",
            "",
            image,
            "tail",
            "-f",
            "/dev/null",
        )
    except BaseException as exc:
        _require_creation_cleanup(
            name,
            owner_token,
            exc,
            foreign_proves_absence=True,
        )
        raise
    if res.returncode != 0:
        error = RuntimeError(f"docker run failed: {res.stderr.strip()}")
        _require_creation_cleanup(
            name,
            owner_token,
            error,
            foreign_proves_absence=True,
        )
        raise error
    cid = res.stdout.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{12,64}", cid) is None:
        error = RuntimeError("docker run returned an invalid container id")
        _require_creation_cleanup(
            name,
            owner_token,
            error,
            foreign_proves_absence=True,
        )
        raise error
    try:
        ensure_workdir = _docker(
            "exec", cid, "bash", "-lc",
            """
set -e
if [ -e /testbed/.git ]; then
  exit 0
fi
if { [ -e /testbed ] || [ -L /testbed ]; } && [ ! -e /testbed/.git ]; then
  rm -rf /testbed
fi
if [ ! -e /testbed ]; then
  for d in /app /workspace /repo /src; do
    if [ -e "$d/.git" ]; then
      ln -s "$d" /testbed
      exit 0
    fi
  done
  found=$(find / -maxdepth 3 -name .git 2>/dev/null | head -1 || true)
  if [ -n "$found" ]; then
    ln -s "$(dirname "$found")" /testbed
    exit 0
  fi
fi
echo "unable to prepare /testbed: no repository checkout found" >&2
exit 2
""",
        )
        _check_docker(ensure_workdir, "docker /testbed workdir setup")
        # Repo is owned by root in the image; allow git to operate on it.
        safe_dir = _docker("exec", cid, "bash", "-lc",
                           f"git config --global --add safe.directory {DOCKER_WORKDIR}")
        _check_docker(safe_dir, "docker git safe.directory setup")
    except BaseException as exc:
        _require_creation_cleanup(
            cid,
            owner_token,
            exc,
            foreign_proves_absence=False,
        )
        raise
    return cid


def _container_is_absent(reference: str) -> bool:
    try:
        result = _docker("inspect", "--type", "container", reference, timeout=30)
    except Exception as exc:  # noqa: BLE001 - absence must be positively verified
        print(f"  warning: container absence check failed for {reference}: {exc!r}")
        return False
    detail = (result.stderr or result.stdout or "").strip()
    return result.returncode != 0 and _MISSING_CONTAINER_RE.search(detail) is not None


def remove_container(reference: str) -> bool:
    """Remove a container and return only after Docker proves it is absent."""
    try:
        result = _docker("rm", "-f", reference, timeout=30)
    except Exception as exc:  # noqa: BLE001 - retain ownership on unknown teardown
        print(f"  warning: container cleanup failed for {reference}: {exc!r}")
        return False
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0 and _MISSING_CONTAINER_RE.search(detail) is None:
        print(
            f"  warning: container cleanup failed for {reference}: "
            f"exit {result.returncode}: {detail[:500]}"
        )
        return False
    if not _container_is_absent(reference):
        print(f"  warning: Docker did not prove container {reference} absent after rm")
        return False
    return True


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_exclusive_lock(fd: int, *, label: str) -> None:
    deadline = time.monotonic() + HARNESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"timed out acquiring {label} after "
                f"{HARNESS_LOCK_TIMEOUT_SECONDS:g}s"
            )
        time.sleep(min(0.01, remaining))


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting container ownership")
        view = view[written:]


def _cleanup_temporary_file(path: Path, original_error: BaseException | None) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except BaseException as cleanup_error:
        if original_error is None:
            raise
        add_note = getattr(original_error, "add_note", None)
        if callable(add_note):
            add_note(
                "temporary-file unlink failed during cleanup: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        return
    try:
        _fsync_directory(path.parent)
    except BaseException as cleanup_error:
        if original_error is None:
            raise
        add_note = getattr(original_error, "add_note", None)
        if callable(add_note):
            add_note(
                "temporary-file directory fsync failed during cleanup: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )


def _open_regular_file(path: Path, flags: int, mode: int) -> tuple[int, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_flags = (
        flags
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"refusing non-regular harness file: {path}")
        try:
            if before is None:
                fd = os.open(path, safe_flags | os.O_CREAT | os.O_EXCL, mode)
                created = True
            else:
                fd = os.open(path, safe_flags)
                created = False
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise OSError(f"refusing non-regular harness file: {path}")
            opened_identity = (opened.st_dev, opened.st_ino)
            if (current.st_dev, current.st_ino) != opened_identity:
                continue
            if before is not None and (before.st_dev, before.st_ino) != opened_identity:
                continue
            result_fd = fd
            fd = -1
            return result_fd, created
        except FileNotFoundError:
            pass
        finally:
            if fd >= 0:
                os.close(fd)
    raise OSError(f"harness file did not stabilize while opening: {path}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    operation_error: BaseException | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        _cleanup_temporary_file(temporary, operation_error)


def _atomic_create_bytes(path: Path, payload: bytes) -> None:
    """Atomically create ``path`` without replacing another live owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    operation_error: BaseException | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temporary, path)
        _fsync_directory(path.parent)
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        _cleanup_temporary_file(temporary, operation_error)


def _atomic_write_text(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))


def _owner_directory(run_dir: Path) -> Path:
    return run_dir / ".opencollab" / "container_owners"


def container_owner_path(run_dir: Path, name: str) -> Path:
    digest = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _owner_directory(run_dir) / f"{digest}.json"


def _process_start_identity(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(fields) > 21:
            return f"proc:{fields[21]}"
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""


def _owner_record(name: str, *, state: str, cid: str = "") -> dict:
    return {
        "schema_version": CONTAINER_OWNER_SCHEMA_VERSION,
        "state": state,
        "container_name": name,
        "container_id": cid,
        "owner_pid": os.getpid(),
        "owner_start_identity": _process_start_identity(os.getpid()),
        "owner_token": uuid.uuid4().hex,
    }


def _encode_owner(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _read_owner(path: Path) -> dict | None:
    document = read_bounded_json(path, max_bytes=1024 * 1024)
    if document is None:
        return None
    value, _opened_stat = document
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != CONTAINER_OWNER_SCHEMA_VERSION:
        return None
    if value.get("state") not in {
        "pending",
        "active",
        "preservation_required",
        "candidate_staged",
        "kept",
    }:
        return None
    if not isinstance(value.get("container_name"), str) or not value["container_name"]:
        return None
    if not isinstance(value.get("container_id", ""), str):
        return None
    pid = value.get("owner_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(value.get("owner_start_identity", ""), str):
        return None
    if not isinstance(value.get("owner_token"), str) or not value["owner_token"]:
        return None
    return value


def _owner_is_live(record: dict) -> bool:
    pid = record["owner_pid"]
    expected = record.get("owner_start_identity", "")
    current = _process_start_identity(pid)
    if expected and current:
        return current == expected
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _create_pending_owner(run_dir: Path, name: str) -> dict:
    record = _owner_record(name, state="pending")
    _atomic_create_bytes(container_owner_path(run_dir, name), _encode_owner(record))
    return record


def _replace_owner(path: Path, previous: dict, updated: dict) -> None:
    current = _read_owner(path)
    if current is None or current.get("owner_token") != previous.get("owner_token"):
        raise RuntimeError("container ownership changed while updating marker")
    if current != previous:
        if current == updated:
            return
        raise RuntimeError("container ownership state changed while updating marker")
    _atomic_write_bytes(path, _encode_owner(updated))


def _path_matches_open_file(path: Path, fd: int) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return bool(
        stat.S_ISREG(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _unlink_owner(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    try:
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > MAX_OWNER_RECORD_BYTES:
                raise OSError(f"container owner record exceeds byte limit: {path}")
            payload = handle.read(MAX_OWNER_RECORD_BYTES + 1)
            if not _path_matches_open_file(path, handle.fileno()):
                return
            path.unlink()
    except OSError:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        raise
    if len(payload) > MAX_OWNER_RECORD_BYTES:
        raise OSError(f"container owner record exceeds byte limit: {path}")
    try:
        _fsync_directory(path.parent)
    except BaseException:
        if not path.exists():
            try:
                _atomic_create_bytes(path, payload)
            except BaseException:
                pass
        raise


def _write_compatibility_markers(run_dir: Path, cid: str, name: str) -> None:
    marker_dir = run_dir / ".opencollab" / "containers" / cid
    _atomic_write_text(marker_dir / "container.id", cid + "\n")
    _atomic_write_text(marker_dir / "container.name", name + "\n")
    _atomic_write_text(run_dir / "container.id", cid + "\n")
    _atomic_write_text(run_dir / "container.name", name + "\n")


def write_container_marker(run_dir: Path, cid: str, name: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = container_owner_path(run_dir, name)
    previous = _read_owner(path)
    if previous is None:
        previous = _owner_record(name, state="pending")
        _atomic_create_bytes(path, _encode_owner(previous))
    if previous["container_name"] != name:
        raise RuntimeError("container owner marker name mismatch")
    updated = {**previous, "state": "active", "container_id": cid}
    _replace_owner(path, previous, updated)
    _write_compatibility_markers(run_dir, cid, name)


def _remove_owned_container(record: dict) -> bool:
    reference = record.get("container_id") or record["container_name"]
    return _remove_labeled_container(
        reference,
        record["owner_token"],
        foreign_proves_absence=not bool(record.get("container_id")),
    )


def recover_stale_container_owners(run_dir: Path) -> bool:
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return True
    recovered = True
    for path in sorted(owner_dir.glob("*.json")):
        record = _read_owner(path)
        if record is None:
            print(f"  warning: invalid container owner record retained: {path}")
            recovered = False
            continue
        if record["state"] == "preservation_required":
            print(
                "  warning: container retained because output staging did not "
                f"complete: {record['container_name']}"
            )
            recovered = False
            continue
        if record["state"] == "kept" or _owner_is_live(record):
            continue
        if not _remove_owned_container(record):
            recovered = False
            continue
        _clear_compatibility_markers(
            run_dir, record.get("container_id") or None, record["container_name"]
        )
        _unlink_owner(path)
    return recovered


def start_container_with_marker(
    image: str,
    name: str,
    run_dir: Path,
) -> str:
    """Persist ownership before Docker creation, then upgrade it with the CID."""
    if not recover_generation_state(run_dir):
        raise RuntimeError("stale generation state recovery failed")
    pending = _create_pending_owner(run_dir, name)
    try:
        cid = start_container(image, name, pending["owner_token"])
        write_container_marker(run_dir, cid, name)
    except BaseException:
        current = _read_owner(container_owner_path(run_dir, name)) or pending
        if _remove_owned_container(current):
            _clear_compatibility_markers(
                run_dir, current.get("container_id") or None, name
            )
            _unlink_owner(container_owner_path(run_dir, name))
        raise
    return cid


def _clear_compatibility_markers(
    run_dir: Path,
    cid: str | None = None,
    name: str | None = None,
) -> None:
    if cid:
        marker_dir = run_dir / ".opencollab" / "containers" / cid
        try:
            marker_fd = os.open(
                marker_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            marker_fd = -1
        if marker_fd >= 0:
            try:
                removed_marker = False
                for marker in ("container.id", "container.name"):
                    try:
                        (marker_dir / marker).unlink()
                        removed_marker = True
                    except FileNotFoundError:
                        pass
                if removed_marker:
                    os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            try:
                marker_dir.rmdir()
            except FileNotFoundError:
                _fsync_directory(marker_dir.parent)
            except OSError:
                pass
            else:
                _fsync_directory(marker_dir.parent)
    legacy_id = run_dir / "container.id"
    if cid:
        value = _read_small_regular_text(legacy_id)
        if value is None or value.strip() != cid:
            return
    elif name:
        value = _read_small_regular_text(run_dir / "container.name")
        if value is None or value.strip() != name:
            return
    removed_legacy = False
    for marker in (legacy_id, run_dir / "container.name"):
        try:
            marker.unlink()
            removed_legacy = True
        except FileNotFoundError:
            pass
    if removed_legacy:
        _fsync_directory(run_dir)


def clear_container_marker(
    run_dir: Path,
    cid: str | None = None,
    name: str | None = None,
) -> None:
    """Clear markers after the caller has already proved container absence."""
    _clear_compatibility_markers(run_dir, cid, name)
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return
    for path in owner_dir.glob("*.json"):
        record = _read_owner(path)
        if record is None:
            continue
        if (cid and record.get("container_id") == cid) or (
            name and record.get("container_name") == name
        ):
            _unlink_owner(path)


def mark_container_kept(run_dir: Path, cid: str) -> None:
    for path in _owner_directory(run_dir).glob("*.json"):
        record = _read_owner(path)
        if record is None or record.get("container_id") != cid:
            continue
        _replace_owner(path, record, {**record, "state": "kept"})
        return
    raise RuntimeError(f"container ownership marker missing for kept container {cid}")


def remove_container_and_clear_marker(run_dir: Path, cid: str) -> bool:
    record = None
    for path in _owner_directory(run_dir).glob("*.json"):
        candidate = _read_owner(path)
        if candidate is not None and candidate.get("container_id") == cid:
            record = candidate
            break
    if record is None:
        return False
    if not _remove_owned_container(record):
        return False
    clear_container_marker(run_dir, cid, record["container_name"])
    return True


def finalize_container_ownership(
    *,
    run_dir: Path,
    cid: str,
    name: str,
    keep_container: bool,
    completed: bool,
    metrics: dict,
) -> None:
    if keep_container and completed:
        try:
            mark_container_kept(run_dir, cid)
        except BaseException as exc:
            if not remove_container_and_clear_marker(run_dir, cid):
                raise RuntimeError(
                    f"technical container cleanup failed for {cid} after "
                    "keep-marker failure; ownership marker retained"
                ) from exc
            raise
        metrics["container_retained"] = True
        print(f"  (left container {cid} running: {name})")
        return
    if not remove_container_and_clear_marker(run_dir, cid):
        raise RuntimeError(
            f"technical container cleanup failed for {cid}; ownership marker retained"
        )
    metrics["container_cleanup_succeeded"] = True


def build_task(instance: dict) -> str:
    problem = instance["problem_statement"]
    f2p = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    tests = "\n".join(f"- {t}" for t in f2p)
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n\n"
        f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n\n"
        "Locate the root cause in the source, apply a minimal fix, and ensure the "
        "behavior described above is satisfied."
    )


def _read_small_regular_text(path: Path) -> str | None:
    try:
        with open_regular_binary(path) as handle:
            info = os.fstat(handle.fileno())
            if info.st_size > MAX_COMPATIBILITY_MARKER_BYTES:
                return None
            raw = handle.read(MAX_COMPATIBILITY_MARKER_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_COMPATIBILITY_MARKER_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def load_instance(path: str | Path) -> dict:
    document = read_bounded_json(Path(path), max_bytes=MAX_INSTANCE_BYTES)
    if document is None or not isinstance(document[0], dict):
        raise ValueError(f"instance input is not a bounded regular JSON object: {path}")
    instance = document[0]
    instance["instance_id"] = validate_instance_id(instance.get("instance_id"))
    return instance


async def _quiesce_agent_tasks(
    tasks: list[asyncio.Task],
    *,
    grace_seconds: float = AGENT_CANCELLATION_GRACE_SECONDS,
) -> bool:
    """Wait for owned agent work, then repeat cancellation once if needed."""

    async def wait_pending(bound: float) -> set[asyncio.Task]:
        pending = {owned for owned in tasks if not owned.done()}
        if not pending:
            return set()
        _done, pending = await asyncio.wait(pending, timeout=bound)
        return set(pending)

    pending = await wait_pending(grace_seconds)
    if not pending:
        return True
    for owned in pending:
        owned.cancel()
    return not await wait_pending(grace_seconds)


async def run_agent(task: str, cid: str, cfg: dict, max_steps: int, budget: int,
                    timeout: float) -> dict:
    env = DockerEnvironment(
        container_id=cid,
        workspace=DOCKER_WORKDIR,
        exec_workdir=DOCKER_WORKDIR,
        command_prefix=_ACTIVATE,
        timeout_returncode=124,
    )
    agent = Agent(
        name="swe_agent",
        system_prompt=AGENT_PROMPT,
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        thinking=cfg.get("thinking", False),
        thinking_params=cfg.get("thinking_params") or {},
    )
    tracer = Tracer(run_id=f"swe_{uuid.uuid4().hex[:8]}",
                    output_dir=str(_REPO_ROOT / "logs" / "trajectories"))
    session = None
    timed_out = False
    failure: Exception | None = None
    tracer_failure: Exception | None = None
    owned_tasks: list[asyncio.Task] = []
    execution_quiesced = True
    deadline = time.monotonic() + timeout

    async def run_owned(awaitable) -> object:
        owned = asyncio.create_task(awaitable)
        owned_tasks.append(owned)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            owned.cancel()
            raise CallerTimeoutError
        return await abandon_on_timeout(owned, remaining)

    try:
        # Autosave a structured per-agent session JSON under the standard
        # .opencollab/sessions/<timestamp>/ run folder (same convention as team runs).
        run_dir = make_run_dir(str(_REPO_ROOT))
        save_path = agent_save_path(run_dir, 0, agent.name)
        session = build_session(
            agent=agent, env=env, tracer=tracer,
            max_budget_tokens=budget, max_steps=max_steps,
            auto_save_path=save_path,
        )
        print(f"  session autosave: {save_path}")
        await run_owned(session.add_user_message(task))
        await run_owned(session.run_loop())
    except CallerTimeoutError:
        timed_out = True
        print("  agent: wall-clock timeout reached, capturing current diff")
    except Exception as exc:  # preserve a partial worktree as a failed candidate
        failure = exc
        print(f"  agent: failed with {type(exc).__name__}: {exc}")
    finally:
        execution_quiesced = await _quiesce_agent_tasks(
            owned_tasks,
            grace_seconds=AGENT_CANCELLATION_GRACE_SECONDS,
        )
        if not execution_quiesced:
            await env.abort()
        try:
            tracer.close()
        except Exception as exc:  # preserve the candidate and expose trace loss
            tracer_failure = exc
        if tracer_failure is None and getattr(tracer, "write_error", None):
            tracer_failure = OSError(
                f"trajectory write failed: {tracer.write_error}"
            )
    step_count = int(getattr(session, "step_count", 0))
    used_tokens = int(getattr(session, "used_tokens", 0))
    print(f"  agent: steps={step_count} tokens={used_tokens}")
    phase = getattr(session, "phase", None)
    phase_value = phase.value if isinstance(phase, SessionPhase) else "error"
    if timed_out and execution_quiesced:
        workflow_status = "done_with_timeout_patch"
    elif not execution_quiesced:
        workflow_status = "error"
    elif failure is not None:
        workflow_status = phase_value if phase is not None and phase.is_terminal() else "error"
    elif phase is SessionPhase.DONE:
        workflow_status = "done"
    else:
        workflow_status = phase_value
    metrics = {
        "workflow_status": workflow_status,
        "session_phase": phase_value,
        "step_count": step_count,
        "used_tokens": used_tokens,
        "wall_clock_timeout": timed_out,
        "execution_quiesced": execution_quiesced,
        "submission_eligible": execution_quiesced
        and workflow_status in {"done", "done_with_timeout_patch"},
    }
    if not execution_quiesced:
        metrics["error_type"] = "ExecutionNotQuiesced"
        metrics["error"] = (
            "agent execution remained active after bounded cancellation cleanup"
        )
    if failure is not None:
        metrics["error_type"] = type(failure).__name__
        metrics["error"] = str(failure)
    if tracer_failure is not None:
        metrics["tracer_close_error_type"] = type(tracer_failure).__name__
        metrics["tracer_close_error"] = str(tracer_failure)
    if getattr(tracer, "write_error", None):
        metrics["tracer_write_error"] = str(tracer.write_error)
    metrics["tracer_dropped_steps"] = int(
        getattr(tracer, "dropped_steps", 0)
    )
    return metrics


def extract_patch(cid: str) -> str:
    # Stage everything so new files are included, then diff against HEAD.
    add_result = _docker("exec", "-w", DOCKER_WORKDIR, cid, "bash", "-lc", "git add -A")
    _check_docker(add_result, "git add -A before patch extraction")
    res = _docker(
        "exec",
        "-w",
        DOCKER_WORKDIR,
        cid,
        "bash",
        "-lc",
        bounded_container_output_command(
            "git diff --cached --binary",
            max_bytes=MAX_EXTRACTED_PATCH_BYTES,
            label="staged patch",
        ),
    )
    _check_docker(res, "git diff --cached during patch extraction")
    if not res.stdout.strip():
        status = _docker(
            "exec",
            "-w",
            DOCKER_WORKDIR,
            cid,
            "bash",
            "-lc",
            bounded_container_output_command(
                "git status --short",
                max_bytes=MAX_STATUS_DIAGNOSTIC_BYTES,
                label="git status diagnostic",
            ),
        )
        _check_docker(status, "git status --short after empty patch")
        print("  patch extraction: staged diff empty")
        print(f"  git status --short: {status.stdout.strip() or '(clean)'}")
    return res.stdout


def bounded_container_output_command(
    command: str,
    *,
    max_bytes: int,
    label: str,
) -> str:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    return (
        'python_bin=$(command -v python3 || command -v python) || exit 127; '
        f'"$python_bin" -c {shlex.quote(_BOUNDED_CAPTURE_SCRIPT)} '
        f"{max_bytes} {MAX_CAPTURED_STDERR_BYTES} {shlex.quote(label)} "
        f"bash -lc {shlex.quote(command)}"
    )


def _patch_sha256(patch: str) -> str:
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def runner_returncode_for_metrics(metrics: dict) -> int:
    if "runner_returncode" in metrics:
        existing = metrics["runner_returncode"]
        if isinstance(existing, bool) or not isinstance(existing, int):
            raise ValueError("runner_returncode must be a non-boolean integer")
        status = str(metrics.get("workflow_status") or "")
        expected = {"done": 0, "done_with_timeout_patch": 124}.get(status)
        if expected is not None and existing != expected:
            raise ValueError(
                f"runner_returncode {existing} conflicts with workflow_status {status!r}"
            )
        return existing
    status = str(metrics.get("workflow_status") or "")
    if status == "done":
        return 0
    if status == "done_with_timeout_patch":
        return 124
    return 1


def metrics_have_completed_identity(metrics: dict, patch: str) -> bool:
    if not patch.strip():
        return False
    if metrics.get("execution_quiesced") is not True:
        return False
    if metrics.get("submission_eligible") is not True:
        return False
    try:
        returncode = runner_returncode_for_metrics(metrics)
    except ValueError:
        return False
    status = str(metrics.get("workflow_status") or "")
    if status == "done":
        return returncode == 0
    if status == "done_with_timeout_patch":
        return returncode == 124
    return False


def complete_single_agent_integrity(
    metrics: dict,
    *,
    patch_extraction_succeeded: bool,
) -> None:
    """Record every proof field required by current harness records."""
    metrics.update(
        {
            "patch_extraction_succeeded": patch_extraction_succeeded,
            "injected_path_cleanup_proven": True,
            "harness_artifact_exclusion_proven": True,
            "checkpoint_restore_integrity_proven": True,
            "task_stage_integrity_proven": True,
            "test_patch_isolation_failed": False,
            "worktree_integrity_proven": True,
        }
    )


def build_output_records(
    *,
    instance_id: str,
    model_name: str,
    patch: str,
    metrics: dict,
    record_id: str | None = None,
) -> tuple[dict, dict]:
    record_id = record_id or uuid.uuid4().hex
    patch_sha = _patch_sha256(patch)
    metric_record = {
        **metrics,
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_name_or_path": model_name,
    }
    metric_record["runner_returncode"] = runner_returncode_for_metrics(metric_record)
    prediction = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha,
        "model_name_or_path": model_name,
        "model_patch": patch,
        "workflow_metric": metric_record,
    }
    return prediction, metric_record


def default_metrics_path(output_path: Path) -> Path:
    return output_path.with_name("metrics.jsonl")


def output_paths(
    output: str | Path,
    metrics: str | Path | None,
) -> tuple[Path, Path]:
    predictions_path = Path(output)
    metrics_path = Path(metrics) if metrics else default_metrics_path(predictions_path)
    predictions_path = _validate_output_target(predictions_path)
    metrics_path = _validate_output_target(metrics_path)
    if output_paths_collide(predictions_path, metrics_path):
        raise ValueError("prediction and metric outputs must use different files")
    return predictions_path, metrics_path


def _validate_output_target(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        existing = absolute.lstat()
    except FileNotFoundError:
        return absolute
    if not stat.S_ISREG(existing.st_mode):
        raise ValueError(f"output path must be a regular file or absent: {path}")
    return absolute


def output_paths_collide(first: Path, second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError):
        return False


def _append_jsonl_durable(path: Path, row: dict) -> None:
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_JSONL_BYTES:
        raise OSError(f"output JSONL row exceeds byte limit: {path}")
    fd, _created = _open_regular_file(
        path,
        os.O_RDWR | os.O_APPEND,
        0o644,
    )
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"output lock {path}")
        locked = True
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_OUTPUT_JSONL_BYTES:
            raise OSError(f"output JSONL exceeds byte limit: {path}")
        if needs_separator:
            _write_all(fd, b"\n")
        _write_all(fd, payload)
        os.fsync(fd)
        _fsync_directory(path.parent)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_output_records(
    predictions_path: Path,
    metrics_path: Path,
    prediction: dict,
    metric: dict,
) -> None:
    if output_paths_collide(predictions_path, metrics_path):
        raise ValueError("prediction and metric outputs must use different files")
    # The prediction is a self-contained commit record: its embedded metric is
    # enough for recovery if the external metrics projection cannot be written.
    _append_jsonl_durable(predictions_path, prediction)
    _append_jsonl_durable(metrics_path, metric)


def _pending_output_directory(run_dir: Path) -> Path:
    return run_dir / ".opencollab" / "pending_outputs"


def pending_output_path(run_dir: Path, instance_id: str, record_id: str) -> Path:
    identity = f"{instance_id}\0{record_id}"
    digest = hashlib.sha256(
        identity.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return _pending_output_directory(run_dir) / f"{digest}.json"


def _row_output_identity(row: dict) -> tuple[str, str, str]:
    instance_id = str(row.get("instance_id") or "")
    record_id = str(row.get("record_id") or "")
    patch_sha = str(row.get("patch_sha256") or "")
    return instance_id, record_id, patch_sha


def _validate_pending_candidate(candidate: dict) -> None:
    if candidate.get("schema_version") != PENDING_OUTPUT_SCHEMA_VERSION:
        raise ValueError("unsupported pending output schema")
    prediction = candidate.get("prediction")
    metric = candidate.get("metric")
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        raise ValueError("pending output must contain prediction and metric objects")
    prediction_identity = _row_output_identity(prediction)
    metric_identity = _row_output_identity(metric)
    if not prediction_identity[0] or not prediction_identity[1]:
        raise ValueError("pending output identity is incomplete")
    if prediction_identity != metric_identity:
        raise ValueError("pending prediction and metric identities differ")
    patch = str(prediction.get("model_patch") or "")
    computed_sha = _patch_sha256(patch)
    if not computed_sha or prediction_identity[2] != computed_sha:
        raise ValueError("pending prediction patch SHA is invalid")
    embedded = prediction.get("workflow_metric")
    if not isinstance(embedded, dict) or embedded != metric:
        raise ValueError("pending embedded metric differs from external metric")
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ValueError("pending metric runner_returncode is invalid")
    for key in (
        "predictions_path",
        "metrics_path",
        "container_id",
        "container_name",
        "owner_token",
    ):
        if not isinstance(candidate.get(key), str) or not candidate[key]:
            raise ValueError(f"pending output field {key} is missing")
    if not Path(candidate["predictions_path"]).is_absolute() or not Path(
        candidate["metrics_path"]
    ).is_absolute():
        raise ValueError("pending output targets must be absolute paths")
    _validate_output_target(Path(candidate["predictions_path"]))
    _validate_output_target(Path(candidate["metrics_path"]))
    if output_paths_collide(
        Path(candidate["predictions_path"]), Path(candidate["metrics_path"])
    ):
        raise ValueError("pending output targets collide")


def persist_pending_output(
    *,
    run_dir: Path,
    predictions_path: Path,
    metrics_path: Path,
    prediction: dict,
    metric: dict,
    cid: str,
    name: str,
) -> Path:
    absolute_predictions = _validate_output_target(predictions_path)
    absolute_metrics = _validate_output_target(metrics_path)
    if output_paths_collide(absolute_predictions, absolute_metrics):
        raise ValueError("pending output targets collide")
    owner_path = container_owner_path(run_dir, name)
    owner = _read_owner(owner_path)
    if (
        owner is None
        or owner.get("container_id") != cid
        or owner.get("state") != "active"
    ):
        raise RuntimeError("active container ownership is missing before output staging")
    preserving_owner = {**owner, "state": "preservation_required"}
    _replace_owner(owner_path, owner, preserving_owner)
    candidate = {
        "schema_version": PENDING_OUTPUT_SCHEMA_VERSION,
        "container_id": cid,
        "container_name": name,
        "owner_token": owner["owner_token"],
        "predictions_path": str(absolute_predictions),
        "metrics_path": str(absolute_metrics),
        "prediction": prediction,
        "metric": metric,
    }
    _validate_pending_candidate(candidate)
    instance_id, record_id, _patch_sha = _row_output_identity(prediction)
    path = pending_output_path(run_dir, instance_id, record_id)
    payload = _encode_owner(candidate)
    if len(payload) > MAX_PENDING_OUTPUT_BYTES:
        raise ValueError("pending output exceeds its byte limit")
    _atomic_create_bytes(path, payload)
    _replace_owner(
        owner_path,
        preserving_owner,
        {**preserving_owner, "state": "candidate_staged"},
    )
    return path


def output_staging_requires_container_preservation(
    run_dir: Path,
    *,
    cid: str,
    name: str,
) -> bool:
    owner = _read_owner(container_owner_path(run_dir, name))
    return bool(
        owner is not None
        and owner.get("container_id") == cid
        and owner.get("container_name") == name
        and owner.get("state")
        in {"preservation_required", "candidate_staged", "kept"}
    )


def _read_pending_fd(fd: int) -> tuple[dict, bytes]:
    size = os.fstat(fd).st_size
    if size <= 0 or size > MAX_PENDING_OUTPUT_BYTES:
        raise ValueError("pending output size is invalid")
    payload = os.pread(fd, size, 0)
    if len(payload) != size:
        raise OSError("short read while loading pending output")
    try:
        candidate = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pending output JSON is invalid") from exc
    if not isinstance(candidate, dict):
        raise ValueError("pending output JSON must be an object")
    _validate_pending_candidate(candidate)
    return candidate, payload


def _open_pending_regular(path: Path) -> int:
    fd = os.open(
        path,
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("pending output path is not a regular file")
        if opened.st_size <= 0 or opened.st_size > MAX_PENDING_OUTPUT_BYTES:
            raise ValueError("pending output size is invalid")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _candidate_matches_owner(candidate: dict, owner: dict) -> bool:
    return (
        candidate.get("container_id") == owner.get("container_id")
        and candidate.get("container_name") == owner.get("container_name")
        and candidate.get("owner_token") == owner.get("owner_token")
    )


def _preservation_was_superseded(owner_path: Path, owner: dict) -> bool:
    current = _read_owner(owner_path)
    if current is None:
        return not owner_path.exists()
    return (
        current.get("owner_token") == owner.get("owner_token")
        and current.get("state") in {"candidate_staged", "kept"}
    )


def _promote_durable_preservation_candidates(run_dir: Path) -> bool:
    owner_dir = _owner_directory(run_dir)
    if not owner_dir.exists():
        return True
    pending_dir = _pending_output_directory(run_dir)
    promoted = True
    for owner_path in sorted(owner_dir.glob("*.json")):
        owner = _read_owner(owner_path)
        if owner is None or owner.get("state") != "preservation_required":
            continue
        matching_paths: list[Path] = []
        if pending_dir.exists():
            for path in sorted(pending_dir.glob("*.json")):
                try:
                    fd = _open_pending_regular(path)
                    locked = False
                    try:
                        _acquire_exclusive_lock(
                            fd,
                            label=f"pending-output lock {path}",
                        )
                        locked = True
                        candidate, _payload = _read_pending_fd(fd)
                    finally:
                        try:
                            if locked:
                                fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)
                except BaseException:
                    continue
                if _candidate_matches_owner(candidate, owner):
                    matching_paths.append(path)
        if len(matching_paths) != 1:
            if not _preservation_was_superseded(owner_path, owner):
                promoted = False
            continue
        path = matching_paths[0]
        try:
            fd = _open_pending_regular(path)
            locked = False
            try:
                _acquire_exclusive_lock(
                    fd,
                    label=f"pending-output lock {path}",
                )
                locked = True
                candidate, _payload = _read_pending_fd(fd)
                if not _candidate_matches_owner(candidate, owner):
                    raise RuntimeError(
                        "pending output identity changed during preservation recovery"
                    )
                os.fsync(fd)
                _fsync_directory(path.parent)
                _replace_owner(
                    owner_path,
                    owner,
                    {**owner, "state": "candidate_staged"},
                )
            finally:
                try:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        except BaseException as exc:
            if _preservation_was_superseded(owner_path, owner):
                continue
            print(
                "  warning: preserved candidate validation failed for "
                f"{owner['container_name']}: {exc!r}"
            )
            promoted = False
    return promoted


def _find_committed_identity(fd: int, expected: dict) -> bool:
    expected_instance, expected_record, expected_sha = _row_output_identity(expected)
    if os.fstat(fd).st_size > MAX_OUTPUT_JSONL_BYTES:
        raise OSError("output JSONL exceeds byte limit")
    with os.fdopen(os.dup(fd), "rb") as handle:
        handle.seek(0)
        while True:
            line = handle.readline(MAX_JSONL_SCAN_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_JSONL_SCAN_LINE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = handle.readline(MAX_JSONL_SCAN_LINE_BYTES + 1)
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            instance_id, record_id, patch_sha = _row_output_identity(row)
            if instance_id != expected_instance or record_id != expected_record:
                continue
            if patch_sha != expected_sha or row != expected:
                raise RuntimeError(
                    "committed output conflicts with pending record identity"
                )
            return True
    return False


def _append_jsonl_durable_once(path: Path, row: dict) -> bool:
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_JSONL_BYTES:
        raise OSError(f"output JSONL row exceeds byte limit: {path}")
    fd, _created = _open_regular_file(
        path,
        os.O_RDWR | os.O_APPEND,
        0o644,
    )
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"output lock {path}")
        locked = True
        if _find_committed_identity(fd, row):
            _fsync_directory(path.parent)
            return False
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_OUTPUT_JSONL_BYTES:
            raise OSError(f"output JSONL exceeds byte limit: {path}")
        if needs_separator:
            _write_all(fd, b"\n")
        _write_all(fd, payload)
        os.fsync(fd)
        _fsync_directory(path.parent)
        return True
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _pending_owner_state(run_dir: Path, candidate: dict) -> str:
    owner_path = container_owner_path(run_dir, candidate["container_name"])
    if not owner_path.exists():
        return "absent"
    owner = _read_owner(owner_path)
    if owner is None:
        raise RuntimeError("pending output has an invalid container owner")
    if owner.get("owner_token") != candidate["owner_token"]:
        raise RuntimeError("pending output owner token mismatch")
    if owner["state"] == "kept":
        return "kept"
    return "deferred"


def _unlink_pending_locked(path: Path, fd: int, payload: bytes) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode):
        return
    if current.st_dev != os.fstat(fd).st_dev or current.st_ino != os.fstat(fd).st_ino:
        return
    path.unlink()
    try:
        _fsync_directory(path.parent)
    except BaseException:
        if not path.exists():
            try:
                _atomic_create_bytes(path, payload)
            except BaseException:
                pass
        raise


def publish_pending_output(run_dir: Path, path: Path) -> str:
    try:
        fd = _open_pending_regular(path)
    except FileNotFoundError:
        return "missing"
    locked = False
    try:
        _acquire_exclusive_lock(fd, label=f"pending-output lock {path}")
        locked = True
        try:
            current = path.lstat()
        except FileNotFoundError:
            return "missing"
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            return "missing"
        candidate, payload = _read_pending_fd(fd)
        if _pending_owner_state(run_dir, candidate) == "deferred":
            return "deferred"
        _append_jsonl_durable_once(
            Path(candidate["predictions_path"]), candidate["prediction"]
        )
        _append_jsonl_durable_once(
            Path(candidate["metrics_path"]), candidate["metric"]
        )
        _unlink_pending_locked(path, fd, payload)
        return "published"
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def recover_generation_state(run_dir: Path) -> bool:
    candidates_promoted = _promote_durable_preservation_candidates(run_dir)
    owners_recovered = recover_stale_container_owners(run_dir)
    outputs_recovered = True
    pending_dir = _pending_output_directory(run_dir)
    if pending_dir.exists():
        for path in sorted(pending_dir.glob("*.json")):
            try:
                publish_pending_output(run_dir, path)
            except BaseException as exc:
                print(f"  warning: pending output recovery failed for {path}: {exc!r}")
                outputs_recovered = False
    return candidates_promoted and owners_recovered and outputs_recovered


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate one SWE-bench prediction with OpenCollab")
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument(
        "--metrics",
        default=None,
        help="Metrics JSONL to append to (default: metrics.jsonl beside --output)",
    )
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--budget", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()
    try:
        args.max_steps, args.budget, args.timeout = validate_generation_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
        )
    except ValueError as exc:
        ap.error(str(exc))
    out_path, metrics_path = output_paths(args.output, args.metrics)

    instance = load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or default_container_image(args.arch, iid)

    cfg = get_config(str(_REPO_ROOT))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    model_name = args.model_name or f"opencollab-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")

    name = unique_container_name("oc-gen-", iid)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out_path.parent
    cid = start_container_with_marker(image, name, run_dir)
    print(f"Container: {cid}")
    patch = ""
    metrics: dict = {}
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    try:
        task = build_task(instance)
        metrics = run_with_bounded_shutdown(
            run_agent(task, cid, cfg, args.max_steps, args.budget, args.timeout)
        )
        if metrics.get("submission_eligible") is True:
            patch = extract_patch(cid)
            patch_extraction_succeeded = True
        else:
            patch = ""
            patch_extraction_succeeded = False
        complete_single_agent_integrity(
            metrics,
            patch_extraction_succeeded=patch_extraction_succeeded,
        )
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        record, metric_record = build_output_records(
            instance_id=iid,
            model_name=model_name,
            patch=patch,
            metrics=metrics,
        )
        pending_required = bool(patch.strip())
        if pending_required:
            pending_path = persist_pending_output(
                run_dir=run_dir,
                predictions_path=out_path,
                metrics_path=metrics_path,
                prediction=record,
                metric=metric_record,
                cid=cid,
                name=name,
            )
    except BaseException as exc:
        generation_error = exc
        raise
    finally:
        preserve_container = (
            pending_required
            and pending_path is None
            and output_staging_requires_container_preservation(
                run_dir,
                cid=cid,
                name=name,
            )
        )
        if preserve_container:
            metrics["container_preservation_required"] = True
        else:
            completed = generation_error is None and metrics_have_completed_identity(
                metrics,
                patch,
            )
            try:
                finalize_container_ownership(
                    run_dir=run_dir,
                    cid=cid,
                    name=name,
                    keep_container=args.keep_container if generation_error is None else False,
                    completed=completed,
                    metrics=metrics,
                )
            except BaseException as cleanup_error:
                if generation_error is None:
                    raise
                add_note = getattr(generation_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "container cleanup failed after generation error: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )

    if record is None or metric_record is None:
        raise RuntimeError("generation output record was not built")
    if pending_path is not None:
        publish_status = publish_pending_output(run_dir, pending_path)
        if publish_status == "deferred":
            raise RuntimeError("pending output remained blocked by container ownership")
    else:
        append_output_records(out_path, metrics_path, record, metric_record)

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (agent made no tracked changes)")

    if not metrics_have_completed_identity(metric_record, patch):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

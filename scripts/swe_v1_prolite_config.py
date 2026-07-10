"""Configuration, proxy setup, and runtime sync for the pro-lite launcher."""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from scripts.swe_v1_prolite_common import (
    DEFAULT_BASE_RUN_DIR_PREFIX,
    MAX_PROXY_ENV_BYTES,
    PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
    REMOTE_HEALTH_SSH_TIMEOUT_FLOOR,
    REMOTE_PROXY_TUNNELS,
    REPO_ROOT,
    SYNC_DIRS,
    SYNC_FILES,
    _redacted,
)
from scripts.swe_v1_prolite_process import (
    _block_local_spawn_signals,
    _ensure_local_process_group_quiesced_after_wait,
    _restore_local_spawn_signals,
    terminate_local_process_group,
)


def run_checked(
    command: list[str], *, timeout: int = 120, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"{command[0]} exited {result.returncode}"))
    return result


def _read_bounded_regular_text(path: Path, *, max_bytes: int) -> str:
    path = path.expanduser()
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise RuntimeError(f"input must be a bounded regular file: {path}")
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(f"input changed while opening: {path}")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RuntimeError(f"input exceeds {max_bytes} bytes: {path}")
    return raw.decode("utf-8")


def load_shell_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = _read_bounded_regular_text(path, max_bytes=MAX_PROXY_ENV_BYTES)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        parsed = shlex.split(value, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def token_from_values(values: dict[str, str]) -> str:
    for name in ("GLM_PROXY_CLIENT_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENCOLLAB_API_KEY"):
        value = values.get(name)
        if value:
            return value
    return ""


def token_from_env_file(path: Path) -> str:
    try:
        return token_from_values(load_shell_env(path))
    except FileNotFoundError:
        return ""


def proxy_env_file_from_ps(ps_text: str) -> Path | None:
    try:
        parts = shlex.split(ps_text)
    except ValueError:
        return None
    for index, part in enumerate(parts):
        if part == "--env-file" and index + 1 < len(parts):
            return Path(parts[index + 1])
        if part.startswith("--env-file="):
            return Path(part.split("=", 1)[1])
    return None


def get_proxy_token(proxy_env_file: Path | None) -> str:
    token = token_from_values(dict(os.environ))
    if token:
        return token
    if proxy_env_file is not None:
        token = token_from_env_file(proxy_env_file)
        if token:
            return token
    try:
        pids = subprocess.check_output(
            [
                "pgrep",
                "-f",
                "opencollab_glm_anthropic_proxy.py|glm_anthropic_proxy.py",
            ],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        ).split()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while locating the glm proxy process") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("glm proxy process not found") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to locate the glm proxy process: {exc}") from exc
    if not pids:
        raise RuntimeError("glm proxy process not found")
    try:
        ps = subprocess.check_output(
            ["ps", "eww", "-p", pids[0]],
            text=True,
            timeout=PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("timed out while reading the glm proxy environment") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(f"failed to read the glm proxy environment: {exc}") from exc
    env_path = proxy_env_file_from_ps(ps)
    if env_path:
        token = token_from_env_file(env_path)
        if token:
            return token
    match = re.search(r"GLM_PROXY_CLIENT_TOKEN=(\S+)", ps)
    if not match:
        raise RuntimeError("proxy token not found in environment, proxy env file, or proxy process")
    return match.group(1)


def url_with_healthz(base_url: str) -> str:
    return base_url.rstrip("/") + "/healthz"


def local_http_ok(base_url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def remote_http_ok(*, ssh_command: list[str], host: str, base_url: str, timeout: int = 10) -> bool:
    probe = "import sys,urllib.request;urllib.request.urlopen(sys.argv[1], timeout=" + str(timeout) + ").read()"
    try:
        result = subprocess.run(
            [*ssh_command, host, "python3 -c " + shlex.quote(probe) + " " + shlex.quote(url_with_healthz(base_url))],
            text=True,
            capture_output=True,
            timeout=max(REMOTE_HEALTH_SSH_TIMEOUT_FLOOR, timeout + 8),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def loopback_port(base_url: str) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if parsed.port is None:
        raise RuntimeError(f"proxy URL must include an explicit port: {base_url}")
    return int(parsed.port)


def loopback_url_with_port(base_url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if host == "::1":
        netloc = f"[::1]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def remote_forward_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "remote port forwarding failed" in lowered
        or "address already in use" in lowered
        or "cannot listen to port" in lowered
    )


def stop_remote_proxy_tunnel(proc: subprocess.Popen[str]) -> bool:
    return terminate_local_process_group(proc)


def cleanup_remote_proxy_tunnels() -> None:
    for proc in list(REMOTE_PROXY_TUNNELS):
        try:
            cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        except BaseException:
            cleanup_quiesced = False
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)


atexit.register(cleanup_remote_proxy_tunnels)


def start_remote_proxy_tunnel(command: list[str]) -> tuple[subprocess.Popen[str] | None, str]:
    spawn_signal_state = _block_local_spawn_signals()
    try:
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        _restore_local_spawn_signals(spawn_signal_state)
        raise
    REMOTE_PROXY_TUNNELS.append(proc)
    try:
        _restore_local_spawn_signals(spawn_signal_state)
        time.sleep(0.2)
        if proc.poll() is not None:
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_quiesced = terminate_local_process_group(proc)
                if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
                    REMOTE_PROXY_TUNNELS.remove(proc)
                return None, "ssh tunnel output drain timed out"
            cleanup_quiesced = _ensure_local_process_group_quiesced_after_wait(proc)
            if cleanup_quiesced:
                REMOTE_PROXY_TUNNELS.remove(proc)
            else:
                return (
                    None,
                    "ssh tunnel leader exited with residual process-group descendants that could not be cleaned",
                )
            message = _redacted(stderr or stdout or f"{command[0]} exited {proc.returncode}")
            return None, message
        return proc, ""
    except BaseException:
        cleanup_quiesced = False
        try:
            cleanup_quiesced = terminate_local_process_group(proc)
        except BaseException:
            pass
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        raise


def ensure_remote_proxy(
    *,
    ssh_command: list[str],
    host: str,
    local_proxy_base_url: str,
    remote_proxy_base_url: str,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    if remote_http_ok(ssh_command=ssh_command, host=host, base_url=remote_proxy_base_url):
        return {"status": "already_healthy", "remote_proxy_base_url": remote_proxy_base_url}
    if not local_http_ok(local_proxy_base_url):
        raise RuntimeError(f"local proxy health check failed: {url_with_healthz(local_proxy_base_url)}")
    local_port = loopback_port(local_proxy_base_url)
    remote_port = loopback_port(remote_proxy_base_url)
    attempts: list[str] = []
    for candidate_port in range(remote_port, remote_port + 21):
        candidate_base_url = loopback_url_with_port(remote_proxy_base_url, candidate_port)
        forward = f"127.0.0.1:{candidate_port}:127.0.0.1:{local_port}"
        command = [
            *ssh_command,
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            forward,
            host,
        ]
        proc, message = start_remote_proxy_tunnel(command)
        if proc is None:
            attempts.append(f"{candidate_port}: {message}")
            if remote_forward_port_conflict(message):
                if remote_http_ok(
                    ssh_command=ssh_command,
                    host=host,
                    base_url=candidate_base_url,
                    timeout=2,
                ):
                    return {
                        "status": "already_healthy",
                        "remote_proxy_base_url": candidate_base_url,
                        "selected_remote_port": candidate_port,
                    }
                continue
            raise RuntimeError(message)
        for _ in range(6):
            if remote_http_ok(ssh_command=ssh_command, host=host, base_url=candidate_base_url, timeout=2):
                return {
                    "status": "started" if candidate_port == remote_port else "started_fallback_port",
                    "local_proxy_base_url": local_proxy_base_url,
                    "remote_proxy_base_url": candidate_base_url,
                    "forward": forward,
                    "selected_remote_port": candidate_port,
                }
            time.sleep(0.5)
        cleanup_quiesced = stop_remote_proxy_tunnel(proc)
        if cleanup_quiesced and proc in REMOTE_PROXY_TUNNELS:
            REMOTE_PROXY_TUNNELS.remove(proc)
        if not cleanup_quiesced:
            raise RuntimeError(f"remote proxy tunnel on port {candidate_port} did not stop")
        attempts.append(f"{candidate_port}: tunnel started but health check failed")
    detail = "; ".join(attempts[-5:])
    raise RuntimeError(f"remote proxy tunnel did not become healthy near port {remote_port}: {detail}")


def sync_runtime(*, ssh_command: list[str], host: str, remote_runtime_repo: str) -> dict[str, Any]:
    synced: list[str] = []
    synced_dirs: list[str] = []
    ssh_part = " ".join(shlex.quote(part) for part in ssh_command)
    with tempfile.TemporaryDirectory(prefix="swe-v1-runtime-") as tmp_dir:
        archive_path = Path(tmp_dir) / "runtime.tgz"

        def archive_filter(tar_info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(tar_info.name).parts
            if "__pycache__" in parts or tar_info.name.endswith((".pyc", ".pyo")):
                return None
            return tar_info

        with tarfile.open(archive_path, "w:gz") as archive:
            for rel in SYNC_FILES:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced.append(rel)
            for rel in SYNC_DIRS:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced_dirs.append(rel)
        run_checked([*ssh_command, host, "mkdir -p " + shlex.quote(remote_runtime_repo)], timeout=60)
        remote_archive = remote_runtime_repo.rstrip("/") + "/runtime.tgz"
        run_checked(["rsync", "-az", "-e", ssh_part, str(archive_path), f"{host}:{remote_archive}"], timeout=300)
        run_checked(
            [*ssh_command, host, "tar -xzf " + shlex.quote(remote_archive) + " -C " + shlex.quote(remote_runtime_repo)],
            timeout=300,
        )
    sh_files = [rel for rel in synced if rel.endswith(".sh")]
    if sh_files:
        run_checked(
            [
                *ssh_command,
                host,
                "cd "
                + shlex.quote(remote_runtime_repo)
                + " && chmod +x "
                + " ".join(shlex.quote(rel) for rel in sh_files),
            ],
            timeout=60,
        )
    compile_targets = [
        rel
        for rel in ("scripts", "swebench", "workflows", *SYNC_DIRS)
        if rel in synced_dirs or any(item == rel or item.startswith(rel + "/") for item in synced)
    ]
    if compile_targets:
        run_checked(
            [
                *ssh_command,
                host,
                "cd "
                + shlex.quote(remote_runtime_repo)
                + " && python3 -m compileall -q "
                + " ".join(shlex.quote(rel) for rel in compile_targets),
            ],
            timeout=180,
        )
    return {
        "remote_runtime_repo": remote_runtime_repo,
        "synced": synced,
        "synced_dirs": synced_dirs,
        "compile_targets": compile_targets,
    }


def configure_run_paths(args: argparse.Namespace) -> None:
    if not args.run_id:
        args.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    if not args.base_run_dir:
        if DEFAULT_BASE_RUN_DIR_PREFIX:
            prefix = DEFAULT_BASE_RUN_DIR_PREFIX.rstrip("_")
            args.base_run_dir = f"{prefix}_{args.run_id}"
        else:
            args.base_run_dir = str(Path(args.remote_root) / "runs" / f"swe_v1_prolite_{args.run_id}")
    if not args.remote_runtime_repo:
        args.remote_runtime_repo = str(Path(args.base_run_dir) / "_runtime" / "repo")


def validate_run_id(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("run_id must be one non-empty path component")
    if Path(value).is_absolute() or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValueError("run_id must be one safe path component")
    if len(value.encode("utf-8")) > 240:
        raise ValueError("run_id exceeds 240 UTF-8 bytes")
    return value


__all__ = [name for name in globals() if not name.startswith("__")]

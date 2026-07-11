from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

from swe_v1_prolite_runner_test_support import (
    SimpleNamespace,
    os,
    pytest,
    runner,
    subprocess,
    sys,
)


def _run_runtime_sync_command_locally(command, *, timeout=120, input_text=None):
    if command[0] == "rsync":
        destination = Path(command[-1].split(":", 1)[1])
        shutil.copy2(command[-2], destination)
        return subprocess.CompletedProcess(command, 0, "", "")
    assert command[:2] == ["ssh", "remote-host"]
    result = subprocess.run(
        ["sh", "-c", command[-1]],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return result


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--budget", "-1"),
        ("--max-steps", "0"),
        ("--swe-timeout", "0"),
        ("--task-wall-timeout", "-2"),
        ("--eval-timeout", "0"),
        ("--llm-timeout", "0"),
        ("--total-timeout", "-3"),
        ("--checkpoint-interval", "-1"),
        ("--limit", "1001"),
        ("--run-id", "../../escape"),
    ],
)
def test_main_rejects_invalid_numeric_limits_before_dry_run(
    monkeypatch, option, value
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swe_v1_prolite_runner.py", "--dry-run", option, value],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("missing_default", "expected_option"),
    [
        ("DEFAULT_HOST", "--host"),
        ("DEFAULT_REMOTE_ROOT", "--remote-root"),
        ("DEFAULT_MODEL_NAME", "--model-name"),
        ("DEFAULT_SESSION_PREFIX", "--session-prefix"),
        ("DEFAULT_IMAGE_REPOSITORY", "--image-repository"),
        ("DEFAULT_REMOTE_PROXY_BASE_URL", "--remote-proxy-base-url"),
        ("DEFAULT_LOCAL_PROXY_BASE_URL", "--local-proxy-base-url"),
    ],
)
def test_main_rejects_missing_runtime_configuration_before_start(
    monkeypatch,
    capsys,
    missing_default,
    expected_option,
):
    defaults = {
        "DEFAULT_HOST": "remote-host",
        "DEFAULT_REMOTE_ROOT": "/remote/root",
        "DEFAULT_MODEL_NAME": "model",
        "DEFAULT_SESSION_PREFIX": "session",
        "DEFAULT_IMAGE_REPOSITORY": "registry.example/swebench",
        "DEFAULT_REMOTE_PROXY_BASE_URL": "http://127.0.0.1:18788",
        "DEFAULT_LOCAL_PROXY_BASE_URL": "http://127.0.0.1:8080",
    }
    for name, value in defaults.items():
        monkeypatch.setattr(runner, name, value)
    monkeypatch.setattr(runner, missing_default, "")
    monkeypatch.setattr(
        runner,
        "run_remote",
        lambda args: pytest.fail("run_remote must not start after parser validation fails"),
    )
    monkeypatch.setattr(sys, "argv", ["swe_v1_prolite_runner.py", "--dry-run"])

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    assert expected_option in capsys.readouterr().err


def test_run_remote_uses_installed_remote_module_without_inline_payload(monkeypatch):
    commands = []

    class FinishedProcess:
        pid = 424280
        returncode = 0

    def fake_popen(command, *args, **kwargs):
        commands.append(command)
        return FinishedProcess()

    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: {"remote_proxy_base_url": "http://127.0.0.1:18788"},
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner, "_block_local_spawn_signals", lambda: object())
    monkeypatch.setattr(runner, "_restore_local_spawn_signals", lambda state: None)
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda proc, payload, timeout: ('{"status":"done"}', ""),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pgid: False)
    args = SimpleNamespace(
        ssh_command="ssh -p 22",
        host="remote-host",
        local_proxy_base_url="http://127.0.0.1:8080",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=False,
        no_sync_runtime=True,
        remote_runtime_repo="/remote/repo",
        proxy_env_file=None,
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
        model_name="model",
        session_prefix="session",
        image_repository="registry.example/swebench",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=30,
    )

    summary = runner.run_remote(args)

    assert summary["status"] == "done"
    remote_command = commands[0][-1]
    assert "python3 -m opencollab.harness.swe_v1_remote_runner" in remote_command
    assert "base64" not in remote_command
    assert "exec(" not in remote_command


def test_runtime_archive_imports_generation_entrypoints_from_clean_directory(monkeypatch, tmp_path):
    extracted = tmp_path / "clean-runtime"
    extracted.mkdir()
    imported = False

    def fake_run_checked(command, *, timeout=120, input_text=None):
        nonlocal imported
        if command[0] == "rsync":
            archive_path = command[-2]
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import gen_prediction; import gen_prediction_workflow",
                ],
                cwd=extracted / "swebench",
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            assert probe.returncode == 0, probe.stderr
            imported = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", fake_run_checked)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    assert imported is True
    assert "scripts/swebench_process.py" in summary["synced"]
    for name in (
        "gen_prediction_agent.py",
        "gen_prediction_bounded_capture.py",
        "gen_prediction_config.py",
        "gen_prediction_constants.py",
        "gen_prediction_docker.py",
        "gen_prediction_pending.py",
        "gen_prediction_safe_output.py",
    ):
        assert (extracted / "swebench" / name).is_file()


def test_runtime_sync_requires_every_declared_input(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SYNC_FILES", ["missing.py"])
    monkeypatch.setattr(runner, "SYNC_DIRS", ["missing-package"])
    monkeypatch.setattr(runner, "run_checked", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RuntimeError, match="missing-package, missing.py"):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert calls == []


def test_runtime_sync_replaces_reused_directory_with_manifested_snapshot(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "remote-runtime"
    remote_runtime.mkdir()
    (remote_runtime / "runtime.tgz").write_bytes(b"legacy runtime marker")
    (remote_runtime / "stale_module.py").write_text("STALE = True\n", encoding="utf-8")

    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo=str(remote_runtime),
    )

    assert not (remote_runtime / "stale_module.py").exists()
    manifest = json.loads((remote_runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert manifest["synced"] == summary["synced"]
    assert manifest["synced_dirs"] == summary["synced_dirs"]
    assert "opencollab/opencollab/application/session.py" in manifest["archive_members"]
    assert not list(tmp_path.glob("remote-runtime.*"))


def test_runtime_sync_refuses_to_replace_unmarked_directory(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "user-directory"
    remote_runtime.mkdir()
    user_file = remote_runtime / "notes.txt"
    user_file.write_text("keep me\n", encoding="utf-8")

    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)

    with pytest.raises(RuntimeError, match="unmarked runtime directory"):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo=str(remote_runtime),
        )

    assert user_file.read_text(encoding="utf-8") == "keep me\n"
    assert not list(tmp_path.glob("user-directory.*"))

def test_ensure_remote_proxy_falls_back_when_default_remote_port_is_busy():
    calls: list[list[str]] = []
    started_ports: set[int] = set()
    old_remote_http_ok = runner.remote_http_ok
    old_local_http_ok = runner.local_http_ok
    old_start_remote_proxy_tunnel = runner.start_remote_proxy_tunnel
    old_sleep = runner.time.sleep

    def fake_remote_http_ok(*, ssh_command, host, base_url, timeout=10):
        return base_url == "http://127.0.0.1:18789" and 18789 in started_ports

    def fake_start_remote_proxy_tunnel(command):
        calls.append(command)
        forward = command[command.index("-R") + 1]
        if forward.startswith("127.0.0.1:18788:"):
            return None, "Error: remote port forwarding failed for listen port 18788"
        if forward.startswith("127.0.0.1:18789:"):
            started_ports.add(18789)
            return SimpleNamespace(poll=lambda: None), ""
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.start_remote_proxy_tunnel = fake_start_remote_proxy_tunnel
        runner.time.sleep = lambda _seconds: None

        summary = runner.ensure_remote_proxy(
            ssh_command=["ssh"],
            host="remote-host",
            local_proxy_base_url="http://127.0.0.1:8878",
            remote_proxy_base_url="http://127.0.0.1:18788",
            enabled=True,
        )
    finally:
        runner.remote_http_ok = old_remote_http_ok
        runner.local_http_ok = old_local_http_ok
        runner.start_remote_proxy_tunnel = old_start_remote_proxy_tunnel
        runner.time.sleep = old_sleep

    assert summary["status"] == "started_fallback_port"
    assert summary["remote_proxy_base_url"] == "http://127.0.0.1:18789"
    assert summary["selected_remote_port"] == 18789
    assert "-N" in calls[1]
    assert "-fN" not in calls[1]
    assert calls[0][calls[0].index("-R") + 1] == "127.0.0.1:18788:127.0.0.1:8878"
    assert calls[1][calls[1].index("-R") + 1] == "127.0.0.1:18789:127.0.0.1:8878"


def test_proxy_tunnel_registers_before_pending_interrupt_restore(monkeypatch):
    process = SimpleNamespace(pid=424253, poll=lambda: None)
    cleanup_seen = []
    real_restore = runner._restore_local_spawn_signals

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert process in runner.REMOTE_PROXY_TUNNELS
        raise SystemExit(79)

    def fake_terminate(proc):
        cleanup_seen.append(proc)
        return True

    monkeypatch.setattr(runner, "_restore_local_spawn_signals", restore_then_interrupt)
    monkeypatch.setattr(runner, "terminate_local_process_group", fake_terminate)

    with pytest.raises(SystemExit) as exc:
        runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

    assert exc.value.code == 79
    assert cleanup_seen == [process]
    assert process not in runner.REMOTE_PROXY_TUNNELS


def test_proxy_tunnel_normal_leader_exit_cleanup_failure_is_explicit(monkeypatch):
    class ReapedTunnelLeader:
        pid = 424265
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return "", "ssh exited"

    process = ReapedTunnelLeader()
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        runner,
        "_ensure_local_process_group_quiesced_after_wait",
        lambda proc: False,
    )
    try:
        tunnel, message = runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

        assert tunnel is None
        assert "residual process-group descendants" in message
        assert process in runner.REMOTE_PROXY_TUNNELS
    finally:
        if process in runner.REMOTE_PROXY_TUNNELS:
            runner.REMOTE_PROXY_TUNNELS.remove(process)


def test_get_proxy_token_process_lookup_timeout_is_bounded(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "token_from_values", lambda values: "")

    def fake_check_output(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError, match="timed out while locating"):
        runner.get_proxy_token(tmp_path / "missing.env")

    assert calls[0][1]["timeout"] == runner.PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_proxy_env_reader_rejects_unsafe_file_without_blocking(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    path = tmp_path / "proxy.env"
    if kind == "symlink":
        target = tmp_path / "real.env"
        target.write_text("GLM_PROXY_CLIENT_TOKEN=secret\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises((OSError, RuntimeError)):
        runner.load_shell_env(path)


def test_proxy_env_reader_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_PROXY_ENV_BYTES", 32)
    path = tmp_path / "proxy.env"
    path.write_text("GLM_PROXY_CLIENT_TOKEN=" + "x" * 64, encoding="utf-8")

    with pytest.raises(RuntimeError, match="bounded regular file"):
        runner.load_shell_env(path)

def test_remote_http_ok_keeps_ssh_outer_timeout_above_short_http_probe():
    calls: list[dict] = []
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="remote-host",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is True
    assert calls[0]["timeout"] == runner.REMOTE_HEALTH_SSH_TIMEOUT_FLOOR
    assert "http://127.0.0.1:18792/healthz" in calls[0]["command"][-1]


def test_remote_http_ok_returns_false_on_outer_timeout():
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="remote-host",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is False

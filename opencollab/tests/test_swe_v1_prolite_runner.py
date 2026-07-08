from __future__ import annotations

import scripts.swe_v1_prolite_runner as runner


def test_ensure_remote_proxy_falls_back_when_default_remote_port_is_busy():
    calls: list[list[str]] = []
    started_ports: set[int] = set()
    old_remote_http_ok = runner.remote_http_ok
    old_local_http_ok = runner.local_http_ok
    old_run_checked = runner.run_checked
    old_sleep = runner.time.sleep

    def fake_remote_http_ok(*, ssh_command, host, base_url, timeout=10):
        return base_url == "http://127.0.0.1:18789" and 18789 in started_ports

    def fake_run_checked(command, *, timeout=120, input_text=None):
        calls.append(command)
        forward = command[command.index("-R") + 1]
        if forward.startswith("127.0.0.1:18788:"):
            raise RuntimeError("Error: remote port forwarding failed for listen port 18788")
        if forward.startswith("127.0.0.1:18789:"):
            started_ports.add(18789)
            return None
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.run_checked = fake_run_checked
        runner.time.sleep = lambda _seconds: None

        summary = runner.ensure_remote_proxy(
            ssh_command=["ssh"],
            host="jinan-aws",
            local_proxy_base_url="http://127.0.0.1:8878",
            remote_proxy_base_url="http://127.0.0.1:18788",
            enabled=True,
        )
    finally:
        runner.remote_http_ok = old_remote_http_ok
        runner.local_http_ok = old_local_http_ok
        runner.run_checked = old_run_checked
        runner.time.sleep = old_sleep

    assert summary["status"] == "started_fallback_port"
    assert summary["remote_proxy_base_url"] == "http://127.0.0.1:18789"
    assert summary["selected_remote_port"] == 18789
    assert calls[0][calls[0].index("-R") + 1] == "127.0.0.1:18788:127.0.0.1:8878"
    assert calls[1][calls[1].index("-R") + 1] == "127.0.0.1:18789:127.0.0.1:8878"


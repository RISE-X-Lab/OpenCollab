"""Workflow CLI exit codes follow real runtime termination metadata."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.support.paths import PACKAGE_ROOT


@pytest.fixture
def completion_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            data = json.dumps({
                "id": "local-completion",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "local reply"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 450, "completion_tokens": 50, "total_tokens": 500},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def _cli(project, name, base_url, *arguments, unbounded=False):
    return subprocess.run(
        [sys.executable, "-m", "opencollab", "workflow", "run", name,
         "--workspace", str(project), "--provider", "openai", "--model", "fixture-model",
         "--api-key", "fixture-key", "--base-url", base_url, *arguments],  # pragma: allowlist secret
        cwd=project,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(PACKAGE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "240",
            "TERMINAL_WIDTH": "240",
            "OPENCOLLAB_UNBOUNDED_LIMITS": str(unbounded).lower(),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

def _payload(result):
    start = result.stdout.rfind("\n{") + 1
    return json.loads(result.stdout[start:])

def _write_two_reply_workflow(project):
    directory = project / "workflows"
    directory.mkdir()
    (directory / "two_replies.py").write_text(
        "from opencollab import workflow\n"
        "@workflow(name='two-replies')\n"
        "async def two_replies(ctx, inputs):\n"
        "    first = await ctx.agent('Reply briefly.', tools=[])\n"
        "    second = await ctx.agent('Reply briefly again.', tools=[])\n"
        "    return {'first': first, 'second': second}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("budget", [10_000, 500], ids=["completed", "budget-stop"])
@pytest.mark.parametrize("save", [True, False], ids=["save", "no-save"])
@pytest.mark.parametrize("trace", [True, False], ids=["trace", "no-trace"])
def test_cli_budget_exit_preserves_json_and_artifacts(tmp_path, completion_server, budget, save, trace):
    _write_two_reply_workflow(tmp_path)
    base_url, requests = completion_server
    result = _cli(
        tmp_path, "two-replies", base_url, "--budget", str(budget),
        "--save" if save else "--no-save", "--trace" if trace else "--no-trace",
    )

    stopped = budget == 500
    assert result.returncode == (1 if stopped else 0), result.stdout + result.stderr
    assert result.stderr == ""
    assert len(requests) == (1 if stopped else 2)
    assert _payload(result) == (
        {"status": "budget_exceeded", "error": "workflow budget exhausted: spent 500 of 500",
         "tokens_spent": 500, "budget_total": 500}
        if stopped else {"first": "local reply", "second": "local reply"}
    )
    sessions = tmp_path / ".opencollab" / "sessions"
    if save:
        manifests = list(sessions.glob("*/workflow.json"))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert manifest["status"] == ("stopped" if stopped else "completed")
        assert manifest["reason"] == ("budget_exceeded" if stopped else None)
        assert manifest["evidence_complete"] is True
        assert manifest["sessions"] == len(requests)
        assert manifest["tokens_spent"] == 500 * len(requests)
        assert manifest["trace_enabled"] is trace
        assert (manifests[0].parent / "orchestration.jsonl").exists() is trace
    else:
        assert not sessions.exists()


@pytest.mark.parametrize("budget", [None, 500], ids=["environment-default", "explicit-limit"])
def test_cli_honors_explicit_budget_in_an_unbounded_environment(tmp_path, completion_server, budget):
    _write_two_reply_workflow(tmp_path)
    base_url, requests = completion_server
    args = [] if budget is None else ["--budget", str(budget)]
    result = _cli(tmp_path, "two-replies", base_url, "--no-trace", *args, unbounded=True)

    stopped = budget is not None
    assert result.returncode == int(stopped), result.stdout + result.stderr
    assert len(requests) == (1 if stopped else 2)
    manifest, = (tmp_path / ".opencollab" / "sessions").glob("*/workflow.json")
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["budget_total"] == budget
    assert saved["status"] == ("stopped" if stopped else "completed")
    assert saved["tokens_spent"] == 500 * len(requests)
    if stopped:
        assert _payload(result)["status"] == "budget_exceeded"
        assert _payload(result)["tokens_spent"] == 500
    else:
        assert _payload(result) == {"first": "local reply", "second": "local reply"}


@pytest.mark.parametrize("status", ["budget_exceeded", "stopped", "failed"])
def test_cli_completed_callback_keeps_arbitrary_status_output(tmp_path, completion_server, status):
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "echo.py").write_text(
        "from opencollab import workflow\n"
        "@workflow(name='echo')\n"
        "async def echo(ctx, inputs):\n"
        "    return inputs\n",
        encoding="utf-8",
    )
    output = {"status": status, "error": "caller field", "value": "\u771f\u5b9e Unicode \U0001f30d"}
    base_url, requests = completion_server
    result = _cli(tmp_path, "echo", base_url, "--args", json.dumps(output), "--no-save")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _payload(result) == output
    assert requests == []

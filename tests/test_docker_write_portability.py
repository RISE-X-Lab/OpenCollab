"""BSD byte-count compatibility preserved from the maintenance release."""

from __future__ import annotations

import os
import subprocess

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters.env import DockerEnvironment

CONTAINER_ID = "a" * 64


async def test_verified_write_accepts_bsd_wc_padding(monkeypatch, tmp_path) -> None:
    """A BSD-style padded ``wc -c`` count still verifies the write."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_wc = fake_bin / "wc"
    fake_wc.write_text("#!/bin/sh\nprintf '      5\\n'\n", encoding="utf-8")
    fake_wc.chmod(0o755)
    child_env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    async def run_locally(command, *, timeout, input_bytes=None):
        completed = subprocess.run(
            ["bash", "-c", command],
            input=input_bytes,
            capture_output=True,
            cwd=workspace,
            env=child_env,
            check=False,
        )
        return ExecResult(
            completed.returncode,
            completed.stdout.decode(),
            completed.stderr.decode(),
        )

    async def discard_temporary(_temporary):
        return None

    env = DockerEnvironment(workspace=str(workspace), container_id=CONTAINER_ID)
    env._attached_bound = True
    monkeypatch.setattr(env, "_exec", run_locally)
    monkeypatch.setattr(env, "_discard_write_temporary", discard_temporary)

    await env.write_file(str(workspace / "padded.txt"), "hello")
    assert (workspace / "padded.txt").read_text(encoding="utf-8") == "hello"

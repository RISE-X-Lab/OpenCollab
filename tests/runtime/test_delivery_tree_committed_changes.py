"""Delivery snapshots compare against the initial run commit."""

import subprocess

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.working_tree import EnvWorkingTreeProbe


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


async def test_delivery_probe_sees_committed_and_checked_out_changes(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("value = 1\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    initial = _git(tmp_path, "rev-parse", "HEAD")
    env = LocalEnvironment(str(tmp_path))
    probe = EnvWorkingTreeProbe(env, from_initial_head=True)
    assert "+value" not in await probe.diff()
    (tmp_path / "app.py").write_text("value = 2\n")
    _git(tmp_path, "commit", "-qam", "candidate")
    committed = await probe.diff()
    assert "[Working tree status]\n(clean)" in committed
    assert "+value = 2" in committed
    _git(tmp_path, "checkout", "-q", "--detach", initial)
    assert "+value" not in await probe.diff()
    await env.cleanup()

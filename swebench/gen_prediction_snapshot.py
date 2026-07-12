"""Host-side installation and verification of solver Git snapshots."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gen_prediction_config import _docker_timeout_from_env
from gen_prediction_constants import DOCKER_WORKDIR
from gen_prediction_docker import _check_docker, _docker

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_CONTAINER_HELPER = "/tmp/opencollab_gen_prediction_snapshot.py"
_CONTAINER_HELPER_SOURCE = Path(__file__).with_name("gen_prediction_snapshot_container.py")
_CONTAINER_CONFIG_HELPER = "/tmp/gen_prediction_snapshot_config.py"
_CONTAINER_CONFIG_HELPER_SOURCE = Path(__file__).with_name("gen_prediction_snapshot_config.py")
_MAX_EVIDENCE_BYTES = 16 * 1024


def _docker_with_stdin(*args: str, input_text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=_docker_timeout_from_env(),
        check=False,
    )


@dataclass(frozen=True)
class SolverGitSnapshot:
    anonymous_head: str
    base_tree: str
    commit_count: int
    remote_count: int
    extra_git_metadata: int
    removed_git_metadata: int

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "enabled": True,
            "anonymous_head": self.anonymous_head,
            "base_tree": self.base_tree,
            "commit_count": self.commit_count,
            "remote_count": self.remote_count,
            "extra_git_metadata": self.extra_git_metadata,
            "removed_git_metadata": self.removed_git_metadata,
        }


def _parse_snapshot_output(output: str) -> SolverGitSnapshot:
    if len(output.encode("utf-8", errors="surrogatepass")) > _MAX_EVIDENCE_BYTES:
        raise RuntimeError("solver Git snapshot evidence exceeded its size bound")
    try:
        values = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("solver Git snapshot returned malformed evidence") from exc
    expected = {
        "enabled",
        "anonymous_head",
        "base_tree",
        "commit_count",
        "remote_count",
        "extra_git_metadata",
        "removed_git_metadata",
    }
    if not isinstance(values, dict) or set(values) != expected or values.get("enabled") is not True:
        raise RuntimeError("solver Git snapshot returned incomplete evidence")
    if _COMMIT_RE.fullmatch(str(values["anonymous_head"])) is None:
        raise RuntimeError("solver Git snapshot returned an invalid anonymous HEAD")
    if _COMMIT_RE.fullmatch(str(values["base_tree"])) is None:
        raise RuntimeError("solver Git snapshot returned an invalid base tree")
    if len(str(values["anonymous_head"])) != len(str(values["base_tree"])):
        raise RuntimeError("solver Git snapshot mixed object-id formats")
    for key in ("commit_count", "remote_count", "extra_git_metadata", "removed_git_metadata"):
        if isinstance(values[key], bool) or not isinstance(values[key], int):
            raise RuntimeError("solver Git snapshot returned non-integer evidence")
        if values[key] < 0:
            raise RuntimeError("solver Git snapshot returned negative evidence")
    snapshot = SolverGitSnapshot(
        anonymous_head=str(values["anonymous_head"]).lower(),
        base_tree=str(values["base_tree"]).lower(),
        commit_count=values["commit_count"],
        remote_count=values["remote_count"],
        extra_git_metadata=values["extra_git_metadata"],
        removed_git_metadata=values["removed_git_metadata"],
    )
    if snapshot.commit_count != 1 or snapshot.remote_count != 0 or snapshot.extra_git_metadata != 0:
        raise RuntimeError("solver Git snapshot integrity verification failed")
    return snapshot


def prepare_solver_git_snapshot(
    container_id: str,
    expected_base_commit: str,
    *,
    workspace: str = DOCKER_WORKDIR,
) -> SolverGitSnapshot:
    """Replace the image's Git history with one anonymous base-tree commit."""
    expected_base_commit = str(expected_base_commit or "").strip()
    if _COMMIT_RE.fullmatch(expected_base_commit) is None:
        raise ValueError("expected base commit must be a full hexadecimal object id")
    for source, destination in (
        (_CONTAINER_CONFIG_HELPER_SOURCE, _CONTAINER_CONFIG_HELPER),
        (_CONTAINER_HELPER_SOURCE, _CONTAINER_HELPER),
    ):
        install = _docker("cp", str(source), f"{container_id}:{destination}")
        _check_docker(install, "solver Git snapshot helper installation")
    result = _docker_with_stdin(
        "exec",
        "-i",
        container_id,
        "python3",
        _CONTAINER_HELPER,
        workspace,
        input_text=expected_base_commit.lower() + "\n",
    )
    _check_docker(result, "solver Git snapshot setup")
    return _parse_snapshot_output(result.stdout)


def anonymous_solver_task_id() -> str:
    """Return an opaque per-attempt task id that carries no dataset identity."""
    return "solver-" + secrets.token_hex(16)

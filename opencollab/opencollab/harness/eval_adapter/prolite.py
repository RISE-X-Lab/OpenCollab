"""SWE-batch pro-lite adapter helpers.

The helpers here are pure: they parse dataset rows, describe workspaces, and
classify technical failures without starting Docker or invoking an LLM.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from opencollab.harness.eval_adapter.models import (
    PatchCandidate,
    TaskSpec,
    WorkspaceSpec,
)

DEFAULT_DATASET_NAME = "swe-batch-pro-lite"
PROLITE_IMAGE_PREFIX = "docker.1panel.live/jefzda/sweap-images:"
DEFAULT_REPO_ROOT_CANDIDATES = ("/app", "/testbed")


def load_jsonl_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def task_spec_from_row(
    row: Mapping[str, Any],
    *,
    dataset: str = DEFAULT_DATASET_NAME,
) -> TaskSpec:
    instance_id = _first_string(row, "instance_id", "task_id", "id")
    dockerhub_tag = _first_string(row, "dockerhub_tag", "image_tag")
    docker_image = _first_string(row, "docker_image", "image")
    if not docker_image and dockerhub_tag:
        docker_image = f"{PROLITE_IMAGE_PREFIX}{dockerhub_tag}"

    repo = _first_string(row, "repo", "repository", "repo_name")
    problem_statement = _first_string(row, "problem_statement", "problem", "description")
    service_dependencies = _service_dependencies(row, instance_id, repo, docker_image)

    return TaskSpec(
        instance_id=instance_id,
        dataset=dataset,
        repo=repo,
        problem_statement=problem_statement,
        base_commit=_first_string(row, "base_commit", "commit"),
        docker_image=docker_image,
        dockerhub_tag=dockerhub_tag,
        fail_to_pass=_string_tuple(row.get("fail_to_pass") or row.get("FAIL_TO_PASS")),
        pass_to_pass=_string_tuple(row.get("pass_to_pass") or row.get("PASS_TO_PASS")),
        selected_test_files=_string_tuple(row.get("selected_test_files")),
        test_patch=str(row.get("test_patch") or ""),
        reference_patch=str(row.get("patch") or row.get("reference_patch") or ""),
        before_repo_set_cmd=_first_string(row, "before_repo_set_cmd", "setup_cmd"),
        service_dependencies=service_dependencies,
        metadata=dict(row),
    )


def workspace_spec_for_task(task: TaskSpec) -> WorkspaceSpec:
    candidates = task.metadata.get("repo_root_candidates")
    repo_root_candidates = _string_tuple(candidates) or DEFAULT_REPO_ROOT_CANDIDATES
    env = task.metadata.get("env")
    return WorkspaceSpec(
        image=task.docker_image,
        repo_root_candidates=repo_root_candidates,
        service_dependencies=task.service_dependencies,
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, Mapping) else {},
    )


def select_repo_root(existing_paths: Iterable[str]) -> str:
    existing = {str(path) for path in existing_paths}
    for candidate in DEFAULT_REPO_ROOT_CANDIDATES:
        if candidate in existing:
            return candidate
    return ""


def patch_candidate_from_diff(
    *,
    task: TaskSpec,
    solver_name: str,
    diff: str,
    record_id: str = "",
    log_path: str = "",
    token_count: int = 0,
    cost_usd: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
) -> PatchCandidate:
    return PatchCandidate(
        task_id=task.instance_id,
        solver_name=solver_name,
        patch=diff,
        record_id=record_id,
        log_path=log_path,
        token_count=token_count,
        cost_usd=cost_usd,
        metadata=dict(metadata or {}),
    )


def classify_technical_failure(
    *,
    log_text: str = "",
    returncode: int | None = None,
    status: str = "",
) -> tuple[str, ...]:
    text = f"{status}\n{log_text}".lower()
    reasons: list[str] = []

    if (
        ("127.0.0.1:6379" in text or "redis" in text)
        and _matches(text, "econnrefused", "connection refused", "connect failed")
    ):
        reasons.append("redis_unavailable")
    if _matches(text, "cannot connect to the docker daemon", "docker daemon", "docker.sock"):
        reasons.append("docker_unavailable")
    if _matches(text, "is already in use by container", "conflict. the container name"):
        reasons.append("docker_name_conflict")
    if _matches(text, "no such container", "container not found"):
        reasons.append("docker_container_missing")
    if "no such file or directory" in text and _matches(text, "/app", "/testbed"):
        reasons.append("workspace_root_missing")
    if _matches(text, "ssh: connect", "connection reset by peer", "connection timed out"):
        reasons.append("ssh_unavailable")
    if _matches(text, "timed out", "timeout", "deadline exceeded"):
        reasons.append("timeout")
    if _matches(text, "apply_patch", "test_patch", "patch failed", "does not apply"):
        reasons.append("test_patch_apply_failed")
    if _matches(text, "missing_report", "report not found", "no report"):
        reasons.append("missing_eval_report")

    if returncode not in (None, 0) and not reasons:
        reasons.append("process_failed")
    return tuple(dict.fromkeys(reasons))


def is_technical_failure(**kwargs: Any) -> bool:
    return bool(classify_technical_failure(**kwargs))


def _first_string(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped[0:1] in ("[", "{"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                return tuple(str(item) for item in decoded)
        return tuple(part.strip() for part in stripped.split(",") if part.strip())
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


def _service_dependencies(
    row: Mapping[str, Any],
    instance_id: str,
    repo: str,
    docker_image: str,
) -> tuple[str, ...]:
    explicit = _string_tuple(row.get("service_dependencies") or row.get("services"))
    if explicit:
        return explicit
    haystack = " ".join((instance_id, repo, docker_image)).lower()
    if "nodebb" in haystack:
        return ("redis",)
    return ()


def _matches(text: str, *needles: str) -> bool:
    return any(re.search(re.escape(needle.lower()), text) for needle in needles)

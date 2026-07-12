from __future__ import annotations

import inspect

from opencollab.harness import swe_v1_remote_evaluation
from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    remote_eval_script,
)


def test_ensure_image_pulls_missing_image(tmp_path):
    namespace = _remote_namespace(tmp_path)
    existing: set[str] = set()
    calls: list[list[str]] = []

    def fake_image_exists(image):
        return image in existing

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            existing.add(command[2])
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "unexpected"}

    namespace["image_exists"] = fake_image_exists
    namespace["run"] = fake_run

    result = namespace["ensure_image"]("example/image:tag")

    assert result["ok"] is True
    assert result["pulled"] is True
    assert calls == [["docker", "pull", "example/image:tag"]]


def test_image_for_row_uses_configured_repository_for_bare_tags(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        image_repository="registry.example/swebench",
    )

    assert namespace["image_for_row"]({"dockerhub_tag": "task-tag"}) == (
        "registry.example/swebench:task-tag"
    )
    assert namespace["image_for_row"]({"instance_id": "instance_task-id"}) == (
        "registry.example/swebench:task-id"
    )
    assert namespace["image_for_row"](
        {"dockerhub_tag": "public.example/team/image:tag"}
    ) == ("public.example/team/image:tag")


def test_image_exists_uses_bounded_docker_inspect(tmp_path):
    namespace = _remote_namespace(tmp_path)
    calls = []

    def fake_run(command, timeout=60):
        calls.append((command, timeout))
        return {"returncode": 124, "stdout": "", "stderr": "timed out"}

    namespace["run"] = fake_run

    assert namespace["image_exists"]("example/image:tag") is False
    assert calls == [(["docker", "image", "inspect", "example/image:tag"], 120)]


def test_image_workdir_preflight_is_offline_owned_and_cleaned_after_timeout(tmp_path):
    namespace = _remote_namespace(tmp_path)
    container_id = "a" * 64
    calls = []
    removed = False

    def fake_run(command, timeout=60):
        nonlocal removed
        calls.append((command, timeout))
        if command[:3] == ["timeout", "120", "docker"]:
            return {"returncode": 124, "stdout": "", "stderr": "timed out"}
        if command[:2] == ["docker", "inspect"]:
            reference = command[-1]
            if removed or reference == container_id:
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "No such container",
                }
            return {
                "returncode": 0,
                "stdout": (
                    f"{container_id}\t{namespace['owner_nonce']}\t"
                    f"{namespace['PREFLIGHT_SCHEMA']}"
                ),
                "stderr": "",
            }
        if command[:4] == ["docker", "rm", "-f", "--"]:
            removed = True
            return {"returncode": 0, "stdout": container_id, "stderr": ""}
        raise AssertionError(command)

    namespace["run"] = fake_run

    result = namespace["image_repo_workdir_status"]("registry.example/image:tag")

    docker_run = calls[0][0]
    assert result["ok"] is False
    assert result["container_cleanup"]["ok"] is True
    assert docker_run[docker_run.index("--network") + 1] == "none"
    assert "--cidfile" in docker_run
    assert "--name" in docker_run
    expected_owner = f"{namespace['PREFLIGHT_OWNER_LABEL']}={namespace['owner_nonce']}"
    assert expected_owner in docker_run
    assert ["docker", "rm", "-f", "--", container_id] in [
        call for call, _timeout in calls
    ]


def test_preflight_cleanup_refuses_container_without_matching_owner_label(tmp_path):
    namespace = _remote_namespace(tmp_path)
    container_id = "b" * 64
    calls = []

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            return {
                "returncode": 0,
                "stdout": (
                    f"{container_id}\tforeign-owner\t"
                    f"{namespace['PREFLIGHT_SCHEMA']}"
                ),
                "stderr": "",
            }
        raise AssertionError(command)

    namespace["run"] = fake_run
    cidfile = namespace["base_run_dir"] / "foreign.cid"

    result = namespace["cleanup_preflight_container"](
        cidfile,
        "foreign-container",
    )

    assert result["ok"] is False
    assert result["status"] == "ownership_unproven"
    assert all(command[:2] != ["docker", "rm"] for command in calls)


def test_remote_runner_bootstraps_redis_for_nodebb(tmp_path):
    namespace = _remote_namespace(tmp_path)

    script = namespace["prolite_service_bootstrap"]({"repo": "NodeBB/NodeBB"})

    assert "redis-server" in script
    assert "127.0.0.1:6379" in script
    assert namespace["prolite_service_bootstrap"]({"repo": "python/cpython"}) == ""


def test_prolite_eval_commands_use_separate_input_files_not_fixed_heredocs():
    source = inspect.getsource(swe_v1_remote_evaluation.eval_for_task_once)
    shell = remote_eval_script.DIRECT_EVAL_SCRIPT

    assert "<<'SERVICE'" not in source
    assert "<<'BEFORE'" not in source
    assert 'input_dir / "service_bootstrap.sh"' in source
    assert 'input_dir / "before_repo.sh"' in source
    assert "bash /eval_input/service_bootstrap.sh" in shell

from __future__ import annotations

import json
from pathlib import Path

from start_team_run_test_support import _run


def test_team_runner_stale_marker_cannot_remove_foreign_labeled_container(
    fake_team_repo,
):
    state = fake_team_repo["state"]
    state.mkdir(parents=True, exist_ok=True)
    name = "oc-team-foreign"
    container_id = "b" * 64
    owner_nonce = "a" * 32
    (state / "team_container.owner").write_text(
        json.dumps(
            {
                "schema": "opencollab.team-owner.v1",
                "session_key": state.name,
                "container_name": name,
                "container_id": container_id,
                "owner_nonce": owner_nonce,
            }
        ),
        encoding="utf-8",
    )
    docker_state = fake_team_repo["root"] / "docker-state"
    (fake_team_repo["root"] / "docker-state.exists").touch()
    (fake_team_repo["root"] / "docker-state.name").write_text(name, encoding="utf-8")
    (fake_team_repo["root"] / "docker-state.cid").write_text(
        container_id,
        encoding="utf-8",
    )
    (fake_team_repo["root"] / "docker-state.label").write_text(
        "f" * 32,
        encoding="utf-8",
    )

    result = _run(fake_team_repo)

    assert result.returncode != 0
    docker_log = fake_team_repo["log"].read_text(encoding="utf-8")
    assert "rm -f" not in docker_log
    assert Path(f"{docker_state}.exists").exists()
    assert (state / "team_container.owner").exists()

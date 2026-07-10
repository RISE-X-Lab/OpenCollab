from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2]
TEAM_RUNNER = SOURCE_ROOT / "scripts" / "start_team_run.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def fake_team_repo(tmp_path):
    root = (tmp_path / "repo").resolve()
    (root / "scripts").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "swebench").mkdir()
    (root / "opencollab" / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(TEAM_RUNNER, root / "scripts" / TEAM_RUNNER.name)
    (root / "configs" / "team.self.collab.yaml").write_text("agents: []\n")
    (root / "configs" / ".env").write_text("\n")
    (root / "swebench" / "gen_prediction.py").write_text(
        "MAX_EXTRACTED_PATCH_BYTES = 8 * 1024 * 1024\n"
        "def bounded_container_output_command(command, *, max_bytes, label, helper_path=None):\n"
        "    return 'true'\n",
        encoding="utf-8",
    )
    shutil.copy2(
        SOURCE_ROOT / "swebench" / "gen_prediction_bounded_capture.py",
        root / "swebench" / "gen_prediction_bounded_capture.py",
    )
    (root / "scripts" / "swebench_loop_monitor.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    os.symlink(sys.executable, root / "opencollab" / ".venv" / "bin" / "python")
    _write_executable(
        root / "opencollab" / ".venv" / "bin" / "opencollab",
        "#!/bin/sh\nexit 0\n",
    )

    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "timeout",
        """#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
    case "$1" in
        --foreground|--kill-after=*) shift ;;
        *) break ;;
    esac
done
[ "$#" -ge 2 ] || exit 125
shift
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:?}"
shift
printf '%s %s\n' "$cmd" "$*" >> "$FAKE_DOCKER_LOG"
case "$cmd" in
    run)
        name=""
        label=""
        previous=""
        for arg in "$@"; do
            if [ "$previous" = "--name" ]; then name="$arg"; fi
            if [ "$previous" = "--label" ] && [[ "$arg" == opencollab.harness.owner-token=* ]]; then
                label="${arg#*=}"
            fi
            previous="$arg"
        done
        printf '%s\n' "$name" > "$FAKE_DOCKER_STATE.name"
        printf '%s\n' "$label" > "$FAKE_DOCKER_STATE.label"
        printf '%064d\n' 0 | tr '0' 'a' > "$FAKE_DOCKER_STATE.cid"
        : > "$FAKE_DOCKER_STATE.exists"
        if [ "${FAKE_RUN_CONFLICT:-0}" = "1" ]; then
            printf '%032d\n' 0 | tr '0' 'f' > "$FAKE_DOCKER_STATE.label"
            echo 'Conflict. The container name is already in use.' >&2
            exit 125
        fi
        if [ "${FAKE_RUN_INVALID_CID:-0}" = "1" ]; then
            echo 'invalid-cid'
            exit 0
        fi
        printf '%064d\n' 0 | tr '0' 'a'
        ;;
    cp)
        exit 0
        ;;
    exec)
        args=" $* "
        if [[ "$args" == *"container_process_guard.sh stop"* ]]; then
            exit "${FAKE_STOP_RC:-0}"
        fi
        if [[ "$args" == *"container_process_guard.sh run"* ]]; then
            if [[ "$args" == *".diff"* ]]; then
                printf 'diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-old\n+new\n'
                exit 0
            fi
            exit "${FAKE_AGENT_RC:-0}"
        fi
        exit 0
        ;;
    rm)
        if [ "${FAKE_CLEANUP_MODE:-ok}" = "unknown" ]; then exit 1; fi
        rm -f "$FAKE_DOCKER_STATE.exists"
        exit 0
        ;;
    inspect)
        if [ "${FAKE_CLEANUP_MODE:-ok}" = "unknown" ]; then
            echo 'Cannot connect to the Docker daemon' >&2
            exit 2
        fi
        if [ -e "$FAKE_DOCKER_STATE.exists" ]; then
            if [[ " $* " == *" --format "* ]]; then
                cid="$(cat "$FAKE_DOCKER_STATE.cid")"
                name="$(cat "$FAKE_DOCKER_STATE.name")"
                label="$(cat "$FAKE_DOCKER_STATE.label")"
                printf '%s\t/%s\t%s\n' "$cid" "$name" "$label"
            else
                cat "$FAKE_DOCKER_STATE.cid"
            fi
            exit 0
        fi
        echo 'Error: No such object: fake' >&2
        exit 1
        ;;
    *)
        echo "unexpected fake docker command: $cmd" >&2
        exit 2
        ;;
esac
""",
    )

    instance = root / "instance.json"
    instance.write_text(
        json.dumps(
            {
                "instance_id": "demo__task-1",
                "repo": "demo/repo",
                "problem_statement": "fix it",
                "FAIL_TO_PASS": [],
            }
        ),
        encoding="utf-8",
    )
    session = root / "session"
    state_key = hashlib.sha256(
        (str(session) + "\0" + "demo__task-1").encode("utf-8")
    ).hexdigest()
    harness_state = root / ".opencollab" / "harness_state" / "team_runs" / state_key
    output = root / "predictions.jsonl"
    log = root / "docker.log"
    state = root / "docker-state"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_STATE": str(state),
        }
    )
    return {
        "root": root,
        "runner": root / "scripts" / "start_team_run.sh",
        "instance": instance,
        "session": session,
        "state": harness_state,
        "output": output,
        "log": log,
        "env": env,
    }


def _run(
    fake_team_repo,
    *,
    output: Path | None = None,
    session_root: Path | None = None,
    env: dict[str, str] | None = None,
):
    values = fake_team_repo
    run_env = values["env"].copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [
            "bash",
            str(values["runner"]),
            "--instance-file",
            str(values["instance"]),
            "--output",
            str(output or values["output"]),
            "--team-file",
            str(values["root"] / "configs" / "team.self.collab.yaml"),
            "--session-root",
            str(session_root or values["session"]),
            "--timeout",
            "5",
        ],
        cwd=values["root"],
        env=run_env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _seed_pending_prediction(
    fake_team_repo,
    *,
    output: Path | None = None,
    omit_integrity_field: str | None = None,
) -> dict:
    state = fake_team_repo["state"]
    state.mkdir(parents=True, exist_ok=True)
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+recovered\n"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    metric = {
        "instance_id": "demo__task-1",
        "record_id": "a" * 32,
        "patch_sha256": digest,
        "workflow_status": "done",
        "runner_returncode": 0,
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": True,
    }
    if omit_integrity_field is not None:
        metric.pop(omit_integrity_field)
    record = {
        "instance_id": "demo__task-1",
        "record_id": "a" * 32,
        "patch_sha256": digest,
        "model_name_or_path": "team",
        "model_patch": patch,
        "workflow_metric": metric,
    }
    envelope = {
        "schema": "opencollab.pending-prediction.v1",
        "output_path": os.path.abspath(output or fake_team_repo["output"]),
        "record": record,
    }
    (state / "pending_prediction.record.json").write_text(
        json.dumps(envelope),
        encoding="utf-8",
    )
    (state / "pending_prediction.patch").write_text(patch, encoding="utf-8")
    return record

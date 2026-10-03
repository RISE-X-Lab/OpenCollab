"""The declaration a prebuilt run opens with names each seat's model.

``assigned.topology_nodes`` already said who was seated, with which tools and
under what shell; it did not say which model each seat ran, so a team whose
roles override ``model:`` could not be read back from its own record.
"""

from __future__ import annotations

import json

from opencollab.adapters.trace import Tracer
from opencollab.bootstrap import build_runtime_context, build_scheduler
from tests.support.prebuilt_team_test_support import CONFIG

TEAM = (
    "entry: lead\nroles:\n"
    "  lead:\n    prompt: hi\n    tools: [message_agent]\n    model: model-for-lead\n"
    "  coder:\n    prompt: hi\n    tools: [message_agent]\n"
    "topology:\n  lead: [coder]\n  coder: [lead]\n"
)


async def test_each_declared_node_names_its_model(tmp_path) -> None:
    team_file = tmp_path / "team.yaml"
    team_file.write_text(TEAM, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    tracer = Tracer("run", output_dir=str(tmp_path), filename="trajectory.jsonl")
    ctx.tracer = tracer
    scheduler = build_scheduler(
        ctx, use_worktrees=False, interactive=False, prebuild_team=True,
        team_config_path=str(team_file),
    )
    try:
        await scheduler.ensure_team_prebuilt()
        tracer.flush()
    finally:
        await scheduler.cleanup()
        tracer.close()
    records = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    (nodes,) = [r["payload"] for r in records if r["type"] == "assigned.topology_nodes"]
    assert {n["role"]: n["model"] for n in nodes["nodes"]} == {
        "lead": "model-for-lead",
        "coder": CONFIG["model"],
    }

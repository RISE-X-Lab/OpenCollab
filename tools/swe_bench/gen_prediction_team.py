"""Generate a SWE-bench prediction with an OpenCollab *team*.

Team-driven sibling of ``gen_prediction.py``. Same lifecycle and output — for one
SWE-bench instance it starts the official ``sweb.eval`` image, runs OpenCollab
inside ``/testbed``, captures ``git diff`` as the model patch, and appends one
``{instance_id, model_name_or_path, model_patch}`` line to a predictions JSONL —
but instead of a single agent it runs the collaboration team defined in
``configs/team.yaml`` (lead + analyst/coder/reviewer) through the Scheduler.

The whole team shares ONE container working tree, so every edit lands in
``/testbed`` and is captured by the final diff (worktrees are deliberately not
used — their diffs never reach ``/testbed``).

Reuses the container plumbing from ``gen_prediction.py`` (importing it also
installs the no-op ``FileLock`` patch and puts the ``opencollab`` package on
``sys.path``).

Run with the OpenCollab venv::

    opencollab/.venv/bin/python tools/swe_bench/gen_prediction_team.py \
        --instance-file /home/xuzhenhua/swebench-eval/instance_sympy-20590.json \
        --output /home/xuzhenhua/swebench-eval/predictions-team.jsonl

Grade with the official harness exactly as for ``gen_prediction.py`` (point
``-p`` at the team predictions file and ``--model-name``/``-id`` at the team name).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Import the single-agent runner first: it inserts the opencollab package onto
# sys.path and monkeypatches FileWriteTool's FileLock to a no-op (the lock file
# would otherwise be created at a /testbed path that does not exist on the host).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import gen_prediction as gp  # noqa: E402  (also sets up sys.path + FileLock patch)

_REPO_ROOT = gp._REPO_ROOT

from opencollab.adapters.storage import SessionStore  # noqa: E402
from opencollab.adapters.trace import Tracer  # noqa: E402
from opencollab.application.event_bus import EventBus  # noqa: E402
from opencollab.application.scheduler import LaunchSpec, Scheduler  # noqa: E402
from opencollab.bootstrap.config import get_config, load_config_env  # noqa: E402
from opencollab.bootstrap.container import (  # noqa: E402
    DefaultSessionFactory,
    SpawnConfig,
    agent_save_path,
    build_session,
    make_run_dir,
)
from opencollab.bootstrap.team_config import (  # noqa: E402
    TeamConfig,
    load_team_config,
    resolve_team_file,
)


class _SharedContainerPool:
    """WorktreePoolPort that hands every spawned agent the SAME container env.

    SWE-bench grades a single ``git diff`` of ``/testbed``, so all agents must
    edit one shared tree — never an isolated worktree. The container's lifecycle
    is owned by ``main``; release/cleanup are no-ops here.
    """

    def __init__(self, env: object) -> None:
        self._env = env

    async def acquire(self, role: str) -> object:
        return self._env

    async def release(self) -> None:
        return None

    async def cleanup(self) -> None:
        return None


class _ContainerSessionFactory(DefaultSessionFactory):
    """Session factory whose lead runs against the container instead of a
    ``LocalEnvironment``. Spawned children already receive their env from the
    pool; ``safety_policy_factory=None`` (set on the SpawnConfig) keeps them
    free of the host-path sandbox, which cannot resolve ``/testbed``.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        cid: str,
        team_cfg: TeamConfig | None = None,
        save_dir: str | None = None,
        lead_max_steps: int = 60,
    ) -> None:
        super().__init__(
            cfg,
            team_cfg=team_cfg,
            lead_workspace=None,
            interactive=False,
            save_dir=save_dir,
        )
        self._cid = cid
        self._lead_max_steps = lead_max_steps

    def create_lead_session(self, *, scheduler, launch, budget, aid: int = 0):
        cfg = self._cfg
        env = gp.ContainerEnv(self._cid)
        agent = self._context_builder.build_agent(
            "lead", scheduler=scheduler, interactive=False
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            max_steps=self._lead_max_steps,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=None,
            auto_save_path=launch.auto_save_path,
            aid=aid,
        )


class _PrintSink:
    """Minimal headless event sink: prints scheduler-level team activity and
    silently drops the high-volume per-session run-loop/tool events.
    """

    async def emit(self, event: object) -> None:
        etype = getattr(event, "type", "")
        data = getattr(event, "data", None) or {}
        if etype == "agent_spawned":
            print(
                f"  [+] spawn aid={data.get('aid')} role={data.get('role')}: "
                f"{str(data.get('task', ''))[:80]}"
            )
        elif etype == "agent_completed":
            print(
                f"  [done] aid={data.get('aid')} role={data.get('role')} "
                f"({data.get('result_len', 0)} chars)"
            )
        elif etype == "agent_failed":
            print(
                f"  [fail] aid={data.get('aid')} role={data.get('role')}: "
                f"{str(data.get('error', ''))[:120]}"
            )
        elif etype == "review_started":
            print(f"  [review] iteration {data.get('iteration')}/{data.get('max')}")
        elif etype == "review_completed":
            print(
                f"  [review] iteration {data.get('iteration')} -> "
                f"{data.get('verdict')}"
            )


def _wire_manifest(scheduler: Scheduler, run_dir: str) -> None:
    """Persist a team.json manifest on every roster change (parity with the CLI
    team runs wired in bootstrap.container.build_scheduler).
    """
    store = SessionStore()
    manifest_path = os.path.join(run_dir, "team.json")
    team_file = resolve_team_file(str(_REPO_ROOT))
    run_id = os.path.basename(run_dir)
    started_at = datetime.now(timezone.utc).isoformat()

    def _write() -> None:
        store.save_manifest(
            manifest_path,
            {
                "run_id": run_id,
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "team_file": str(team_file) if team_file else None,
                "agents": scheduler.team_snapshot(),
            },
        )

    scheduler.set_manifest_writer(_write)


async def run_team(
    task: str,
    cid: str,
    cfg: dict,
    team_cfg: TeamConfig,
    *,
    budget: int,
    max_steps: int,
    timeout: float,
    tracer: Tracer,
    run_dir: str | None,
) -> None:
    env = gp.ContainerEnv(cid)
    event_bus = EventBus(_PrintSink())
    spawn_cfg = SpawnConfig(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        tracer=tracer,
        event_bus=event_bus,
        permission_policy=None,
        safety_policy_factory=None,
    )
    factory = _ContainerSessionFactory(
        spawn_cfg, cid=cid, team_cfg=team_cfg, save_dir=run_dir, lead_max_steps=max_steps
    )
    scheduler = Scheduler(
        session_factory=factory,
        worktree_pool=_SharedContainerPool(env),
        event_sink=event_bus,
        tracer=tracer,
        max_budget_tokens=budget,
        permission_policy=None,
        topology=team_cfg.topology,
        roles=tuple(team_cfg.roles),
    )
    if run_dir is not None:
        _wire_manifest(scheduler, run_dir)

    lead_save_path = agent_save_path(run_dir, 0, "lead") if run_dir else None
    scheduler.create_init_process(LaunchSpec(auto_save_path=lead_save_path))
    if lead_save_path:
        print(f"  lead autosave: {lead_save_path}")

    try:
        await asyncio.wait_for(scheduler.run(task), timeout=timeout)
    except asyncio.TimeoutError:
        print("  team: wall-clock timeout reached, capturing current diff")
    finally:
        await scheduler.cleanup()
    print(f"  team: tokens={scheduler.used_tokens}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one SWE-bench prediction with an OpenCollab team"
    )
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    ap.add_argument("--team-file", default=None, help="Team YAML (default configs/team.yaml)")
    ap.add_argument("--max-steps", type=int, default=60, help="Lead agent step cap")
    ap.add_argument("--budget", type=int, default=400_000, help="Total team token budget")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()

    instance = json.loads(Path(args.instance_file).read_text())
    iid = instance["instance_id"]
    image = args.image or f"sweb.eval.{args.arch}.{iid}:latest"

    team_file = args.team_file or str(_REPO_ROOT / "configs" / "team.yaml")
    os.environ["OPENCOLLAB_TEAM_FILE"] = team_file
    team_cfg = load_team_config(str(_REPO_ROOT))

    cfg = get_config(str(_REPO_ROOT))
    # get_config() resolves os.environ before configs/.env, so a stray
    # ANTHROPIC_API_KEY/OPENAI_API_KEY in the shell shadows the real key in the
    # env file. Prefer the env-file key (same resolution as gen_prediction.py).
    env_file = load_config_env(str(_REPO_ROOT))
    file_key = env_file.get("DASHSCOPE_API_KEY") or env_file.get("OPENCOLLAB_API_KEY")
    if file_key:
        cfg["api_key"] = file_key
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    model_name = args.model_name or f"opencollab-team-{cfg['model']}"

    print(f"Instance:  {iid}")
    print(f"Image:     {image}")
    print(f"Model:     {cfg['model']} (provider={cfg['provider']})")
    print(f"Team file: {team_file}")
    print(f"Roles:     {', '.join(team_cfg.roles)}")

    tracer = Tracer(
        run_id=f"swe_team_{uuid.uuid4().hex[:8]}",
        output_dir=str(_REPO_ROOT / "logs" / "trajectories"),
    )
    run_dir = make_run_dir(str(_REPO_ROOT))

    name = f"oc-team-{iid}-{uuid.uuid4().hex[:6]}"[:60]
    cid = gp.start_container(image, name)
    print(f"Container: {cid}")
    try:
        task = gp.build_task(instance)
        asyncio.run(
            run_team(
                task, cid, cfg, team_cfg,
                budget=args.budget, max_steps=args.max_steps,
                timeout=args.timeout, tracer=tracer, run_dir=run_dir,
            )
        )
        patch = gp.extract_patch(cid)
    finally:
        tracer.close()
        if not args.keep_container:
            gp.remove_container(cid)
        else:
            print(f"  (left container {cid} running: {name})")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "instance_id": iid,
        "model_name_or_path": model_name,
        "model_patch": patch,
    }
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (team made no tracked changes)")


if __name__ == "__main__":
    main()

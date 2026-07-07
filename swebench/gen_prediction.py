"""Generate a SWE-bench prediction with an OpenCollab agent.

Host-runnable bridge between the OpenCollab agent framework and the official
SWE-bench evaluation harness. For one SWE-bench instance it:

  1. starts the official ``sweb.eval`` image as a container (repo baked at
     /testbed, deps installed in the ``testbed`` conda env),
  2. runs a single OpenCollab agent inside it (edits + can run tests),
  3. captures ``git diff`` as the model patch,
  4. appends one ``{instance_id, model_name_or_path, model_patch}`` line to a
     predictions JSONL.

Grade the result with the official harness, e.g.::

    cd /home/xuzhenhua/swebench-eval
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-opencollab.jsonl -i sympy__sympy-20590 \
        -id oc-kimi --cache_level env --report_dir reports

Run with the OpenCollab venv (it must import ``opencollab``)::

    opencollab/.venv/bin/python swebench/gen_prediction.py \
        --instance-file /home/xuzhenhua/swebench-eval/instance_sympy-20590.json \
        --output /home/xuzhenhua/swebench-eval/predictions-opencollab.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# Make the opencollab package importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "opencollab"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.adapters.tools.bash import BashTool  # noqa: E402
from opencollab.adapters.tools.fs import (  # noqa: E402
    FileReadTool,
    FileWriteTool,
    GrepTool,
)
from opencollab.adapters.trace import Tracer  # noqa: E402
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.bootstrap.container import (  # noqa: E402
    agent_save_path,
    build_session,
    make_run_dir,
)
from opencollab.domain.agent import Agent  # noqa: E402

DOCKER_WORKDIR = "/testbed"
# Activate the testbed conda env so the agent's `python`/tests see the repo deps.
_ACTIVATE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true"

AGENT_PROMPT = """\
You are an autonomous software engineer fixing a real bug in a Python repository.
The repository is checked out at /testbed and all dependencies are installed.

Rules:
- Explore briefly to find the root cause (a few grep/file_read calls), then ACT.
- As soon as you know the fix, APPLY it with the file_write tool (str_replace
  mode is best for a targeted edit). Diagnosing is not enough — you MUST edit
  the source file. Do not keep exploring once the cause is clear.
- Make the smallest correct change to the SOURCE code that fixes the issue.
- Do NOT edit test files — your fix is graded against the project's own tests.
- After editing, verify with a quick Python snippet that the reported behavior
  is fixed, then stop.
- Do NOT run `git commit`. Just leave your edits in the working tree.
"""


def _docker(*args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = int(os.environ.get("OPENCOLLAB_DOCKER_TIMEOUT", "60"))
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _check_docker(res: subprocess.CompletedProcess, action: str) -> None:
    if res.returncode == 0:
        return
    detail = (res.stderr or res.stdout).strip()
    raise RuntimeError(f"{action} failed (exit {res.returncode}): {detail}")


def start_container(image: str, name: str) -> str:
    res = _docker("run", "-d", "--name", name, "--entrypoint", "", image,
                  "tail", "-f", "/dev/null")
    if res.returncode != 0:
        raise RuntimeError(f"docker run failed: {res.stderr.strip()}")
    cid = res.stdout.strip()[:12]
    ensure_workdir = _docker(
        "exec", cid, "bash", "-lc",
        """
set -e
if [ -e /testbed/.git ]; then
  exit 0
fi
if { [ -e /testbed ] || [ -L /testbed ]; } && [ ! -e /testbed/.git ]; then
  rm -rf /testbed
fi
if [ ! -e /testbed ]; then
  for d in /app /workspace /repo /src; do
    if [ -e "$d/.git" ]; then
      ln -s "$d" /testbed
      exit 0
    fi
  done
  found=$(find / -maxdepth 3 -name .git 2>/dev/null | head -1 || true)
  if [ -n "$found" ]; then
    ln -s "$(dirname "$found")" /testbed
    exit 0
  fi
fi
echo "unable to prepare /testbed: no repository checkout found" >&2
exit 2
""",
    )
    try:
        _check_docker(ensure_workdir, "docker /testbed workdir setup")
        # Repo is owned by root in the image; allow git to operate on it.
        safe_dir = _docker("exec", cid, "bash", "-lc",
                           f"git config --global --add safe.directory {DOCKER_WORKDIR}")
        _check_docker(safe_dir, "docker git safe.directory setup")
    except Exception:
        remove_container(cid)
        raise
    return cid


def remove_container(cid: str) -> None:
    """Best-effort teardown of a throwaway container.

    Cleanup must NEVER lose an already-computed result: under heavy daemon load
    ``docker rm`` can exceed its timeout and raise ``TimeoutExpired``, which —
    when this runs in a ``finally`` after the workflow has finished and the patch
    has been extracted — would propagate and abort the caller before the
    prediction is persisted (observed: a healthy run lost to a 30s ``rm`` timeout
    at load ~42). A leaked container is recoverable; a dropped prediction is not.
    Swallow any failure with a warning instead.
    """
    try:
        _docker("rm", "-f", cid, timeout=30)
    except Exception as exc:  # noqa: BLE001 — teardown must never propagate
        print(f"  warning: container cleanup failed for {cid}: {exc!r} "
              f"(best-effort, continuing)")


def build_task(instance: dict) -> str:
    problem = instance["problem_statement"]
    f2p = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    tests = "\n".join(f"- {t}" for t in f2p)
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n\n"
        f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n\n"
        "Locate the root cause in the source, apply a minimal fix, and ensure the "
        "behavior described above is satisfied."
    )


async def run_agent(task: str, cid: str, cfg: dict, max_steps: int, budget: int,
                    timeout: float) -> str:
    env = DockerEnvironment(
        container_id=cid,
        workspace=DOCKER_WORKDIR,
        exec_workdir=DOCKER_WORKDIR,
        command_prefix=_ACTIVATE,
        timeout_returncode=124,
    )
    agent = Agent(
        name="swe_agent",
        system_prompt=AGENT_PROMPT,
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        thinking=cfg.get("thinking", False),
        thinking_params=cfg.get("thinking_params") or {},
    )
    tracer = Tracer(run_id=f"swe_{uuid.uuid4().hex[:8]}",
                    output_dir=str(_REPO_ROOT / "logs" / "trajectories"))
    # Autosave a structured per-agent session JSON under the standard
    # .opencollab/sessions/<timestamp>/ run folder (same convention as team runs).
    run_dir = make_run_dir(str(_REPO_ROOT))
    save_path = agent_save_path(run_dir, 0, agent.name)
    session = build_session(
        agent=agent, env=env, tracer=tracer,
        max_budget_tokens=budget, max_steps=max_steps,
        auto_save_path=save_path,
    )
    print(f"  session autosave: {save_path}")
    await session.add_user_message(task)
    try:
        await asyncio.wait_for(session.run_loop(), timeout=timeout)
    except asyncio.TimeoutError:
        print("  agent: wall-clock timeout reached, capturing current diff")
    print(f"  agent: steps={session.step_count} tokens={session.used_tokens}")
    return ""


def extract_patch(cid: str) -> str:
    # Stage everything so new files are included, then diff against HEAD.
    add_result = _docker("exec", "-w", DOCKER_WORKDIR, cid, "bash", "-lc", "git add -A")
    _check_docker(add_result, "git add -A before patch extraction")
    res = _docker("exec", "-w", DOCKER_WORKDIR, cid, "bash", "-lc",
                  "git diff --cached")
    _check_docker(res, "git diff --cached during patch extraction")
    if not res.stdout.strip():
        status = _docker("exec", "-w", DOCKER_WORKDIR, cid, "bash", "-lc",
                         "git status --short")
        _check_docker(status, "git status --short after empty patch")
        print("  patch extraction: staged diff empty")
        print(f"  git status --short: {status.stdout.strip() or '(clean)'}")
    return res.stdout


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate one SWE-bench prediction with OpenCollab")
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--budget", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()

    instance = json.loads(Path(args.instance_file).read_text())
    iid = instance["instance_id"]
    image = args.image or f"sweb.eval.{args.arch}.{iid}:latest"

    cfg = get_config(str(_REPO_ROOT))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    model_name = args.model_name or f"opencollab-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")

    name = f"oc-gen-{iid}-{uuid.uuid4().hex[:6]}"[:60]
    cid = start_container(image, name)
    print(f"Container: {cid}")
    try:
        task = build_task(instance)
        asyncio.run(run_agent(task, cid, cfg, args.max_steps, args.budget, args.timeout))
        patch = extract_patch(cid)
    finally:
        if not args.keep_container:
            remove_container(cid)
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
        print("\nWARNING: empty patch (agent made no tracked changes)")


if __name__ == "__main__":
    main()

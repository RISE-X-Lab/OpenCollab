"""Generate a SWE-bench prediction via the harness workflow mode (A/B driver).

Same container plumbing as ``gen_prediction.py`` (official ``sweb.eval`` image,
repo at /testbed, ``testbed`` conda env), but instead of one bespoke agent
session it drives ``run_eval_task(task, workflow=generate_review_fix)`` —
implement -> structured review verdict -> conditional fix — so the prediction
exercises the mini workflow engine end-to-end. This is the A/B candidate
against the 61.7% team baseline (`opencollab-team.oc-team.json`).

Baseline-input parity: the task prompt forwards ``hints_text`` and warns that
FAIL_TO_PASS tests may not exist yet (the with-hints team baseline saw both).
Patch extraction is also baseline-identical: ``git add -A && git diff --cached``
inside the container (includes new files), NOT the ``git diff`` that
``run_eval_task`` captures internally.

Generate (OpenCollab venv, absolute paths in background shells)::

    opencollab/.venv/bin/python swebench/gen_prediction_workflow.py \
        --instance-file /home/xuzhenhua/swebench-eval/instance_sympy-20590.json \
        --output /home/xuzhenhua/swebench-eval/predictions-review-fix.jsonl

Grade with the official harness (separate venv)::

    cd /home/xuzhenhua/swebench-eval && HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-review-fix.jsonl -i sympy__sympy-20590 \
        -id review-fix-1 --cache_level env --report_dir reports
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

# Make the opencollab package importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "opencollab"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import gen_prediction as gp  # noqa: E402 — shared container plumbing

from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.harness.evaluator import EvalTask, run_eval_task  # noqa: E402
from opencollab.harness.workflows import generate_review_fix  # noqa: E402

# Team-baseline parity: 500k tokens per instance was the binding constraint in
# the oc-team run this A/B compares against.
DEFAULT_BUDGET = 500_000
DEFAULT_MAX_STEPS = 60  # per workflow session; 60 proved enough to act, 40 did not
DEFAULT_TIMEOUT = 1800.0  # the workflow runs up to 3 sequential sessions


def build_task(instance: dict) -> str:
    """Issue prompt with hints_text forwarded and the FAIL_TO_PASS caveat.

    Matches the inputs the with-hints team baseline saw; keep in sync if that
    baseline's prompt builder changes.
    """
    problem = instance["problem_statement"]
    f2p = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    tests = "\n".join(f"- {t}" for t in f2p)
    hints = (instance.get("hints_text") or "").strip()
    hints_block = (
        f"\n## Hints (from the issue discussion — may help locate the cause)\n{hints}\n"
        if hints
        else ""
    )
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n{hints_block}\n"
        f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n"
        "Note: some of these tests may not exist yet at this commit — they are "
        "added by the grading harness. Do not be surprised if you cannot run "
        "them; verify the fixed behavior directly instead.\n\n"
        "Locate the root cause in the source, apply a minimal fix, and ensure "
        "the behavior described above is satisfied."
    )


async def generate(instance: dict, image: str, cfg: dict, args: argparse.Namespace) -> tuple[str, dict]:
    """Run the review-fix workflow in a fresh container; return (patch, metrics)."""
    iid = instance["instance_id"]
    name = f"oc-wf-{iid}-{uuid.uuid4().hex[:6]}"[:60]
    cid = gp.start_container(image, name)
    print(f"Container: {cid}")
    try:
        # Attach mode: run_eval_task's internal env.cleanup() no-ops on attached
        # containers, so the container survives for baseline-style extraction.
        env = DockerEnvironment(
            container_id=cid,
            workspace=gp.DOCKER_WORKDIR,
            exec_workdir=gp.DOCKER_WORKDIR,
            command_prefix=gp._ACTIVATE,
            timeout_returncode=124,
        )

        async def env_factory(_task: EvalTask) -> DockerEnvironment:
            return env

        task = EvalTask(
            task_id=iid,
            description=build_task(instance),
            timeout=args.timeout,
            max_tokens=args.budget,
        )
        result = await run_eval_task(
            task,
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            output_dir=str(_REPO_ROOT / "logs" / "eval_workflow"),
            prompt=gp.AGENT_PROMPT,
            env_factory=env_factory,
            max_steps=args.max_steps,
            workflow=generate_review_fix,
        )
        print(
            f"  workflow: tokens={result.tokens_used} steps={result.steps} "
            f"duration={result.duration:.0f}s error={result.error}"
        )
        patch = gp.extract_patch(cid)
    finally:
        if not args.keep_container:
            gp.remove_container(cid)
        else:
            print(f"  (left container {cid} running: {name})")

    metrics = {k: v for k, v in asdict(result).items() if k != "patch"}
    return patch, metrics


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one SWE-bench prediction with the review-fix workflow"
    )
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument("--metrics", default=None,
                    help="Metrics JSONL to append to (default: <output>.metrics.jsonl)")
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                    help="Step cap per workflow session")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="Shared token budget across all workflow sessions")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
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
    model_name = args.model_name or f"opencollab-review-fix-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")
    print(f"Workflow: generate_review_fix (budget={args.budget}, "
          f"max_steps/session={args.max_steps})")

    patch, metrics = asyncio.run(generate(instance, image, cfg, args))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "instance_id": iid,
        "model_name_or_path": model_name,
        "model_patch": patch,
    }
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    metrics_path = Path(args.metrics or f"{args.output}.metrics.jsonl")
    with metrics_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**metrics, "model_name_or_path": model_name}) + "\n")

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (workflow made no tracked changes)")


if __name__ == "__main__":
    main()

"""Generate a SWE-bench prediction via the harness workflow mode (A/B driver).

Same container plumbing as ``gen_prediction.py`` (official ``sweb.eval`` image,
repo at /testbed, ``testbed`` conda env), but instead of one bespoke agent
session it drives ``run_eval_task(task, workflow=generate_review_fix)`` —
implement -> structured review verdict -> conditional fix — so the prediction
exercises the mini workflow engine end-to-end. This is the A/B candidate
against the 61.7% team baseline (`opencollab-team.oc-team.json`).

Baseline-input parity: the default task prompt forwards ``hints_text`` and
warns that FAIL_TO_PASS tests may not exist yet (the with-hints team baseline
saw both). The validation-council workflow defaults to blind validation and
therefore withholds official test patches and FAIL_TO_PASS ids. Patch extraction
is baseline-identical: ``git add -A && git diff --cached`` inside the container
(includes new files), NOT the ``git diff`` that ``run_eval_task`` captures
internally.

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
import os
import shlex
import sys
import uuid
from dataclasses import fields
from pathlib import Path

# Make the opencollab package importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "opencollab"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import gen_prediction as gp  # noqa: E402 — shared container plumbing

from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.bootstrap.workflow_runtime import discover_workflows  # noqa: E402
from opencollab.harness.evaluator import EvalTask, run_eval_task  # noqa: E402
from opencollab.harness.workflows import generate_review_fix  # noqa: E402

# Team-baseline parity: use the current default per-instance cap for comparable
# OpenCollab SWE-bench runs.
DEFAULT_BUDGET = 1_000_000
DEFAULT_MAX_STEPS = 60  # per workflow session; 60 proved enough to act, 40 did not
DEFAULT_TIMEOUT = 1800.0  # the workflow runs up to 3 sequential sessions
BLIND_BY_DEFAULT_WORKFLOWS = {"validation-council-solve", "swe-committee-v2"}
VALIDATION_ARTIFACT_MARKERS = (
    "opencollab-validation",
    "opencollab_validation",
    "validation_probe",
    "validation-probe",
    "tmp_validation",
    "tmp-validation",
)
TEST_DIR_NAMES = {"test", "tests", "testing"}


def _fail_to_pass_ids(instance: dict) -> list[str]:
    """Parse the FAIL_TO_PASS node-ids (JSON string or list) from an instance."""
    f2p = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(f2p, str):
        f2p = json.loads(f2p)
    return list(f2p)


def build_task(instance: dict, *, include_fail_to_pass: bool = True) -> str:
    """Issue prompt with hints_text forwarded and the FAIL_TO_PASS caveat.

    With ``include_fail_to_pass=True`` this matches the inputs the with-hints
    team baseline saw. With ``False`` it is suitable for blind validation
    workflows that must not see official grading ids.
    """
    problem = instance["problem_statement"]
    hints = (instance.get("hints_text") or "").strip()
    hints_block = (
        f"\n## Hints (from the issue discussion — may help locate the cause)\n{hints}\n"
        if hints
        else ""
    )
    if include_fail_to_pass:
        f2p = _fail_to_pass_ids(instance)
        tests = "\n".join(f"- {t}" for t in f2p)
        tests_block = (
            f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n"
            "Note: some of these tests may not exist yet at this commit — they are "
            "added by the grading harness. Do not be surprised if you cannot run "
            "them; verify the fixed behavior directly instead.\n\n"
        )
    else:
        tests_block = (
            "## Blind validation mode\n"
            "Do not use official hidden tests, injected grader patches, or "
            "FAIL_TO_PASS node ids. Infer validation only from the issue text, "
            "repository code, public tests, and public documentation.\n\n"
        )
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n{hints_block}\n"
        f"{tests_block}"
        "Locate the root cause in the source, apply a minimal fix, and ensure "
        "the behavior described above is satisfied."
    )


def build_extras(instance: dict, *, include_hidden_tests: bool = True) -> dict:
    """Build EvalTask extras, optionally withholding official grading data."""
    if not include_hidden_tests:
        return {"blind_validation": True}
    return {
        "test_patch": instance.get("test_patch") or "",
        "fail_to_pass": _fail_to_pass_ids(instance),
    }


def _blind_validation_default(workflow_name: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return workflow_name in BLIND_BY_DEFAULT_WORKFLOWS


def _workflow_name(workflow_fn, workflow_label: str | None = None) -> str:
    if workflow_label:
        return workflow_label
    spec = getattr(workflow_fn, "__workflow_spec__", None)
    name = getattr(spec, "name", None)
    if name:
        return str(name)
    return getattr(workflow_fn, "__name__", "")


def _resolve_blind_validation(workflow_fn, explicit: bool | None, workflow_label: str | None = None) -> bool:
    return _blind_validation_default(_workflow_name(workflow_fn, workflow_label), explicit)


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path and path != "/dev/null":
            paths.append(path)
    return paths


def _normalize_patch_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/").lstrip("/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    return normalized


def _workflow_allowed_patch_paths(workflow_result: object) -> set[str] | None:
    if not isinstance(workflow_result, dict):
        return None
    paths = workflow_result.get("allowed_patch_paths")
    if isinstance(paths, list):
        return {_normalize_patch_path(str(path)) for path in paths if str(path).strip()}
    attempts = workflow_result.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1]
    if not isinstance(last, dict):
        return None
    verdict = last.get("final_verdict")
    if not isinstance(verdict, dict):
        return None
    paths = verdict.get("allowed_patch_paths")
    if not isinstance(paths, list):
        return None
    return {_normalize_patch_path(str(path)) for path in paths if str(path).strip()}


def _workflow_disallowed_patch_paths(workflow_result: object) -> set[str]:
    if not isinstance(workflow_result, dict):
        return set()
    paths = workflow_result.get("disallowed_patch_paths")
    if isinstance(paths, list):
        return {_normalize_patch_path(str(path)) for path in paths if str(path).strip()}
    attempts = workflow_result.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return set()
    last = attempts[-1]
    if not isinstance(last, dict):
        return set()
    verdict = last.get("final_verdict")
    if not isinstance(verdict, dict):
        return set()
    paths = verdict.get("disallowed_patch_paths")
    if not isinstance(paths, list):
        return set()
    return {_normalize_patch_path(str(path)) for path in paths if str(path).strip()}


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


def _result_metrics(result) -> dict:
    return {
        field.name: _json_safe(getattr(result, field.name))
        for field in fields(result)
        if field.name != "patch"
    }


def _looks_like_validation_artifact(path: str) -> bool:
    normalized = _normalize_patch_path(path)
    lowered = normalized.lower()
    if any(marker in lowered for marker in VALIDATION_ARTIFACT_MARKERS):
        return True
    parts = [part.lower() for part in normalized.split("/") if part]
    if not parts:
        return False
    basename = parts[-1]
    if parts[0] in TEST_DIR_NAMES:
        return True
    if basename.startswith("test_") or basename.endswith("_test.py"):
        return True
    if any(part in TEST_DIR_NAMES for part in parts[:-1]) and (
        basename.startswith("test_") or basename.endswith("_test.py")
    ):
        return True
    return False


def _validation_artifact_paths(patch: str) -> list[str]:
    return [path for path in _patch_paths(patch) if _looks_like_validation_artifact(path)]


def _patch_paths_to_remove(
    patch: str,
    *,
    allowed_paths: set[str] | None = None,
    disallowed_paths: set[str] | None = None,
) -> list[str]:
    disallowed_paths = disallowed_paths or set()
    remove: list[str] = []
    for path in _patch_paths(patch):
        normalized = _normalize_patch_path(path)
        if normalized in disallowed_paths:
            remove.append(path)
            continue
        if _looks_like_validation_artifact(normalized):
            remove.append(path)
            continue
        if allowed_paths is not None and normalized not in allowed_paths:
            remove.append(path)
    return remove


def _remove_patch_paths(cid: str, paths: list[str]) -> None:
    if not paths:
        return
    quoted = " ".join(shlex.quote(path) for path in paths)
    cmd = (
        f"git restore --staged --worktree -- {quoted} 2>/dev/null || true; "
        f"git clean -fdq -- {quoted}"
    )
    res = gp._docker("exec", "-w", gp.DOCKER_WORKDIR, cid, "bash", "-lc", cmd)
    gp._check_docker(res, "remove validation artifacts before patch extraction")


def extract_patch_guarded(
    cid: str,
    *,
    guard_validation_artifacts: bool = False,
    allowed_paths: set[str] | None = None,
    disallowed_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    patch = gp.extract_patch(cid)
    if not guard_validation_artifacts:
        return patch, []
    violations = _patch_paths_to_remove(
        patch,
        allowed_paths=allowed_paths,
        disallowed_paths=disallowed_paths,
    )
    if not violations:
        return patch, []
    _remove_patch_paths(cid, violations)
    patch = gp.extract_patch(cid)
    remaining = _patch_paths_to_remove(
        patch,
        allowed_paths=allowed_paths,
        disallowed_paths=disallowed_paths,
    )
    if remaining:
        raise RuntimeError(
            "disallowed paths remain in patch after cleanup: "
            + ", ".join(sorted(set(remaining)))
        )
    return patch, violations


async def generate(
    instance: dict,
    image: str,
    cfg: dict,
    args: argparse.Namespace,
    workflow_fn,
    workflow_label: str | None = None,
) -> tuple[str, dict]:
    """Run the chosen workflow in a fresh container; return (patch, metrics)."""
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

        blind_validation = _resolve_blind_validation(
            workflow_fn, getattr(args, "blind_validation", None), workflow_label
        )
        include_hidden_tests = not blind_validation
        task = EvalTask(
            task_id=iid,
            description=build_task(instance, include_fail_to_pass=include_hidden_tests),
            timeout=args.timeout,
            max_tokens=args.budget,
            extras=build_extras(instance, include_hidden_tests=include_hidden_tests),
        )
        result = await run_eval_task(
            task,
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            output_dir=os.environ.get(
                "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR",
                str(_REPO_ROOT / "logs" / "eval_workflow"),
            ),
            prompt=gp.AGENT_PROMPT,
            env_factory=env_factory,
            max_steps=args.max_steps,
            workflow=workflow_fn,
            temperature=cfg["temperature"],
            top_p=cfg.get("top_p"),
            thinking=cfg.get("thinking", False),
            thinking_params=cfg.get("thinking_params") or None,
        )
        print(
            f"  workflow: tokens={result.tokens_used} steps={result.steps} "
            f"duration={result.duration:.0f}s error={result.error}"
        )
        workflow_result = getattr(result, "workflow_result", None)
        guard_patch_paths = _workflow_name(
            workflow_fn, workflow_label
        ) in BLIND_BY_DEFAULT_WORKFLOWS
        allowed_paths = _workflow_allowed_patch_paths(workflow_result)
        workflow_allowlist_missing = guard_patch_paths and allowed_paths is None
        if workflow_allowlist_missing:
            allowed_paths = set()
        patch, removed_validation_artifacts = extract_patch_guarded(
            cid,
            guard_validation_artifacts=guard_patch_paths,
            allowed_paths=allowed_paths,
            disallowed_paths=_workflow_disallowed_patch_paths(workflow_result),
        )
    finally:
        if not args.keep_container:
            gp.remove_container(cid)
        else:
            print(f"  (left container {cid} running: {name})")

    metrics = _result_metrics(result)
    metrics["patch_produced"] = bool(patch.strip())
    metrics["submitted_patch_chars"] = len(patch)
    if workflow_allowlist_missing:
        metrics["workflow_allowlist_missing"] = True
    if removed_validation_artifacts:
        metrics["validation_artifacts_removed"] = removed_validation_artifacts
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
    ap.add_argument("--workflow", default=None,
                    help="CLI workflow name from workflows/ (e.g. analyst-solve); "
                         "default: the built-in generate_review_fix")
    blind_group = ap.add_mutually_exclusive_group()
    blind_group.add_argument("--blind-validation", dest="blind_validation",
                             action="store_true",
                             help="Do not inject official test_patch or FAIL_TO_PASS ids")
    blind_group.add_argument("--with-hidden-tests", dest="blind_validation",
                             action="store_false",
                             help="Inject official test_patch and FAIL_TO_PASS ids")
    ap.set_defaults(blind_validation=None)
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

    # Resolve the workflow: a named CLI workflow from workflows/, or the built-in.
    if args.workflow:
        registry = discover_workflows(str(_REPO_ROOT / "workflows"))
        try:
            spec = registry.get(args.workflow)
        except KeyError:
            names = ", ".join(s.name for s in registry.list_specs()) or "(none)"
            ap.error(f"unknown --workflow {args.workflow!r}; available: {names}")
        workflow_fn, wf_label = spec.fn, spec.name
    else:
        workflow_fn, wf_label = generate_review_fix, "generate_review_fix"
    args.blind_validation = _resolve_blind_validation(workflow_fn, args.blind_validation, wf_label)

    cfg = get_config(str(_REPO_ROOT))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    model_name = args.model_name or f"opencollab-{wf_label}-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")
    print(f"Thinking: {cfg.get('thinking', False)}")
    print(f"Workflow: {wf_label} (budget={args.budget}, "
          f"max_steps/session={args.max_steps})")
    print(f"Blind validation: {args.blind_validation}")

    patch, metrics = asyncio.run(generate(instance, image, cfg, args, workflow_fn, wf_label))

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

"""Generate a SWE-bench prediction via the harness workflow mode (A/B driver).

Same container plumbing as ``gen_prediction.py`` (official ``sweb.eval`` image,
repo at /testbed, ``testbed`` conda env), but instead of one bespoke agent
session it drives ``run_eval_task(task, workflow=generate_review_fix)`` —
implement -> structured review verdict -> conditional fix — so the prediction
exercises the mini workflow engine end-to-end. This is the A/B candidate
against the 61.7% team baseline (`opencollab-team.oc-team.json`).

The current generator always uses blind validation and withholds official test
patches and FAIL_TO_PASS ids. Before Solver execution it captures an anonymous
Git baseline. After execution it copies a bounded workspace archive and uses a
clean host Git directory to extract the candidate against that fixed baseline.

Generate (OpenCollab venv, absolute paths in background shells)::

    opencollab/.venv/bin/python swebench/gen_prediction_workflow.py \
        --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
        --output /path/to/swebench-eval/predictions-review-fix.jsonl

Grade with the official harness (separate venv)::

    cd /path/to/swebench-eval && HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-review-fix.jsonl -i sympy__sympy-20590 \
        -id review-fix-1 --cache_level env --report_dir reports
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
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
from gen_prediction_workflow_inputs import (  # noqa: E402
    BLIND_BY_DEFAULT_WORKFLOWS as BLIND_BY_DEFAULT_WORKFLOWS,
)
from gen_prediction_workflow_inputs import (  # noqa: E402
    _blind_validation_default as _blind_validation_default,
)
from gen_prediction_workflow_inputs import (  # noqa: E402
    _fail_to_pass_ids as _fail_to_pass_ids,
)
from gen_prediction_workflow_inputs import (  # noqa: E402
    _resolve_blind_validation as _resolve_blind_validation,
)
from gen_prediction_workflow_inputs import (  # noqa: E402
    _workflow_name as _workflow_name,
)
from gen_prediction_workflow_inputs import build_extras as build_extras  # noqa: E402
from gen_prediction_workflow_inputs import build_task as build_task  # noqa: E402
from gen_prediction_workflow_inputs import json as json  # noqa: E402
from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.adapters.llm.types import (  # noqa: E402
    DEFAULT_MAX_OUTPUT_TOKENS,
    model_context_window,
)
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.bootstrap.workflow_runtime import discover_workflows  # noqa: E402
from opencollab.harness.evaluator import EvalTask, run_eval_task  # noqa: E402
from opencollab.harness.swe_generation_proof import (  # noqa: E402
    current_generation_proof_valid,
)
from opencollab.harness.test_injection import _decode_git_c_path  # noqa: E402

from swebench.container_quiescence import require_container_quiescence  # noqa: E402


def validate_workflow_limits(
    *,
    max_steps: object,
    budget: object,
    timeout: object,
    checkpoint_interval: object,
) -> tuple[int, int, float, float]:
    normalized_steps, normalized_budget, normalized_timeout = (
        gp.validate_generation_limits(
            max_steps=max_steps,
            budget=budget,
            timeout=timeout,
        )
    )
    if isinstance(checkpoint_interval, bool):
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        )
    try:
        normalized_checkpoint = float(checkpoint_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        ) from exc
    if not math.isfinite(normalized_checkpoint) or normalized_checkpoint < 0:
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        )
    return (
        normalized_steps,
        normalized_budget,
        normalized_timeout,
        normalized_checkpoint,
    )
from opencollab.harness.workflows import generate_review_fix  # noqa: E402

# Team-baseline parity: use the current default per-instance cap for comparable
# OpenCollab SWE-bench runs.
DEFAULT_BUDGET = 1_000_000
DEFAULT_MAX_STEPS = 60  # per workflow session; 60 proved enough to act, 40 did not
DEFAULT_TIMEOUT = 1800.0  # the workflow runs up to 3 sequential sessions
DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 0.0
VALIDATION_ARTIFACT_MARKERS = (
    "opencollab-validation",
    "opencollab_validation",
    "validation_probe",
    "validation-probe",
    "tmp_validation",
    "tmp-validation",
)
TEST_DIR_NAMES = {"test", "tests", "testing"}


def _patch_entries(patch: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        tokens = _git_diff_header_tokens(line)
        if len(tokens) < 2:
            continue
        old_path = _git_diff_endpoint(tokens[0], "a")
        new_path = _git_diff_endpoint(tokens[1], "b")
        if old_path or new_path:
            entries.append((old_path, new_path))
    return entries


def _patch_paths(patch: str) -> list[str]:
    paths: dict[str, None] = {}
    for old_path, new_path in _patch_entries(patch):
        for path in (old_path, new_path):
            if path:
                paths.setdefault(path, None)
    return list(paths)


def _git_diff_header_tokens(header: str) -> list[str]:
    text = str(header or "").strip()
    prefix = "diff --git "
    if not text.startswith(prefix):
        return []
    text = text[len(prefix) :]
    tokens: list[str] = []
    index = 0
    while index < len(text) and len(tokens) < 2:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        start = index
        if text[index] == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace():
                index += 1
        tokens.append(text[start:index])
    return tokens


def _git_diff_endpoint(token: str, side: str) -> str:
    path = _decode_git_c_path(token)
    if path == "/dev/null":
        return ""
    prefix = f"{side}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path


def _normalize_patch_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


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


def _patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def _result_metrics(result) -> dict:
    return {
        field.name: _json_safe(getattr(result, field.name))
        for field in fields(result)
        if field.name != "patch"
    }


def _workflow_status_for_result(result, patch: str) -> str:
    error = str(getattr(result, "error", None) or "")
    if error:
        if error.startswith("Task timed out after ") and patch.strip():
            return "done_with_timeout_patch"
        return "error"
    if not patch.strip():
        return "empty_patch_after_done"
    workflow_result = getattr(result, "workflow_result", None)
    if isinstance(workflow_result, dict) and workflow_result.get("status"):
        return str(workflow_result["status"])
    return "done" if patch.strip() else ""


def build_output_records(
    *,
    instance_id: str,
    model_name: str,
    patch: str,
    metrics: dict,
    workflow_name: str | None = None,
    record_id: str | None = None,
) -> tuple[dict, dict]:
    record_id = record_id or uuid.uuid4().hex
    patch_sha256 = _patch_sha256(patch)
    metric_record = {
        **metrics,
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha256,
        "model_name_or_path": model_name,
    }
    metric_record["runner_returncode"] = gp.runner_returncode_for_metrics(
        metric_record
    )
    prediction = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha256,
        "model_name_or_path": model_name,
        "model_patch": patch,
        "workflow_metric": metric_record,
    }
    if workflow_name:
        prediction["workflow"] = workflow_name
        metric_record["workflow"] = workflow_name
    return prediction, metric_record


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
    normalized_allowed = (
        {_normalize_patch_path(path) for path in allowed_paths}
        if allowed_paths is not None
        else None
    )
    normalized_disallowed = {
        _normalize_patch_path(path) for path in (disallowed_paths or set())
    }
    remove: dict[str, None] = {}
    for old_path, new_path in _patch_entries(patch):
        endpoints = [path for path in (old_path, new_path) if path]
        violates_guard = any(
            path in normalized_disallowed
            or _looks_like_validation_artifact(path)
            or (
                normalized_allowed is not None and path not in normalized_allowed
            )
            for path in endpoints
        )
        if violates_guard:
            for path in endpoints:
                remove.setdefault(path, None)
    return list(remove)


def extract_patch_guarded(
    cid: str,
    trusted_baseline,
    *,
    guard_validation_artifacts: bool = False,
    allowed_paths: set[str] | None = None,
    disallowed_paths: set[str] | None = None,
) -> tuple[str, list[str], dict]:
    patch, extraction = gp.extract_patch_trusted(cid, trusted_baseline)
    if not guard_validation_artifacts:
        return patch, [], extraction.as_dict()
    violations = _patch_paths_to_remove(
        patch,
        allowed_paths=allowed_paths,
        disallowed_paths=disallowed_paths,
    )
    if not violations:
        return patch, [], extraction.as_dict()
    raise RuntimeError(
        "trusted host patch contains disallowed paths: "
        + ", ".join(sorted(set(violations)))
    )


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
    name = gp.unique_container_name("oc-wf-", iid)
    run_dir = Path(args.output).parent
    cid = gp.start_container_with_marker(image, name, run_dir)
    print(f"Container: {cid}")
    patch = ""
    metrics: dict = {}
    output_path: Path | None = None
    metrics_path: Path | None = None
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    trusted_baseline = None
    try:
        snapshot = gp.prepare_solver_git_snapshot(
            cid,
            str(instance.get("base_commit") or ""),
        )
        trusted_baseline = gp.prepare_trusted_patch_baseline(cid, snapshot)
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
        if not blind_validation:
            raise RuntimeError(
                "trusted host extraction requires blind validation without injected tests"
            )
        if bool(args.resume) or args.checkpoint_interval_seconds > 0:
            raise RuntimeError(
                "trusted host extraction does not accept container Git checkpoints"
            )
        include_hidden_tests = not blind_validation
        task = EvalTask(
            task_id=gp.anonymous_solver_task_id(),
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
            max_output_tokens=cfg.get(
                "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
            ),
            thinking=cfg.get("thinking", False),
            thinking_params=cfg.get("thinking_params") or None,
            checkpoint_interval_seconds=None,
            resume_from_checkpoint=False,
            defer_patch_extraction=True,
        )
        require_container_quiescence(cid)
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
        outer_extraction_allowed = bool(
            result.execution_quiesced
            and result.injected_path_cleanup_proven
            and result.harness_artifact_exclusion_proven
            and result.checkpoint_restore_integrity_proven
            and result.task_stage_integrity_proven
            and not result.test_patch_isolation_failed
            and not result.error
        )
        if outer_extraction_allowed:
            patch, removed_validation_artifacts, extraction_proof = extract_patch_guarded(
                cid,
                trusted_baseline,
                guard_validation_artifacts=guard_patch_paths,
                allowed_paths=allowed_paths,
                disallowed_paths=_workflow_disallowed_patch_paths(workflow_result),
            )
        else:
            patch = ""
            removed_validation_artifacts = []
            extraction_proof = None
        metrics = _result_metrics(result)
        metrics["container_execution_quiesced"] = True
        metrics["execution_quiesced"] = metrics.get("execution_quiesced") is True
        metrics["submission_eligible"] = (
            metrics.get("submission_eligible") is True
            and metrics["execution_quiesced"] is True
        )
        metrics.update(
            {
                "llm_model": cfg["model"],
                "llm_provider": cfg["provider"],
                "context_window": model_context_window(cfg["model"]),
                "temperature": cfg["temperature"],
                "top_p": cfg.get("top_p"),
                "max_output_tokens": cfg.get(
                    "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
                "budget": args.budget,
                "max_steps": args.max_steps,
                "llm_base_url_sha256": hashlib.sha256(
                    str(cfg["base_url"]).encode("utf-8")
                ).hexdigest(),
                "workflow_env": {
                    key: os.environ[key]
                    for key in (
                        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
                        "OPENCOLLAB_TEMPERATURE",
                        "OPENCOLLAB_THINKING",
                        "OPENCOLLAB_THINKING_PARAMS",
                        "OPENCOLLAB_TOP_P",
                    )
                    if key in os.environ
                },
            }
        )
        metrics["solver_git_snapshot"] = snapshot.as_dict()
        if extraction_proof is not None:
            metrics["trusted_patch_extraction"] = extraction_proof
        extraction_valid = current_generation_proof_valid(metrics, patch)
        metrics["patch_extraction_succeeded"] = extraction_valid
        metrics["task_stage_integrity_proven"] = extraction_valid
        metrics["worktree_integrity_proven"] = extraction_valid
        metrics["submission_eligible"] = (
            outer_extraction_allowed and extraction_valid and bool(patch.strip())
        )
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        if not metrics.get("workflow_status"):
            metrics["workflow_status"] = _workflow_status_for_result(result, patch)
        gp.normalize_trusted_extraction_status(metrics, patch)
        if workflow_allowlist_missing:
            metrics["workflow_allowlist_missing"] = True
        if removed_validation_artifacts:
            metrics["validation_artifacts_removed"] = removed_validation_artifacts
        if getattr(args, "_persist_output_after_cleanup", False):
            output_path = Path(args.output)
            metrics_path_arg = getattr(args, "metrics", None)
            metrics_path = (
                Path(metrics_path_arg)
                if metrics_path_arg
                else gp.default_metrics_path(output_path)
            )
            persisted_model_name = getattr(args, "model_name", None) or (
                f"opencollab-{_workflow_name(workflow_fn, workflow_label)}-{cfg['model']}"
            )
            record, metric_record = build_output_records(
                instance_id=iid,
                model_name=persisted_model_name,
                patch=patch,
                metrics=metrics,
                workflow_name=_workflow_name(workflow_fn, workflow_label),
            )
            pending_required = bool(patch.strip())
            if pending_required:
                pending_path = gp.persist_pending_output(
                    run_dir=run_dir,
                    predictions_path=output_path,
                    metrics_path=metrics_path,
                    prediction=record,
                    metric=metric_record,
                    cid=cid,
                    name=name,
                )
    except BaseException as exc:
        generation_error = exc
        raise
    finally:
        if trusted_baseline is not None:
            trusted_baseline.cleanup()
        preserve_container = (
            pending_required
            and pending_path is None
            and gp.output_staging_requires_container_preservation(
                run_dir,
                cid=cid,
                name=name,
            )
        )
        if preserve_container:
            metrics["container_preservation_required"] = True
        else:
            completed = generation_error is None and gp.metrics_have_completed_identity(
                metrics,
                patch,
            )
            try:
                gp.finalize_container_ownership(
                    run_dir=run_dir,
                    cid=cid,
                    name=name,
                    keep_container=args.keep_container if generation_error is None else False,
                    completed=completed,
                    metrics=metrics,
                )
            except BaseException as cleanup_error:
                if generation_error is None:
                    raise
                add_note = getattr(generation_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "container cleanup failed after generation error: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )

    if getattr(args, "_persist_output_after_cleanup", False):
        if (
            output_path is None
            or metrics_path is None
            or record is None
            or metric_record is None
        ):
            raise RuntimeError("workflow output record was not built")
        if pending_path is not None:
            publish_status = gp.publish_pending_output(run_dir, pending_path)
            gp.require_published_output(
                publish_status,
                label="pending workflow output",
            )
        else:
            gp.append_output_records(output_path, metrics_path, record, metric_record)
    return patch, metrics


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one SWE-bench prediction with the review-fix workflow"
    )
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument("--metrics", default=None,
                    help="Metrics JSONL to append to (default: metrics.jsonl beside --output)")
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--top-p", type=float)
    ap.add_argument("--max-output-tokens", type=int)
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
    ap.set_defaults(blind_validation=True)
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                    help="Step cap per workflow session")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="Shared token budget across all workflow sessions")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument(
        "--checkpoint-interval-seconds",
        type=float,
        default=DEFAULT_CHECKPOINT_INTERVAL_SECONDS,
        help="Must remain 0 for trusted host extraction; positive values are rejected",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Unsupported in trusted host extraction mode; passing it is rejected",
    )
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()
    try:
        (
            args.max_steps,
            args.budget,
            args.timeout,
            args.checkpoint_interval_seconds,
        ) = validate_workflow_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
            checkpoint_interval=args.checkpoint_interval_seconds,
        )
    except (TypeError, ValueError) as exc:
        ap.error(str(exc))
    out_path, _metrics_path = gp.output_paths(args.output, args.metrics)

    instance = gp.load_instance(args.instance_file)
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
    if args.temperature is not None:
        if not 0.0 <= args.temperature <= 2.0:
            ap.error("--temperature must be between 0 and 2")
        cfg["temperature"] = args.temperature
    if args.top_p is not None:
        if not 0.0 <= args.top_p <= 1.0:
            ap.error("--top-p must be between 0 and 1")
        cfg["top_p"] = args.top_p
    if args.max_output_tokens is not None:
        if args.max_output_tokens <= 0:
            ap.error("--max-output-tokens must be positive")
        cfg["max_output_tokens"] = args.max_output_tokens
    model_name = args.model_name or f"opencollab-{wf_label}-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")
    print(f"Thinking: {cfg.get('thinking', False)}")
    print(f"Workflow: {wf_label} (budget={args.budget}, "
          f"max_steps/session={args.max_steps})")
    print(f"Blind validation: {args.blind_validation}")
    print(
        "Checkpoint: "
        f"{args.checkpoint_interval_seconds:g}s"
        f"{' resume' if args.resume else ''}"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    args.model_name = model_name
    args._persist_output_after_cleanup = True
    patch, metrics = gp.run_with_bounded_shutdown(
        generate(instance, image, cfg, args, workflow_fn, wf_label)
    )

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (workflow made no tracked changes)")

    if not gp.metrics_have_completed_identity(metrics, patch):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

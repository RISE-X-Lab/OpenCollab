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

    cd /path/to/swebench-eval
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-opencollab.jsonl -i sympy__sympy-20590 \
        -id oc-kimi --cache_level env --report_dir reports

Run with the OpenCollab venv (it must import ``opencollab``)::

    opencollab/.venv/bin/python swebench/gen_prediction.py \
        --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
        --output /path/to/swebench-eval/predictions-opencollab.jsonl
"""

# ruff: noqa: E402, F401

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import math
import operator
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import types
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

# Make the opencollab package importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "opencollab"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import gen_prediction_agent
import gen_prediction_config
import gen_prediction_constants
import gen_prediction_docker
import gen_prediction_pending
import gen_prediction_safe_output
from gen_prediction_agent import (
    _quiesce_agent_tasks,
    bounded_container_output_command,
    build_task,
    extract_patch,
    load_instance,
    run_agent,
)
from gen_prediction_config import (
    _docker_timeout_from_env,
    _stable_docker_component,
    default_container_image,
    unique_container_name,
    validate_generation_limits,
    validate_instance_id,
)
from gen_prediction_constants import (
    _ACTIVATE,
    _BOUNDED_CAPTURE_SCRIPT,
    _MISSING_CONTAINER_RE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    CONTAINER_BOUNDED_CAPTURE_HELPER,
    CONTAINER_OWNER_LABEL,
    CONTAINER_OWNER_SCHEMA_VERSION,
    DOCKER_WORKDIR,
    HARNESS_LOCK_TIMEOUT_SECONDS,
    MAX_CAPTURED_STDERR_BYTES,
    MAX_COMPATIBILITY_MARKER_BYTES,
    MAX_EXTRACTED_PATCH_BYTES,
    MAX_INSTANCE_BYTES,
    MAX_INSTANCE_ID_BYTES,
    MAX_JSONL_SCAN_LINE_BYTES,
    MAX_OUTPUT_JSONL_BYTES,
    MAX_OWNER_RECORD_BYTES,
    MAX_PENDING_OUTPUT_BYTES,
    MAX_STATUS_DIAGNOSTIC_BYTES,
    PENDING_OUTPUT_SCHEMA_VERSION,
    SAFE_FILE_OPEN_RETRIES,
)
from gen_prediction_docker import (
    _check_docker,
    _clear_compatibility_markers,
    _container_is_absent,
    _container_owner_label_state,
    _create_pending_owner,
    _docker,
    _encode_owner,
    _owner_directory,
    _owner_is_live,
    _owner_record,
    _path_matches_open_file,
    _process_start_identity,
    _read_owner,
    _read_small_regular_text,
    _remove_labeled_container,
    _remove_owned_container,
    _replace_owner,
    _require_creation_cleanup,
    _unlink_owner,
    _write_compatibility_markers,
    clear_container_marker,
    container_owner_path,
    finalize_container_ownership,
    mark_container_kept,
    recover_stale_container_owners,
    remove_container,
    remove_container_and_clear_marker,
    start_container,
    start_container_with_marker,
    write_container_marker,
)
from gen_prediction_pending import (
    _append_jsonl_durable_once,
    _candidate_matches_owner,
    _find_committed_identity,
    _open_pending_regular,
    _pending_output_directory,
    _pending_owner_state,
    _preservation_was_superseded,
    _promote_durable_preservation_candidates,
    _read_pending_fd,
    _row_output_identity,
    _unlink_pending_locked,
    _validate_pending_candidate,
    output_staging_requires_container_preservation,
    pending_output_path,
    persist_pending_output,
    publish_pending_output,
    recover_generation_state,
)
from gen_prediction_safe_output import (
    _acquire_exclusive_lock,
    _append_jsonl_durable,
    _atomic_create_bytes,
    _atomic_write_bytes,
    _atomic_write_text,
    _cleanup_temporary_file,
    _fsync_directory,
    _open_regular_file,
    _patch_sha256,
    _validate_output_target,
    _write_all,
    append_output_records,
    build_output_records,
    complete_single_agent_integrity,
    default_metrics_path,
    metrics_have_completed_identity,
    output_paths,
    output_paths_collide,
    runner_returncode_for_metrics,
)
from opencollab.adapters.env import DockerEnvironment  # noqa: E402
from opencollab.adapters.tools.bash import BashTool  # noqa: E402
from opencollab.adapters.tools.fs import (  # noqa: E402
    FileReadTool,
    FileWriteTool,
    GrepTool,
)
from opencollab.adapters.trace import Tracer  # noqa: E402
from opencollab.application.async_timeout import (  # noqa: E402
    CallerTimeoutError,
    abandon_on_timeout,
    run_with_bounded_shutdown,
)
from opencollab.bootstrap.config import get_config  # noqa: E402
from opencollab.bootstrap.container import (  # noqa: E402
    agent_save_path,
    build_session,
    make_run_dir,
)
from opencollab.domain.agent import Agent  # noqa: E402
from opencollab.domain.session import SessionPhase  # noqa: E402
from opencollab.harness.swe_eval_records import (  # noqa: E402
    MAX_JSONL_SCAN_BYTES,
    open_regular_binary,
    read_bounded_json,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate one SWE-bench prediction with OpenCollab")
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument(
        "--metrics",
        default=None,
        help="Metrics JSONL to append to (default: metrics.jsonl beside --output)",
    )
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
    try:
        args.max_steps, args.budget, args.timeout = validate_generation_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
        )
    except ValueError as exc:
        ap.error(str(exc))
    out_path, metrics_path = output_paths(args.output, args.metrics)

    instance = load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or default_container_image(args.arch, iid)

    cfg = get_config(str(_REPO_ROOT))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    model_name = args.model_name or f"opencollab-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")

    name = unique_container_name("oc-gen-", iid)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out_path.parent
    cid = start_container_with_marker(image, name, run_dir)
    print(f"Container: {cid}")
    patch = ""
    metrics: dict = {}
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    try:
        task = build_task(instance)
        metrics = run_with_bounded_shutdown(run_agent(task, cid, cfg, args.max_steps, args.budget, args.timeout))
        if metrics.get("submission_eligible") is True:
            patch = extract_patch(cid)
            patch_extraction_succeeded = True
        else:
            patch = ""
            patch_extraction_succeeded = False
        complete_single_agent_integrity(
            metrics,
            patch_extraction_succeeded=patch_extraction_succeeded,
        )
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        record, metric_record = build_output_records(
            instance_id=iid,
            model_name=model_name,
            patch=patch,
            metrics=metrics,
        )
        pending_required = bool(patch.strip())
        if pending_required:
            pending_path = persist_pending_output(
                run_dir=run_dir,
                predictions_path=out_path,
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
        preserve_container = (
            pending_required
            and pending_path is None
            and output_staging_requires_container_preservation(
                run_dir,
                cid=cid,
                name=name,
            )
        )
        if preserve_container:
            metrics["container_preservation_required"] = True
        else:
            completed = generation_error is None and metrics_have_completed_identity(
                metrics,
                patch,
            )
            try:
                finalize_container_ownership(
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

    if record is None or metric_record is None:
        raise RuntimeError("generation output record was not built")
    if pending_path is not None:
        publish_status = publish_pending_output(run_dir, pending_path)
        if publish_status == "deferred":
            raise RuntimeError("pending output remained blocked by container ownership")
    else:
        append_output_records(out_path, metrics_path, record, metric_record)

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (agent made no tracked changes)")

    if not metrics_have_completed_identity(metric_record, patch):
        raise SystemExit(1)


_COMPATIBILITY_MODULES = (
    gen_prediction_constants,
    gen_prediction_config,
    gen_prediction_safe_output,
    gen_prediction_docker,
    gen_prediction_agent,
    gen_prediction_pending,
)


class _GenPredictionFacade(types.ModuleType):
    """Mirror compatibility patches into focused prediction helpers."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _GenPredictionFacade


if __name__ == "__main__":
    main()

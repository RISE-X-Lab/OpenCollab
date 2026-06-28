#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_instance(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _prediction_has_patch(output: Path, instance_id: str) -> bool:
    if not output.exists():
        return False
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("instance_id") == instance_id and record.get("model_patch"):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small OpenCollab SWE-bench smoke batch")
    parser.add_argument("--instances-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--budget", type=int, default=40_000)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--model-name", default="opencollab-glm52-single-smoke5")
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    instances_dir = Path(args.instances_dir)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "predictions.jsonl"
    manifest_path = output_dir / "manifest.jsonl"
    instance_paths = sorted(instances_dir.glob("*.json"))[: args.limit]
    if not instance_paths:
        raise SystemExit(f"No instance JSON files found in {instances_dir}")

    env = os.environ.copy()
    default_cache_root = output_dir / ".cache"
    default_cache_paths = {
        "TMPDIR": default_cache_root / "tmp",
        "HF_HOME": default_cache_root / "hf",
        "HF_DATASETS_CACHE": default_cache_root / "datasets",
    }
    for key, path in default_cache_paths.items():
        if not env.get(key):
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
    env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    env.setdefault("OPENCOLLAB_DOCKER_TIMEOUT", "900")
    env.setdefault("OPENCOLLAB_TEMPERATURE", "0")
    env.setdefault("OPENCOLLAB_THINKING", "false")
    env.setdefault("OPENCOLLAB_LLM_TIMEOUT", "240")

    for path in instance_paths:
        instance = _read_instance(path)
        instance_id = instance["instance_id"]
        spec = make_test_spec(instance, namespace="swebench")
        image = spec.instance_image_key
        print(f"\n=== {instance_id} ===", flush=True)
        print(f"image: {image}", flush=True)

        if _prediction_has_patch(output_path, instance_id):
            print("prediction with patch already exists, skipping", flush=True)
            continue

        record = {
            "instance_id": instance_id,
            "instance_file": str(path),
            "image": image,
            "model_name": args.model_name,
        }
        with manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        cmd = [
            sys.executable,
            str(REPO_ROOT / "swebench" / "gen_prediction.py"),
            "--instance-file",
            str(path),
            "--output",
            str(output_path),
            "--image",
            image,
            "--model-name",
            args.model_name,
            "--budget",
            str(args.budget),
            "--max-steps",
            str(args.max_steps),
            "--timeout",
            str(args.timeout),
        ]
        completed = subprocess.run(cmd, cwd=REPO_ROOT, env=env)
        if completed.returncode != 0:
            print(f"instance failed with exit code {completed.returncode}: {instance_id}", flush=True)

    print(f"\nBatch output: {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

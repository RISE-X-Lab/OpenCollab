#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def report_path(work_dir: Path, run_id: str, model_name: str, instance_id: str) -> Path:
    return (
        work_dir
        / "logs"
        / "run_evaluation"
        / run_id
        / model_name.replace("/", "__")
        / instance_id
        / "report.json"
    )


def report_is_done(path: Path, instance_id: str) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False
    return instance_id in data and isinstance(data[instance_id].get("resolved"), bool)


def load_eval_queue(dataset_path: Path, predictions_path: Path, run_id: str, work_dir: Path) -> list[tuple[str, str]]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    predictions = {row["instance_id"]: row for row in read_jsonl(predictions_path)}
    queue: list[tuple[str, str]] = []
    for instance in dataset:
        iid = instance["instance_id"]
        prediction = predictions.get(iid)
        if not prediction:
            continue
        patch = prediction.get("model_patch") or ""
        if not patch.strip():
            continue
        model_name = prediction.get("model_name_or_path") or "unknown-model"
        if report_is_done(report_path(work_dir, run_id, model_name, iid), iid):
            continue
        queue.append((iid, model_name))
    return queue


def run_one(
    *,
    iid: str,
    model_name: str,
    ordinal: int,
    total: int,
    dataset_path: Path,
    predictions_path: Path,
    work_dir: Path,
    run_id: str,
    timeout: int,
    namespace: str,
    cache_level: str,
    clean: str,
    outer_timeout: int,
    env: dict[str, str],
    print_lock: threading.Lock,
) -> tuple[str, int]:
    if report_is_done(report_path(work_dir, run_id, model_name, iid), iid):
        with print_lock:
            print(f"[{ordinal}/{total}] skipping {iid} (report exists)", flush=True)
        return iid, 0

    log_path = work_dir / "command_logs" / f"{iid}.log"
    report_dir = work_dir / "reports" / iid
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_swebench_eval_with_docker_timeout.py"),
        "-d",
        str(dataset_path),
        "-s",
        "test",
        "-i",
        iid,
        "-p",
        str(predictions_path),
        "--max_workers",
        "1",
        "-t",
        str(timeout),
        "--cache_level",
        cache_level,
        "--clean",
        clean,
        "-id",
        run_id,
        "-n",
        namespace,
        "--report_dir",
        str(report_dir),
    ]
    with print_lock:
        print(f"[{ordinal}/{total}] evaluating {iid}", flush=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\n\n$ " + " ".join(cmd) + "\n")
        log_file.write(f"# outer_timeout={outer_timeout}s\n")
        process = subprocess.Popen(
            cmd,
            cwd=work_dir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=outer_timeout)
        except subprocess.TimeoutExpired:
            log_file.write(f"\nouter timeout after {outer_timeout}s; terminating process group\n")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                log_file.write("process did not terminate after SIGTERM; sending SIGKILL\n")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            returncode = 124
    if returncode == 0:
        with print_lock:
            print(f"done {iid}", flush=True)
    else:
        with print_lock:
            print(f"failed {iid} exit={returncode}; see {log_path}", flush=True)
    return iid, returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SWE-bench official evaluation one instance at a time")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--namespace", default="swebench")
    parser.add_argument("--cache-level", default="instance")
    parser.add_argument("--clean", default="False")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--outer-timeout",
        type=int,
        default=0,
        help="Wall-clock timeout per subprocess in seconds. Defaults to --timeout + 900.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    predictions_path = Path(args.predictions).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "command_logs").mkdir(parents=True, exist_ok=True)

    queue = load_eval_queue(dataset_path, predictions_path, args.run_id, work_dir)
    if args.limit > 0:
        queue = queue[: args.limit]
    print(f"pending_non_empty_instances={len(queue)}")

    env = os.environ.copy()
    env.setdefault("OPENCOLLAB_DOCKER_API_TIMEOUT", "900")
    env.setdefault("DOCKER_CLIENT_TIMEOUT", "900")
    env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

    workers = max(1, args.workers)
    outer_timeout = args.outer_timeout if args.outer_timeout > 0 else args.timeout + 900
    print_lock = threading.Lock()
    failures: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_one,
                iid=iid,
                model_name=model_name,
                ordinal=index,
                total=len(queue),
                dataset_path=dataset_path,
                predictions_path=predictions_path,
                work_dir=work_dir,
                run_id=args.run_id,
                timeout=args.timeout,
                namespace=args.namespace,
                cache_level=args.cache_level,
                clean=args.clean,
                outer_timeout=outer_timeout,
                env=env,
                print_lock=print_lock,
            )
            for index, (iid, model_name) in enumerate(queue, 1)
        ]
        for future in as_completed(futures):
            iid, returncode = future.result()
            if returncode != 0:
                failures.append((iid, returncode))

    if failures:
        print("failures=" + ", ".join(f"{iid}:{code}" for iid, code in failures), flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRAJECTORIES_DIR = REPO_ROOT / ".opencollab" / "logs" / "trajectories"
DEFAULT_GLM52_INPUT_USD_PER_MTOK = 1.4
DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK = 0.26
DEFAULT_GLM52_OUTPUT_USD_PER_MTOK = 4.4
MAX_TRAJECTORY_RECORD_BYTES = 4 * 1024 * 1024
MAX_TRAJECTORY_FILE_BYTES = 256 * 1024 * 1024
MAX_TRAJECTORY_TOTAL_BYTES = 512 * 1024 * 1024
MAX_TRAJECTORY_RECORDS_PER_FILE = 1_000_000
MAX_TRAJECTORY_TOTAL_RECORDS = 2_000_000
MAX_TRAJECTORY_FILES = 4_096
MAX_TRAJECTORY_DIRECTORY_ENTRIES = 20_000
MAX_TRAJECTORY_DIRECTORY_DEPTH = 64

PKG_ROOT = REPO_ROOT / "opencollab"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from opencollab.adapters.safe_files import (  # noqa: E402
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
)


@contextmanager
def _open_regular_binary(path: Path) -> Iterator[BinaryIO]:
    """Open a stable regular trajectory without following its final symlink."""
    path = Path(os.path.abspath(os.fspath(path)))
    parent_fd = _open_directory_no_symlinks(path.parent)
    fd = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"trajectory is not a regular file: {path}")
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"trajectory is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"trajectory changed while opening: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            yield handle
            after = os.fstat(handle.fileno())
            current = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if opened_identity != after_identity or after_identity != current_identity:
                raise OSError(f"trajectory changed while reading: {path}")
            if not _directory_path_matches_fd(path.parent, parent_fd):
                raise OSError(f"trajectory parent changed while reading: {path}")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _float_env(name: str, default: float | None = None) -> float | None:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _nonnegative_float(value: object) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _nonnegative_float_arg(value: str) -> float:
    parsed = _nonnegative_float(value)
    try:
        raw = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("expected a finite non-negative number") from exc
    if not math.isfinite(raw) or raw < 0:
        raise argparse.ArgumentTypeError("expected a finite non-negative number")
    return parsed


def _iter_records(path: Path, *, expected_size: int | None = None):
    with _open_regular_binary(path) as handle:
        opened_size = os.fstat(handle.fileno()).st_size
        if expected_size is not None and opened_size != expected_size:
            raise OSError(f"trajectory changed between accounting and read: {path}")
        if opened_size > MAX_TRAJECTORY_FILE_BYTES:
            raise ValueError(
                "trajectory exceeds "
                f"{MAX_TRAJECTORY_FILE_BYTES}-byte limit: {path}"
            )
        bytes_read = 0
        records_read = 0
        while True:
            line = handle.readline(MAX_TRAJECTORY_RECORD_BYTES + 1)
            if not line:
                break
            bytes_read += len(line)
            if bytes_read > MAX_TRAJECTORY_FILE_BYTES:
                raise ValueError(
                    "trajectory exceeds "
                    f"{MAX_TRAJECTORY_FILE_BYTES}-byte limit: {path}"
                )
            if len(line) > MAX_TRAJECTORY_RECORD_BYTES:
                raise ValueError(
                    "trajectory record exceeds "
                    f"{MAX_TRAJECTORY_RECORD_BYTES}-byte limit: {path}"
                )
            if not line.strip():
                continue
            records_read += 1
            if records_read > MAX_TRAJECTORY_RECORDS_PER_FILE:
                raise ValueError(
                    "trajectory records exceed limit of "
                    f"{MAX_TRAJECTORY_RECORDS_PER_FILE}: {path}"
                )
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid trajectory JSON record: {path}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"trajectory JSON record is not an object: {path}")
            yield record


def _trajectory_paths(directory: Path) -> list[Path]:
    absolute = Path(os.path.abspath(os.fspath(directory)))
    try:
        inspected = absolute.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(inspected.st_mode):
        raise ValueError(f"trajectory directory is not a real directory: {absolute}")
    try:
        directory_fd = _open_directory_no_symlinks(absolute)
    except OSError as exc:
        raise ValueError(
            f"trajectory directory is not a real directory: {absolute}"
        ) from exc
    os.close(directory_fd)
    paths: list[Path] = []
    pending: list[tuple[Path, int]] = [(absolute, 0)]
    scanned = 0
    while pending:
        current, depth = pending.pop()
        if depth > MAX_TRAJECTORY_DIRECTORY_DEPTH:
            raise ValueError(
                "trajectory directory depth exceeds limit of "
                f"{MAX_TRAJECTORY_DIRECTORY_DEPTH}: {current}"
            )
        with os.scandir(current) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_TRAJECTORY_DIRECTORY_ENTRIES:
                    raise ValueError(
                        "trajectory directory entries exceed limit of "
                        f"{MAX_TRAJECTORY_DIRECTORY_ENTRIES}"
                    )
                inspected = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(inspected.st_mode):
                    if entry.name.endswith(".jsonl"):
                        # Keep the name visible so collect() records a concrete
                        # unsafe-input error for this candidate.
                        paths.append(Path(entry.path))
                    else:
                        raise ValueError(
                            f"trajectory tree contains a symlink: {entry.path}"
                        )
                elif stat.S_ISDIR(inspected.st_mode):
                    pending.append((Path(entry.path), depth + 1))
                elif stat.S_ISREG(inspected.st_mode) and entry.name.endswith(".jsonl"):
                    paths.append(Path(entry.path))
                if len(paths) > MAX_TRAJECTORY_FILES:
                    raise ValueError(
                        f"trajectory files exceed limit of {MAX_TRAJECTORY_FILES}"
                    )
    return sorted(paths)


def collect(trajectories_dir: Path, model_filter: str | None) -> dict:
    totals = {
        "files": 0,
        "runs": set(),
        "calls": 0,
        "input_tokens": 0,
        "uncached_input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "split_total_tokens": 0,
        "unknown_split_tokens": 0,
        "unknown_cache_input_tokens": 0,
        "unknown_cache_calls": 0,
        "estimated_calls": 0,
        "latency_s": 0.0,
        "complete": True,
        "input_errors": [],
    }
    total_bytes = 0
    total_records = 0
    for path in _trajectory_paths(trajectories_dir):
        try:
            with _open_regular_binary(path) as handle:
                file_bytes = os.fstat(handle.fileno()).st_size
        except (OSError, ValueError) as exc:
            totals["complete"] = False
            totals["input_errors"].append(
                f"{path}: {type(exc).__name__}: {exc}"
            )
            continue
        total_bytes += file_bytes
        if total_bytes > MAX_TRAJECTORY_TOTAL_BYTES:
            totals["complete"] = False
            totals["input_errors"].append(
                "trajectory inputs exceed total byte limit of "
                f"{MAX_TRAJECTORY_TOTAL_BYTES}"
            )
            break
        seen_file = False
        try:
            for record in _iter_records(path, expected_size=file_bytes):
                total_records += 1
                if total_records > MAX_TRAJECTORY_TOTAL_RECORDS:
                    raise ValueError(
                        "trajectory inputs exceed total record limit of "
                        f"{MAX_TRAJECTORY_TOTAL_RECORDS}"
                    )
                if record.get("type") != "llm_call":
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                model = str(payload.get("model") or "")
                if model_filter and model_filter not in model:
                    continue

                usage = payload.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                metrics = record.get("metrics") or {}
                if not isinstance(metrics, dict):
                    metrics = {}
                input_tokens = _nonnegative_int(usage.get("input_tokens"))
                output_tokens = _nonnegative_int(usage.get("output_tokens"))
                raw_total_tokens = usage.get("total_tokens")
                if raw_total_tokens is None:
                    raw_total_tokens = metrics.get("tokens")
                if raw_total_tokens is None:
                    total_tokens = input_tokens + output_tokens
                else:
                    total_tokens = _nonnegative_int(raw_total_tokens)
                cached_input_tokens = _nonnegative_int(usage.get("cache_read_tokens"))
                if "uncached_input_tokens" in usage:
                    uncached_input_tokens = _nonnegative_int(
                        usage.get("uncached_input_tokens")
                    )
                else:
                    uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
                has_cache_accounting = (
                    "cache_read_tokens" in usage
                    or "cache_creation_tokens" in usage
                    or bool(usage.get("raw_usage"))
                    or bool(usage.get("estimated"))
                )

                totals["runs"].add(record.get("run_id") or path.stem)
                totals["calls"] += 1
                totals["input_tokens"] += input_tokens
                totals["uncached_input_tokens"] += uncached_input_tokens
                totals["cached_input_tokens"] += cached_input_tokens
                totals["output_tokens"] += output_tokens
                totals["latency_s"] += _nonnegative_float(metrics.get("latency_s"))
                if usage.get("estimated"):
                    totals["estimated_calls"] += 1
                if input_tokens and not has_cache_accounting:
                    totals["unknown_cache_calls"] += 1
                    totals["unknown_cache_input_tokens"] += input_tokens
                if input_tokens or output_tokens:
                    totals["split_total_tokens"] += total_tokens
                else:
                    totals["unknown_split_tokens"] += total_tokens
                seen_file = True
        except (OSError, ValueError) as exc:
            totals["complete"] = False
            totals["input_errors"].append(
                f"{path}: {type(exc).__name__}: {exc}"
            )
        if seen_file:
            totals["files"] += 1
    totals["runs"] = len(totals["runs"])
    return totals


def estimate_cost(
    totals: dict,
    total_price_per_mtok: float | None,
    input_price_per_mtok: float | None,
    cached_input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
) -> tuple[float | None, str]:
    split_cost = 0.0
    has_split_price = (
        input_price_per_mtok is not None
        or cached_input_price_per_mtok is not None
        or output_price_per_mtok is not None
    )
    if has_split_price:
        if totals["cached_input_tokens"] and cached_input_price_per_mtok is None:
            return None, "missing_cached_price"
        split_cost += totals["uncached_input_tokens"] / 1_000_000 * (input_price_per_mtok or 0.0)
        split_cost += totals["cached_input_tokens"] / 1_000_000 * (cached_input_price_per_mtok or 0.0)
        split_cost += totals["output_tokens"] / 1_000_000 * (output_price_per_mtok or 0.0)

    unknown_tokens = totals["unknown_split_tokens"]
    if unknown_tokens and total_price_per_mtok is not None:
        return split_cost + unknown_tokens / 1_000_000 * total_price_per_mtok, "mixed"
    if unknown_tokens and has_split_price:
        return None, "unknown_split"
    if has_split_price:
        return split_cost, "split"
    if total_price_per_mtok is not None:
        total_tokens = totals["split_total_tokens"] + unknown_tokens
        return total_tokens / 1_000_000 * total_price_per_mtok, "total"
    return None, "no_price"


def print_report(args: argparse.Namespace) -> bool:
    totals = collect(Path(args.trajectories_dir), args.model)
    total_tokens = totals["split_total_tokens"] + totals["unknown_split_tokens"]
    cost, mode = estimate_cost(
        totals,
        args.total_price_per_mtok,
        args.input_price_per_mtok,
        args.cached_input_price_per_mtok,
        args.output_price_per_mtok,
    )

    print(f"model_filter: {args.model or '(all)'}")
    print(f"trajectory_files: {totals['files']}  runs: {totals['runs']}  llm_calls: {totals['calls']}")
    print(
        "tokens: "
        f"input={totals['input_tokens']} "
        f"uncached_input={totals['uncached_input_tokens']} "
        f"cached_input={totals['cached_input_tokens']} "
        f"output={totals['output_tokens']} "
        f"unknown_split={totals['unknown_split_tokens']} "
        f"total={total_tokens}"
    )
    if not totals["complete"]:
        print("input_status: incomplete")
        for error in totals["input_errors"][:20]:
            print(f"input_error: {error}")
    print(
        f"latency_s: {totals['latency_s']:.1f}  "
        f"estimated_calls: {totals['estimated_calls']}  "
        f"legacy_unknown_cache_calls: {totals['unknown_cache_calls']}"
    )
    if cost is None:
        if mode == "missing_cached_price":
            print("cost_usd: unknown because cached input tokens were logged but no cached-input price was provided.")
        elif mode == "unknown_split":
            print("cost_usd: unknown because older logs only contain total tokens; set GLM_TOTAL_USD_PER_MTOK to price those logs.")
        else:
            print("cost_usd: set GLM_TOTAL_USD_PER_MTOK, or GLM input/cached-input/output prices.")
    else:
        exact_from_log = (
            totals["estimated_calls"] == 0
            and totals["unknown_cache_input_tokens"] == 0
            and totals["unknown_split_tokens"] == 0
        )
        label = "cost_usd_from_logged_usage" if exact_from_log else "cost_usd_estimate"
        print(f"{label}: ${cost:.6f} ({mode})")
        if totals["unknown_cache_input_tokens"]:
            print(
                "cost_note: legacy logs without cache fields were priced as uncached input; "
                "provider billing is needed for exact historical cache discounts."
            )
    return bool(totals["complete"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize GLM token usage from OpenCollab trajectory logs")
    parser.add_argument(
        "--trajectories-dir",
        default=os.environ.get("OPENCOLLAB_TRAJECTORIES_DIR", str(DEFAULT_TRAJECTORIES_DIR)),
    )
    parser.add_argument("--model", default="glm-5.2")
    parser.add_argument(
        "--total-price-per-mtok",
        type=_nonnegative_float_arg,
        default=_float_env("GLM_TOTAL_USD_PER_MTOK"),
    )
    parser.add_argument(
        "--input-price-per-mtok",
        type=_nonnegative_float_arg,
        default=_float_env("GLM_INPUT_USD_PER_MTOK", DEFAULT_GLM52_INPUT_USD_PER_MTOK),
    )
    parser.add_argument(
        "--cached-input-price-per-mtok",
        type=_nonnegative_float_arg,
        default=_float_env("GLM_CACHED_INPUT_USD_PER_MTOK", DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK),
    )
    parser.add_argument(
        "--output-price-per-mtok",
        type=_nonnegative_float_arg,
        default=_float_env("GLM_OUTPUT_USD_PER_MTOK", DEFAULT_GLM52_OUTPUT_USD_PER_MTOK),
    )
    parser.add_argument(
        "--watch",
        type=_nonnegative_float_arg,
        default=0.0,
        help="Refresh interval in seconds",
    )
    args = parser.parse_args()

    complete = True
    while True:
        complete = print_report(args)
        if not args.watch:
            break
        print()
        time.sleep(args.watch)
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())

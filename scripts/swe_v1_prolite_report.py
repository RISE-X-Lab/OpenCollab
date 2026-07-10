"""Atomic local report publication for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


def _prepare_local_report_output(path: Path, payload: bytes) -> tuple[Path, os.stat_result | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"report destination must be regular or absent: {path}")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary, before


def _commit_local_report_output(
    path: Path,
    temporary: Path,
    before: os.stat_result | None,
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if before is None:
        if current is not None:
            raise OSError(f"report destination appeared during write: {path}")
    elif (
        current is None
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise OSError(f"report destination changed during write: {path}")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.abspath(json_path) == os.path.abspath(md_path):
        raise ValueError("JSON and Markdown reports must use different paths")
    bundle_id = uuid.uuid4().hex
    bundled_summary = {**summary, "local_report_bundle_id": bundle_id}
    json_payload = (json.dumps(bundled_summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    markdown = markdown.rstrip("\n") + f"\n\n<!-- local_report_bundle_id:{bundle_id} -->\n"
    prepared: list[tuple[Path, Path, os.stat_result | None]] = []
    try:
        json_temp, json_before = _prepare_local_report_output(json_path, json_payload)
        prepared.append((json_path, json_temp, json_before))
        md_temp, md_before = _prepare_local_report_output(
            md_path,
            markdown.encode("utf-8"),
        )
        prepared.append((md_path, md_temp, md_before))
        # JSON is the commit marker: both complete files exist before either
        # destination changes, and JSON is published after Markdown.
        _commit_local_report_output(md_path, md_temp, md_before)
        _commit_local_report_output(json_path, json_temp, json_before)
    finally:
        for _path, temporary, _before in prepared:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [name for name in globals() if not name.startswith("__")]

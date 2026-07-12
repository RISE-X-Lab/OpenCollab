from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.harness import swe_v1_remote_cleanup as cleanup


def _eval_marker(path: Path, *, cid: str = "a" * 64, nonce: str = "b" * 32, **overrides) -> dict:
    value = {
        "schema": cleanup.EVAL_CONTAINER_SCHEMA,
        "state": "active",
        "container_name": "opencollab-prolite-owned",
        "container_id": cid,
        "owner_nonce": nonce,
        "owner_label": cleanup.EVAL_OWNER_LABEL,
        "owner_schema_label": cleanup.EVAL_SCHEMA_LABEL,
        "owner_schema": cleanup.EVAL_SCHEMA_LABEL_VALUE,
    }
    value.update(overrides)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized"])
def test_cleanup_marker_reader_rejects_unsafe_inputs_without_blocking(tmp_path, kind):
    marker = tmp_path / "container.marker.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        _eval_marker(target)
        marker.symlink_to(target)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO requires POSIX")
        os.mkfifo(marker)
    else:
        marker.write_bytes(b"{" + b"x" * (cleanup.MAX_CONTAINER_MARKER_BYTES + 1))

    with pytest.raises((OSError, cleanup.CleanupInputError)):
        cleanup.read_eval_container_marker(
            marker,
            expected_runner_nonce="b" * 32,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema": "wrong"}, "ownership"),
        ({"state": "pending"}, "ownership"),
        ({"container_id": "a" * 12}, "complete container id"),
        ({"owner_nonce": "bad"}, "owner nonce"),
        ({"owner_label": "foreign"}, "ownership"),
        ({"owner_schema": "foreign"}, "ownership"),
    ],
)
def test_eval_marker_requires_schema_full_id_nonce_and_labels(tmp_path, overrides, message):
    marker = tmp_path / "container.marker.json"
    _eval_marker(marker, **overrides)

    with pytest.raises(cleanup.CleanupInputError, match=message):
        cleanup.read_eval_container_marker(
            marker,
            expected_runner_nonce="b" * 32,
        )


def test_owned_container_removal_requires_matching_inspect_and_uses_separator(monkeypatch):
    cid = "a" * 64
    nonce = "b" * 32
    candidate = {
        "kind": "eval",
        "container_id": cid,
        "container_name": "opencollab-prolite-owned",
        "owner_nonce": nonce,
        "owner_label": cleanup.EVAL_OWNER_LABEL,
        "owner_schema_label": cleanup.EVAL_SCHEMA_LABEL,
        "owner_schema": cleanup.EVAL_SCHEMA_LABEL_VALUE,
        "marker_path": "/run/container.marker.json",
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "inspect" and len(calls) == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{cid}\t{nonce}\t{cleanup.EVAL_SCHEMA_LABEL_VALUE}\n",
                stderr="",
            )
        if command[1] == "inspect":
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        return SimpleNamespace(returncode=0, stdout=cid, stderr="")

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)

    result = cleanup.remove_owned_container(candidate)

    assert result["ok"] is True
    assert result["status"] == "removed"
    assert calls[1][0] == ["docker", "rm", "-f", "--", cid]


def test_owner_label_mismatch_refuses_removal(monkeypatch):
    cid = "c" * 64
    candidate = {
        "kind": "generation",
        "container_id": cid,
        "container_name": "opencollab-generation-owned",
        "owner_nonce": "d" * 32,
        "owner_label": cleanup.GENERATION_OWNER_LABEL,
        "marker_path": "/run/owner.json",
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=f"{cid}\tforeign-owner\n",
            stderr="",
        )

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)

    result = cleanup.remove_owned_container(candidate)

    assert result["ok"] is False
    assert result["status"] == "owner_unverified"
    assert len(calls) == 1
    assert calls[0][0][1] == "inspect"


def test_raw_container_id_without_owner_marker_is_reported_not_removed(tmp_path):
    cid = "e" * 64
    (tmp_path / "container.cid").write_text(cid, encoding="ascii")

    candidates, errors = cleanup.discover_owned_containers(
        tmp_path,
        runner_nonce="f" * 32,
    )

    assert candidates == []
    assert errors == [f"{tmp_path / 'container.cid'}: container id has no ownership marker"]


def test_cleanup_tree_scan_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "MAX_SCAN_ENTRIES", 2)
    for index in range(3):
        (tmp_path / f"entry-{index}").write_text("x", encoding="ascii")

    candidates, errors = cleanup.discover_owned_containers(
        tmp_path,
        runner_nonce="f" * 32,
    )

    assert candidates == []
    assert errors == ["cleanup directory entry count exceeds its bound"]


def test_cleanup_tree_scan_does_not_follow_directory_symlinks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _eval_marker(outside / "container.marker.json", nonce="f" * 32)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    candidates, errors = cleanup.discover_owned_containers(
        tmp_path,
        runner_nonce="f" * 32,
    )

    assert candidates == []
    assert errors == []

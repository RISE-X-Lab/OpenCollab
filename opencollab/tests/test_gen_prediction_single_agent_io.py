from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import opencollab.adapters._atomic_rename as atomic_rename_mod
import opencollab.adapters._owned_file_cleanup as owned_cleanup_mod
import opencollab.adapters.safe_files as safe_files_mod
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gp = pytest.importorskip("gen_prediction")


def test_load_instance_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"instance_id": "attacker"}), encoding="utf-8")
    link = tmp_path / "instance.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        gp.load_instance(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_load_instance_rejects_fifo_without_blocking(tmp_path):
    path = tmp_path / "instance.json"
    os.mkfifo(path)

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        gp.load_instance(path)


def test_load_instance_rejects_oversized_document(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "MAX_INSTANCE_BYTES", 32)
    path = tmp_path / "instance.json"
    path.write_bytes(b'{"instance_id":"' + b"x" * 64 + b'"}')

    with pytest.raises(ValueError, match="bounded regular JSON object"):
        gp.load_instance(path)


@pytest.mark.parametrize(
    ("instance_id", "message"),
    [
        (None, "non-empty path component"),
        ("", "non-empty path component"),
        (".", "non-empty path component"),
        ("..", "non-empty path component"),
        ("../victim", "safe path component"),
        (r"C:\\victim", "safe path component"),
        ("task\u202ehidden", "safe path component"),
        ("x" * 241, "byte limit"),
    ],
)
def test_load_instance_rejects_missing_or_unsafe_instance_id(
    tmp_path,
    instance_id,
    message,
):
    path = tmp_path / "instance.json"
    path.write_text(json.dumps({"instance_id": instance_id}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        gp.load_instance(path)


def test_unique_container_name_uses_ascii_slug_and_instance_digest(monkeypatch):
    monkeypatch.setattr(gp.uuid, "uuid4", lambda: type("U", (), {"hex": "a" * 32})())
    instance_id = "café task"

    name = gp.unique_container_name("oc-gen-", instance_id)

    digest = gp.hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:12]
    assert name.isascii()
    assert digest in name
    assert name.endswith("-aaaaaaaaaaaaaaaa")
    assert len(name) <= 63


@pytest.mark.parametrize("instance_id", ["x y", "x:y", "😀"])
def test_default_container_image_maps_unsafe_instance_id_to_stable_ascii(
    instance_id,
):
    image = gp.default_container_image("x86_64", instance_id)
    digest = gp.hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:12]

    assert image.isascii()
    assert digest in image
    assert " " not in image
    assert image.count(":") == 1
    assert image.endswith(":latest")


def test_default_container_image_preserves_canonical_swebench_reference():
    assert (
        gp.default_container_image("x86_64", "django__django-12345")
        == "sweb.eval.x86_64.django__django-12345:latest"
    )


def test_durable_append_rejects_symlink_without_touching_target(tmp_path):
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    link = tmp_path / "predictions.jsonl"
    link.symlink_to(victim)

    with pytest.raises(OSError, match="non-regular"):
        gp._append_jsonl_durable(link, {"instance_id": "task-1"})

    assert victim.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_durable_append_rejects_fifo_without_blocking(tmp_path):
    path = tmp_path / "predictions.jsonl"
    os.mkfifo(path)

    with pytest.raises(OSError, match="non-regular"):
        gp._append_jsonl_durable(path, {"instance_id": "task-1"})


def test_durable_append_rejects_oversized_existing_output(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "MAX_OUTPUT_JSONL_BYTES", 32)
    path = tmp_path / "predictions.jsonl"
    original = b"x" * 32
    path.write_bytes(original)

    with pytest.raises(OSError, match="byte limit"):
        gp._append_jsonl_durable(path, {"instance_id": "task-1"})

    assert path.read_bytes() == original


def test_durable_append_rejects_oversized_row_before_creating_output(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(gp, "MAX_OUTPUT_JSONL_BYTES", 32)
    path = tmp_path / "predictions.jsonl"

    with pytest.raises(OSError, match="row exceeds byte limit"):
        gp._append_jsonl_durable(path, {"payload": "x" * 64})

    assert not path.exists()


def test_durable_append_reports_directory_fsync_failure(tmp_path, monkeypatch):
    path = tmp_path / "predictions.jsonl"

    def fail_directory_fsync(_path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(gp, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        gp._append_jsonl_durable(path, {"instance_id": "task-1"})

    assert json.loads(path.read_text(encoding="utf-8"))["instance_id"] == "task-1"


def test_durable_append_reports_target_replacement_after_write(tmp_path, monkeypatch):
    path = tmp_path / "predictions.jsonl"
    detached = tmp_path / "detached.jsonl"
    original_fsync_directory = gp._fsync_directory
    replaced = False

    def replace_target_after_sync(directory):
        nonlocal replaced
        original_fsync_directory(directory)
        if not replaced and path.exists():
            replaced = True
            path.rename(detached)
            path.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(gp, "_fsync_directory", replace_target_after_sync)

    with pytest.raises(OSError, match="output path changed"):
        gp._append_jsonl_durable(path, {"instance_id": "task-1"})

    assert path.read_text(encoding="utf-8") == "foreign\n"
    assert json.loads(detached.read_text(encoding="utf-8"))["instance_id"] == "task-1"


def test_durable_append_output_lock_has_bounded_wait(tmp_path, monkeypatch):
    path = tmp_path / "predictions.jsonl"
    path.touch()
    holder = os.open(path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(gp, "HARNESS_LOCK_TIMEOUT_SECONDS", 0.03)
    try:
        with pytest.raises(TimeoutError, match="timed out acquiring output lock"):
            gp._append_jsonl_durable(path, {"instance_id": "task-1"})
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_atomic_create_fsyncs_directory_after_noreplace_commit(tmp_path, monkeypatch):
    calls = []
    original_fsync = safe_files_mod.os.fsync

    def record_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(safe_files_mod.os, "fsync", record_fsync)
    target = tmp_path / "owner.json"

    gp._atomic_create_bytes(target, b"{}\n")

    assert len(calls) == 1
    assert target.read_bytes() == b"{}\n"
    assert list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*")) == []


def test_atomic_write_rejects_swapped_temp_without_touching_victim(tmp_path, monkeypatch):
    target = tmp_path / "owner.json"
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"foreign")
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def swap_before_commit(source, destination, **kwargs):
        parent_fd = kwargs["src_dir_fd"]
        os.rename(
            source,
            f"{source}.detached",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.symlink(victim, source, dir_fd=parent_fd)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", swap_before_commit)

    with pytest.raises(OSError, match="changed during create"):
        gp._atomic_write_bytes(target, b"owned")

    assert target.is_symlink() and target.resolve() == victim
    assert victim.read_bytes() == b"foreign"
    detached = next(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*.detached"))
    assert detached.read_bytes() == b"owned"


def test_atomic_create_rejects_foreign_temp_commit_without_deleting_victim(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "owner.json"
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"foreign")
    real_link = os.link
    real_rename_noreplace = atomic_rename_mod.rename_noreplace

    def swap_before_commit(source, destination, **kwargs):
        parent_fd = kwargs["src_dir_fd"]
        os.rename(
            source,
            f"{source}.detached",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        real_link(victim, source, dst_dir_fd=parent_fd)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename_mod, "rename_noreplace", swap_before_commit)

    with pytest.raises(OSError, match="changed during create"):
        gp._atomic_create_bytes(target, b"owned")

    assert target.stat().st_ino == victim.stat().st_ino
    assert victim.stat().st_nlink == 2
    assert victim.read_bytes() == b"foreign"
    detached = next(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*.detached"))
    assert detached.read_bytes() == b"owned"


@pytest.mark.parametrize("operation", ["write", "create"])
def test_atomic_commit_rejects_parent_swap_and_cleans_owned_temp(
    tmp_path,
    monkeypatch,
    operation,
):
    parent = tmp_path / "owners"
    parent.mkdir()
    moved_parent = tmp_path / "owners-moved"
    target = parent / "owner.json"
    if operation == "write":
        target.write_bytes(b"old")
    real_fsync = safe_files_mod.os.fsync
    swapped = False

    def swap_parent_after_payload_fsync(fd):
        nonlocal swapped
        result = real_fsync(fd)
        if not swapped and stat.S_ISREG(os.fstat(fd).st_mode):
            parent.rename(moved_parent)
            parent.mkdir()
            swapped = True
        return result

    monkeypatch.setattr(safe_files_mod.os, "fsync", swap_parent_after_payload_fsync)

    atomic = gp._atomic_write_bytes if operation == "write" else gp._atomic_create_bytes
    with pytest.raises(OSError, match="parent changed before atomic"):
        atomic(target, b"owned")

    assert swapped is True
    assert list(parent.iterdir()) == []
    assert list(moved_parent.glob(".oc-*.tmp")) == []
    if operation == "write":
        assert (moved_parent / "owner.json").read_bytes() == b"old"
    else:
        assert not (moved_parent / "owner.json").exists()


def test_temporary_cleanup_refuses_swapped_path_without_deleting_victim(tmp_path):
    temporary = tmp_path / "owned.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"owned")
    detached = tmp_path / "detached.tmp"
    temporary.rename(detached)
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"foreign")
    temporary.symlink_to(victim)
    original = RuntimeError("primary failure")

    try:
        gp._cleanup_temporary_file(temporary, original, owned_fd=fd)
    finally:
        os.close(fd)

    assert temporary.is_symlink() and temporary.resolve() == victim
    assert detached.read_bytes() == b"owned"
    assert victim.read_bytes() == b"foreign"
    assert not list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert any("retired entry does not match owned file" in note for note in original.__notes__)


def test_temporary_cleanup_retires_owned_inode_without_unlink(
    tmp_path,
    monkeypatch,
):
    temporary = tmp_path / "owned.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"owned")
    monkeypatch.setattr(
        owned_cleanup_mod.os,
        "unlink",
        lambda *_args, **_kwargs: pytest.fail("retirement must not unlink by name"),
        raising=False,
    )
    try:
        gp._cleanup_temporary_file(temporary, None, owned_fd=fd)
    finally:
        os.close(fd)

    assert not temporary.exists()
    retired = list(tmp_path.glob(f"{owned_cleanup_mod.RETIRED_FILE_PREFIX}*"))
    assert len(retired) == 1 and retired[0].read_bytes() == b"owned"


def test_unlink_owner_rejects_symlink_without_touching_target(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "owner.json"
    link.symlink_to(victim)

    with pytest.raises(OSError):
        gp._unlink_owner(link)

    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_unlink_owner_does_not_delete_concurrent_successor(tmp_path, monkeypatch):
    gp.write_container_marker(tmp_path, "cid-a", "name-a")
    owner_path = gp.container_owner_path(tmp_path, "name-a")
    current = gp._read_owner(owner_path)
    assert current is not None
    successor = {
        **current,
        "container_id": "cid-b",
        "owner_token": "successor-token",
    }
    original_match = gp._path_matches_open_file
    raced = False

    def replace_before_match(path, fd):
        nonlocal raced
        if not raced:
            raced = True
            gp._atomic_write_bytes(owner_path, gp._encode_owner(successor))
        return original_match(path, fd)

    monkeypatch.setattr(gp, "_path_matches_open_file", replace_before_match)

    gp._unlink_owner(owner_path)

    assert gp._read_owner(owner_path) == successor


def test_container_markers_are_isolated_by_container(monkeypatch, tmp_path):
    gp.write_container_marker(tmp_path, "cid-a", "name-a")
    gp.write_container_marker(tmp_path, "cid-b", "name-b")
    monkeypatch.setattr(gp, "remove_container", lambda cid: True)

    gp.remove_container_and_clear_marker(tmp_path, "cid-a")

    assert (tmp_path / "container.id").read_text(encoding="utf-8") == "cid-b\n"
    assert (
        tmp_path / ".opencollab" / "containers" / "cid-b" / "container.id"
    ).read_text(encoding="utf-8") == "cid-b\n"


def test_start_container_timeout_compensates_by_unique_name(monkeypatch):
    calls = []
    token = "1" * 32
    removed = False

    def fake_docker(*args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[0] == "run":
            raise subprocess.TimeoutExpired(cmd="docker run", timeout=1)
        if args[0] == "rm":
            removed = True
            return subprocess.CompletedProcess(args, 0, stdout="unique-name\n", stderr="")
        if args[0] == "inspect" and removed:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="No such container"
            )
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0, stdout=token + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(gp, "_docker", fake_docker)

    with pytest.raises(subprocess.TimeoutExpired):
        gp.start_container("image", "unique-name", token)

    run_call = next(call for call in calls if call[0] == "run")
    network_index = run_call.index("--network")
    assert run_call[network_index + 1] == "none"
    assert any(call[:3] == ("rm", "-f", "unique-name") for call in calls)


def test_start_container_name_conflict_does_not_remove_foreign_container(monkeypatch):
    calls = []
    token = "1" * 32

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "run":
            return subprocess.CompletedProcess(
                args,
                125,
                stdout="",
                stderr="daemon lost response",
            )
        if args[0] == "inspect":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="2" * 32 + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(gp, "_docker", fake_docker)

    with pytest.raises(RuntimeError, match="docker run failed"):
        gp.start_container("image", "unique-name", token)

    assert not any(call[:2] == ("rm", "-f") for call in calls)


def test_start_container_setup_failure_compensates_by_container_id(monkeypatch):
    calls = []
    token = "1" * 32
    removed = False

    def fake_docker(*args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[0] == "run":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="abcdef123456789\n",
                stderr="",
            )
        if args[0] == "exec":
            raise subprocess.TimeoutExpired(cmd="docker exec", timeout=1)
        if args[0] == "rm":
            removed = True
            return subprocess.CompletedProcess(args, 0, stdout=args[-1], stderr="")
        if args[0] == "inspect" and removed:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="No such container"
            )
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0, stdout=token + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(gp, "_docker", fake_docker)

    with pytest.raises(subprocess.TimeoutExpired):
        gp.start_container("image", "unique-name", token)

    assert any(call[:3] == ("rm", "-f", "abcdef123456789") for call in calls)


@pytest.mark.parametrize(
    "docker_output",
    [
        "",
        "abc",
        "g" * 64,
        "a" * 65,
        "a" * 12 + " extra",
        "a" * 12 + "\n" + "b" * 12,
    ],
)
def test_start_container_rejects_invalid_full_container_id(
    monkeypatch,
    docker_output,
):
    calls = []
    token = "1" * 32
    removed = False

    def fake_docker(*args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[0] == "run":
            return subprocess.CompletedProcess(
                args, 0, stdout=docker_output, stderr=""
            )
        if args[0] == "rm":
            removed = True
            return subprocess.CompletedProcess(args, 0, stdout=args[-1], stderr="")
        if args[0] == "inspect" and removed:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="No such container"
            )
        return subprocess.CompletedProcess(args, 0, stdout=token + "\n", stderr="")

    monkeypatch.setattr(gp, "_docker", fake_docker)

    with pytest.raises(RuntimeError, match="invalid container id"):
        gp.start_container("image", "unique-name", token)

    assert any(call[:3] == ("rm", "-f", "unique-name") for call in calls)


def test_start_container_marker_failure_removes_created_container(monkeypatch, tmp_path):
    removed = []
    monkeypatch.setattr(
        gp, "start_container", lambda image, name, owner_token: "cid"
    )

    def fail_marker(run_dir, cid, name):
        raise OSError("marker disk full")

    monkeypatch.setattr(gp, "write_container_marker", fail_marker)
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    with pytest.raises(OSError, match="marker disk full"):
        gp.start_container_with_marker("image", "name", tmp_path)

    assert removed == ["name"]
    assert not gp.container_owner_path(tmp_path, "name").exists()


def test_start_container_persists_pending_owner_before_docker_run(monkeypatch, tmp_path):
    observed = {}

    def fake_start(image, name, owner_token):
        owner = json.loads(
            gp.container_owner_path(tmp_path, name).read_text(encoding="utf-8")
        )
        assert owner_token == owner["owner_token"]
        observed.update(owner)
        return "cid123"

    monkeypatch.setattr(gp, "start_container", fake_start)

    cid = gp.start_container_with_marker("image", "unique-name", tmp_path)

    active = json.loads(
        gp.container_owner_path(tmp_path, "unique-name").read_text(encoding="utf-8")
    )
    assert cid == "cid123"
    assert observed["state"] == "pending"
    assert observed["container_id"] == ""
    assert active["state"] == "active"
    assert active["container_id"] == "cid123"


def test_failed_start_retains_pending_owner_when_absence_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gp,
        "start_container",
        lambda image, name, owner_token: (_ for _ in ()).throw(
            RuntimeError("run failed")
        ),
    )
    monkeypatch.setattr(gp, "_remove_labeled_container", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="run failed"):
        gp.start_container_with_marker("image", "unique-name", tmp_path)

    owner = json.loads(
        gp.container_owner_path(tmp_path, "unique-name").read_text(encoding="utf-8")
    )
    assert owner["state"] == "pending"
    assert owner["container_name"] == "unique-name"


def test_startup_recovers_stale_pending_owner(monkeypatch, tmp_path):
    owner = gp._create_pending_owner(tmp_path, "stale-name")
    owner["owner_pid"] = 2**30
    owner["owner_start_identity"] = "proc:dead"
    path = gp.container_owner_path(tmp_path, "stale-name")
    gp._atomic_write_bytes(path, gp._encode_owner(owner))
    removed = []
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    assert gp.recover_stale_container_owners(tmp_path) is True

    assert removed == ["stale-name"]
    assert not path.exists()


def test_remove_container_requires_absent_inspect_result(monkeypatch):
    responses = iter(
        [
            subprocess.CompletedProcess(("rm",), 0, stdout="cid\n", stderr=""),
            subprocess.CompletedProcess(("inspect",), 0, stdout="cid\n", stderr=""),
        ]
    )
    monkeypatch.setattr(gp, "_docker", lambda *args, **kwargs: next(responses))

    assert gp.remove_container("cid") is False


def test_remove_container_accepts_only_confirmed_missing_inspect(monkeypatch):
    responses = iter(
        [
            subprocess.CompletedProcess(("rm",), 0, stdout="cid\n", stderr=""),
            subprocess.CompletedProcess(
                ("inspect",), 1, stdout="", stderr="Error: No such object: cid"
            ),
        ]
    )
    monkeypatch.setattr(gp, "_docker", lambda *args, **kwargs: next(responses))

    assert gp.remove_container("cid") is True


def test_keep_marker_failure_forces_container_cleanup(monkeypatch, tmp_path):
    gp.write_container_marker(tmp_path, "cid", "name")
    removed = []
    monkeypatch.setattr(
        gp,
        "mark_container_kept",
        lambda run_dir, cid: (_ for _ in ()).throw(OSError("marker failed")),
    )
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    with pytest.raises(OSError, match="marker failed"):
        gp.finalize_container_ownership(
            run_dir=tmp_path,
            cid="cid",
            name="name",
            keep_container=True,
            completed=True,
            metrics={},
        )

    assert removed == ["cid"]
    assert not gp.container_owner_path(tmp_path, "name").exists()


def test_keep_container_is_ignored_for_failed_generation(monkeypatch, tmp_path):
    gp.write_container_marker(tmp_path, "cid", "name")
    removed = []
    metrics = {}
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    gp.finalize_container_ownership(
        run_dir=tmp_path,
        cid="cid",
        name="name",
        keep_container=True,
        completed=False,
        metrics=metrics,
    )

    assert removed == ["cid"]
    assert metrics["container_cleanup_succeeded"] is True
    assert not gp.container_owner_path(tmp_path, "name").exists()


def test_completed_keep_container_persists_kept_owner(monkeypatch, tmp_path):
    gp.write_container_marker(tmp_path, "cid", "name")
    metrics = {}

    gp.finalize_container_ownership(
        run_dir=tmp_path,
        cid="cid",
        name="name",
        keep_container=True,
        completed=True,
        metrics=metrics,
    )

    owner = json.loads(
        gp.container_owner_path(tmp_path, "name").read_text(encoding="utf-8")
    )
    assert owner["state"] == "kept"
    assert metrics["container_retained"] is True
    monkeypatch.setattr(
        gp,
        "remove_container",
        lambda reference: (_ for _ in ()).throw(AssertionError("kept owner recovered")),
    )
    assert gp.recover_stale_container_owners(tmp_path) is True

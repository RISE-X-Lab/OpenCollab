"""Platform-boundary regressions for descriptor-safe adapter files."""

from __future__ import annotations

import builtins
import errno
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import opencollab.adapters._atomic_rename as atomic_rename
import opencollab.adapters._owned_file_cleanup as owned_cleanup
import opencollab.adapters._posix_file_support as platform_support
import opencollab.adapters.safe_files as safe_files
import pytest


def test_project_metadata_declares_posix_platform_support():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert '"Operating System :: OS Independent"' not in pyproject
    assert '"Operating System :: POSIX"' not in pyproject
    assert '"Operating System :: POSIX :: Linux"' in pyproject
    assert '"Operating System :: MacOS"' in pyproject


def test_platform_module_imports_without_fcntl_and_reports_clear_failure(monkeypatch):
    module_path = Path(platform_support.__file__)
    spec = importlib.util.spec_from_file_location(
        "opencollab_test_posix_file_support_probe",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    real_import = builtins.__import__

    def import_without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("simulated non-POSIX host")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_fcntl)
    spec.loader.exec_module(probe)

    with pytest.raises(
        probe.UnsupportedSafeFilePlatformError,
        match=r"require POSIX support \(fcntl\.flock unavailable\)",
    ):
        probe.require_posix_file_support()


def test_safe_file_call_checks_platform_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_support, "_fcntl", None)

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match=r"require POSIX support \(fcntl\.flock unavailable\)",
    ):
        safe_files.read_regular_bytes(tmp_path / "value", max_bytes=1)


def test_non_posix_host_reports_explicit_platform_failure(monkeypatch):
    monkeypatch.setattr(platform_support.os, "name", "nt")

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match=r"require POSIX support \(POSIX host unavailable\)",
    ):
        platform_support.require_posix_file_support()


def test_owned_file_cleanup_checks_platform_before_dir_fd_use(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_support, "_fcntl", None)
    parent_fd = -1
    try:
        parent_fd = safe_files.os.open(tmp_path, safe_files.os.O_RDONLY)
        with pytest.raises(
            platform_support.UnsupportedSafeFilePlatformError,
            match=r"require POSIX support \(fcntl\.flock unavailable\)",
        ):
            owned_cleanup.retire_unverified_file(
                parent_fd,
                "value",
                path_label=str(tmp_path / "value"),
            )
    finally:
        if parent_fd >= 0:
            safe_files.os.close(parent_fd)


def test_capability_check_includes_hard_links_and_fd_listdir(monkeypatch):
    monkeypatch.setattr(
        platform_support,
        "_SUPPORTED_DIR_FD_FUNCTION_NAMES",
        platform_support._SUPPORTED_DIR_FD_FUNCTION_NAMES - {"link"},
    )
    monkeypatch.setattr(platform_support, "_SUPPORTS_FD_LISTDIR", False)

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match=r"dir_fd operations, file-descriptor listdir unavailable",
    ):
        platform_support.require_posix_file_support()


def test_atomic_rename_reports_missing_native_noreplace_symbol(monkeypatch):
    monkeypatch.setattr(atomic_rename, "_native_rename_noreplace", None)

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match="require atomic rename-noreplace",
    ):
        atomic_rename.rename_noreplace(
            "source",
            "destination",
            src_dir_fd=-1,
            dst_dir_fd=-1,
        )


def test_atomic_rename_reports_missing_native_exchange_symbol(monkeypatch):
    monkeypatch.setattr(atomic_rename, "_native_rename_exchange", None)

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match="require atomic rename-exchange",
    ):
        atomic_rename.rename_exchange(
            "first",
            "second",
            first_dir_fd=-1,
            second_dir_fd=-1,
        )


def test_atomic_create_uses_hard_link_when_nfs_rejects_rename_noreplace(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "value.json"
    real_rename_noreplace = atomic_rename.rename_noreplace

    def reject_target_create(source, destination, **kwargs):
        if destination == target.name:
            raise OSError(errno.EINVAL, "simulated NFS renameat2 rejection", destination)
        return real_rename_noreplace(source, destination, **kwargs)

    monkeypatch.setattr(atomic_rename, "rename_noreplace", reject_target_create)

    safe_files.write_regular_bytes_atomic(target, b"payload")

    assert target.read_bytes() == b"payload"
    aliases = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(owned_cleanup.RETIRED_FILE_PREFIX)
    ]
    assert len(aliases) == 1
    assert aliases[0].stat().st_ino == target.stat().st_ino


def test_capability_check_requires_callable_flock(monkeypatch):
    monkeypatch.setattr(platform_support, "_fcntl", ModuleType("fcntl_without_flock"))

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match=r"fcntl\.flock unavailable",
    ):
        platform_support.require_posix_file_support()


@pytest.mark.parametrize("missing_name", ["link", "stat"])
def test_capability_check_requires_no_follow_link_and_stat(monkeypatch, missing_name):
    monkeypatch.setattr(
        platform_support,
        "_SUPPORTED_FOLLOW_SYMLINK_FUNCTION_NAMES",
        platform_support._SUPPORTED_FOLLOW_SYMLINK_FUNCTION_NAMES - {missing_name},
    )

    with pytest.raises(
        platform_support.UnsupportedSafeFilePlatformError,
        match=r"no-follow link/stat operations unavailable",
    ):
        platform_support.require_posix_file_support()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS root aliases")
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("/etc", "/private/etc"),
        ("/tmp", "/private/tmp"),
        ("/var", "/private/var"),
    ],
)
def test_macos_system_root_alias_is_normalized_only_after_verification(
    alias,
    canonical,
):
    candidate = Path(alias) / "folders" / "safe-file.json"

    assert platform_support.normalize_trusted_root_alias(candidate) == (
        Path(canonical) / "folders" / "safe-file.json"
    )


def _macos_alias_path(path: Path) -> Path:
    canonical_path = Path(os.path.realpath(path))
    aliases = (
        (Path("/tmp"), Path("/private/tmp")),
        (Path("/var"), Path("/private/var")),
    )
    for alias, canonical in aliases:
        try:
            relative = canonical_path.relative_to(canonical)
        except ValueError:
            continue
        return alias / relative
    pytest.skip(f"temporary path has no supported macOS root alias: {canonical_path}")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS root aliases")
def test_safe_write_accepts_verified_macos_temporary_alias(tmp_path):
    aliased_target = _macos_alias_path(tmp_path) / "result.json"

    safe_files.write_regular_bytes_atomic(aliased_target, b"safe")

    assert (tmp_path / "result.json").read_bytes() == b"safe"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS root aliases")
def test_macos_alias_normalization_still_rejects_descendant_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    link = tmp_path / "attacker-link"
    link.symlink_to(victim, target_is_directory=True)
    aliased_target = _macos_alias_path(tmp_path) / link.name / "escaped.json"

    with pytest.raises(OSError, match="not a real directory"):
        safe_files.write_regular_bytes_atomic(aliased_target, b"escaped")

    assert not (victim / "escaped.json").exists()


def test_unverified_root_alias_is_not_normalized(monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "darwin")
    monkeypatch.setattr(
        platform_support,
        "_verified_macos_root_alias",
        lambda _alias, _canonical: False,
    )

    assert platform_support.normalize_trusted_root_alias(Path("/var/owned/file")) == Path(
        "/var/owned/file"
    )


def test_arbitrary_descendant_is_never_canonicalized(monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "darwin")
    monkeypatch.setattr(
        platform_support,
        "_verified_macos_root_alias",
        lambda alias, canonical: (alias, canonical) == ("/var", "/private/var"),
    )

    assert platform_support.normalize_trusted_root_alias(
        Path("/var/attacker-link/file")
    ) == Path(
        "/private/var/attacker-link/file"
    )

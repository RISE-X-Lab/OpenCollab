"""Unit tests for the file-backed and null skill stores."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencollab.adapters.skills import file_skill_store as skill_store_mod
from opencollab.adapters.skills.file_skill_store import (
    SKILL_BODY_MAX_CHARS,
    FileSkillStore,
)
from opencollab.adapters.skills.null_skill_store import NullSkillStore
from opencollab.domain.skill import SkillManifest


def _write_skill(root: Path, name: str, *, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


# --- NullSkillStore ---------------------------------------------------------


def test_null_store_lists_nothing():
    store = NullSkillStore()
    assert store.list_manifests() == ()


def test_null_store_body_is_none():
    store = NullSkillStore()
    assert store.get_body("anything") is None


# --- FileSkillStore: parsing + retrieval ------------------------------------


def test_file_store_parses_frontmatter_into_manifests(tmp_path):
    _write_skill(tmp_path, "alpha", description="Alpha skill.", body="Do alpha things.")
    store = FileSkillStore(tmp_path)
    manifests = store.list_manifests()
    assert manifests == (SkillManifest(name="alpha", description="Alpha skill."),)


def test_file_store_lists_all_skills(tmp_path):
    _write_skill(tmp_path, "alpha", description="A.", body="a")
    _write_skill(tmp_path, "beta", description="B.", body="b")
    names = {m.name for m in FileSkillStore(tmp_path).list_manifests()}
    assert names == {"alpha", "beta"}


def test_file_store_get_body_hit(tmp_path):
    _write_skill(tmp_path, "alpha", description="A.", body="full instructions here")
    store = FileSkillStore(tmp_path)
    assert store.get_body("alpha") == "full instructions here"


def test_file_store_get_body_miss_returns_none(tmp_path):
    _write_skill(tmp_path, "alpha", description="A.", body="x")
    store = FileSkillStore(tmp_path)
    assert store.get_body("nonexistent") is None


# --- FileSkillStore: robustness ---------------------------------------------


def test_file_store_absent_dir_is_empty(tmp_path):
    store = FileSkillStore(tmp_path / "does-not-exist")
    assert store.list_manifests() == ()
    assert store.get_body("alpha") is None


def test_file_store_skips_malformed_without_raising(tmp_path):
    # A well-formed skill plus a garbage one in a sibling dir.
    _write_skill(tmp_path, "good", description="Good.", body="ok")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text("not frontmatter at all\njust text", encoding="utf-8")
    store = FileSkillStore(tmp_path)  # must not raise
    names = {m.name for m in store.list_manifests()}
    assert names == {"good"}
    assert store.get_body("bad") is None


def test_file_store_skips_skill_missing_name(tmp_path):
    d = tmp_path / "noname"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name key\n---\nbody", encoding="utf-8")
    store = FileSkillStore(tmp_path)
    assert store.list_manifests() == ()


def test_file_store_skips_unclosed_frontmatter(tmp_path):
    d = tmp_path / "unclosed"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: unclosed\ndescription: x\nbody never closes", encoding="utf-8")
    store = FileSkillStore(tmp_path)
    assert store.list_manifests() == ()


def test_file_store_skips_dir_without_skill_md(tmp_path):
    (tmp_path / "empty").mkdir()
    _write_skill(tmp_path, "good", description="G.", body="ok")
    store = FileSkillStore(tmp_path)
    assert {m.name for m in store.list_manifests()} == {"good"}


# --- FileSkillStore: size cap (single cap site) -----------------------------


def test_file_store_caps_oversized_body(tmp_path):
    big_body = "x" * (SKILL_BODY_MAX_CHARS + 5_000)
    _write_skill(tmp_path, "huge", description="Huge.", body=big_body)
    store = FileSkillStore(tmp_path)
    body = store.get_body("huge")
    assert body is not None
    assert len(body) <= SKILL_BODY_MAX_CHARS + 200  # cap + truncation marker
    assert len(body) < len(big_body)
    assert "truncated" in body


def test_file_store_does_not_cap_small_body(tmp_path):
    _write_skill(tmp_path, "small", description="S.", body="tiny")
    store = FileSkillStore(tmp_path)
    assert store.get_body("small") == "tiny"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_file_store_skips_unsafe_or_oversized_skill_file(
    tmp_path,
    monkeypatch,
    kind,
):
    skill_dir = tmp_path / "unsafe"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    if kind == "fifo":
        os.mkfifo(skill_file)
    elif kind == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text("---\nname: leaked\n---\nsecret", encoding="utf-8")
        skill_file.symlink_to(outside)
    else:
        skill_file.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(skill_store_mod, "MAX_SKILL_FILE_BYTES", 64)

    store = FileSkillStore(tmp_path)

    assert store.list_manifests() == ()


def test_file_store_does_not_follow_symlink_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    _write_skill(real, "alpha", description="A", body="secret")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    store = FileSkillStore(linked)

    assert store.list_manifests() == ()


def test_file_store_root_enumeration_is_bounded(tmp_path, monkeypatch):
    for index in range(4):
        (tmp_path / f"entry-{index}").mkdir()
    monkeypatch.setattr(skill_store_mod, "MAX_SKILL_ROOT_ENTRIES", 3)

    with pytest.raises(ValueError, match="entries exceed limit"):
        FileSkillStore(tmp_path)

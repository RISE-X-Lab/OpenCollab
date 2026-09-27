"""Public patch paths retain Git filenames and both rename endpoints."""

from __future__ import annotations

import subprocess

import pytest

from opencollab.patches import (
    decode_git_c_path,
    diff_target_path,
    git_diff_endpoint,
    git_header_tokens,
    normalize_patch_path,
    patch_block_target_path,
    patch_entries,
    patch_paths,
    split_patch_blocks,
)


@pytest.mark.parametrize(
    "name",
    ["trailing ", " leading", "both sides ", "unicode\u2028name", "tab\tname", "\u4e2d\u6587.txt", "a b/path"],
)
@pytest.mark.parametrize("kind", ["text", "binary", "rename", "mode"])
@pytest.mark.parametrize("quoted", ["true", "false"])
def test_patch_paths_preserve_real_git_filenames(tmp_path, name, kind, quoted):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"before\x00" if kind == "binary" else b"before\n")
    git("add", "-A")
    git("commit", "-qm", "\u521d\u59cb\u5316")
    if kind == "rename":
        path.rename(tmp_path / (name + " renamed "))
        git("add", "-A")
    elif kind == "mode":
        path.chmod(0o755)
    else:
        path.write_bytes(b"after\x00" if kind == "binary" else b"after\n")
    diff = git("-c", "core.quotePath=" + quoted, "diff", "-M", "HEAD")
    expected = [(name, name + " renamed ")] if kind == "rename" else [(name, name)]
    assert patch_entries(diff) == expected
    assert patch_paths(diff) == list(dict.fromkeys(path for pair in expected for path in pair))
    assert patch_block_target_path(split_patch_blocks(diff)[0]) == expected[0][1]


def test_git_c_quoted_paths_decode_utf8_bytes_and_control_characters():
    token = r'"b/caf\303\251\t\"\\.py"'
    assert decode_git_c_path(token) == 'b/café\t"\\.py'
    assert git_diff_endpoint(token, "b") == 'café\t"\\.py'
    header = f'diff --git "a/source.py" {token}'
    assert git_header_tokens(header) == ['"a/source.py"', token]
    assert diff_target_path(header) == 'café\t"\\.py'


def test_patch_parsing_retains_copy_endpoints_and_repository_b_directory():
    patch = (
        "diff --git a/b/source.py b/pkg/copied.py\n"
        "similarity index 100%\n"
        "copy from b/source.py\n"
        "copy to pkg/copied.py\n"
    )
    assert patch_entries(patch) == [("b/source.py", "pkg/copied.py")]
    assert patch_paths(patch) == ["b/source.py", "pkg/copied.py"]
    assert normalize_patch_path("b/source.py") == "b/source.py"


def test_patch_blocks_keep_hunk_bytes_and_addition_deletion_endpoints():
    added = (
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
    )
    deleted = (
        "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n"
        "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
    )
    blocks = split_patch_blocks(added + deleted)
    assert ["".join(block) for block in blocks] == [added, deleted]
    assert patch_entries(added + deleted) == [("", "new.txt"), ("old.txt", "")]
    assert patch_paths(added + deleted) == ["new.txt", "old.txt"]
    assert patch_block_target_path(blocks[1]) == "old.txt"


def test_public_patch_parser_retains_all_path_categories():
    paths = ["src/test_parser.py", "tests/test_bug.py", ".opencollab-validation/probe.py"]
    patch = "\n".join(f"diff --git a/{path} b/{path}" for path in paths)
    assert patch_paths(patch) == paths
    assert patch_paths("") == []
    assert patch_block_target_path(split_patch_blocks(patch)[0] + split_patch_blocks(patch)[1]) == ""

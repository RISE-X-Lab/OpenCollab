import asyncio
import tracemalloc
from hashlib import sha256

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.apply_patch_engine import _find_block
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


def test_apply_patch_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 1,
                "end_line": 1,
                "new_str": "x",
            },
            runtime,
        )
    )

    assert result == "Error: no execution environment available."


def test_line_replace_applies_range_and_stale_guard_keeps_file_unchanged(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")
    tool = ApplyPatchTool()
    runtime = _runtime(ws)

    result = run(
        tool.execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 3,
                "new_str": "X\nY",
            },
            runtime,
        )
    )

    assert "Applied line_replace" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"

    result = run(
        tool.execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "stale write",
                "expected_str": "WRONG",
            },
            runtime,
        )
    )

    assert "expected_str does not match" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"


def test_unified_diff_matches_by_content_despite_line_drift(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("extra0\nextra1\nlineA\nlineB\nlineC\n", encoding="utf-8")
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " lineA\n"
        "-lineB\n"
        "+lineB_fixed\n"
        " lineC\n"
    )

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch},
            _runtime(ws),
        )
    )

    assert "Applied unified_diff" in result
    assert target.read_text(encoding="utf-8") == "extra0\nextra1\nlineA\nlineB_fixed\nlineC\n"


def test_fuzzy_block_search_uses_constant_auxiliary_memory():
    # Roughly 4 MiB of repeated source text. Every position matches, so the old
    # collect-and-sort implementation allocated hundreds of thousands of ints.
    source = ["repeated-line"] * 350_000
    tracemalloc.start()
    try:
        position = _find_block(
            source,
            ["repeated-line"],
            expected_idx=175_000,
            min_idx=0,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert position == 175_000
    assert peak_bytes < 1_000_000


def test_fuzzy_block_search_preserves_nearest_then_earliest_tie_break():
    source = ["x", "target", "x", "target", "x"]

    assert _find_block(source, ["target"], expected_idx=2, min_idx=0) == 1
    assert _find_block(source, ["target"], expected_idx=3, min_idx=0) == 3


def test_unified_diff_failed_hunk_writes_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    original = "a\nb\nc\nd\ne\nf\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "@@ -5,2 +5,2 @@\n"
        " e\n"
        "-NOPE\n"
        "+X\n"
    )

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch},
            _runtime(ws),
        )
    )

    assert "hunk #2" in result
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "params",
    [
        {
            "path": "f.py",
            "mode": "line_replace",
            "start_line": 1,
            "end_line": 1,
            "new_str": "a",
        },
        {
            "path": "f.py",
            "mode": "unified_diff",
            "patch": "@@ -1,2 +1,2 @@\n a\n b\n",
        },
    ],
)
def test_apply_patch_rejects_successful_noop_without_writing(tmp_path, params):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("a\nb\n", encoding="utf-8")

    result = run(ApplyPatchTool().execute_with_runtime(params, _runtime(ws)))

    assert result.startswith("Error applying patch")
    assert "no changes" in result
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_unified_diff_treats_header_like_hunk_content_as_content(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("old\n", encoding="utf-8")

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "unified_diff",
                "patch": "@@ -1 +1 @@\n-old\n+++value\n",
            },
            _runtime(ws),
        )
    )

    assert "Applied unified_diff" in result
    assert target.read_text(encoding="utf-8") == "++value\n"


def test_unified_diff_recomputes_a_miscounted_hunk_header(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    original = "a\nb\nc\n"
    target.write_text(original, encoding="utf-8")

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "unified_diff",
                "patch": "@@ -1,3 +1,3 @@\n a\n-b\n+x\n",
            },
            _runtime(ws),
        )
    )

    # The header says 3 old and 3 new lines; the body describes 2 and 2. The
    # applier never reads those counts -- it finds the hunk by matching " a"
    # and "-b" against the file -- so the miscount is recomputed away rather
    # than turned into a rejection of an edit that was described correctly.
    assert result.startswith("Applied unified_diff")
    assert target.read_text(encoding="utf-8") == "a\nx\nc\n"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("@@ -0,0 +1 @@", "X\na\nb\nc\n"),
        ("@@ -2,0 +3 @@", "a\nb\nX\nc\n"),
        ("@@ -3,0 +4 @@", "a\nb\nc\nX\n"),
    ],
)
def test_unified_diff_pure_insertions_use_header_position(tmp_path, header, expected):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "unified_diff",
                "patch": f"{header}\n+X\n",
            },
            _runtime(ws),
        )
    )

    assert "Applied unified_diff" in result
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    ("source", "patch", "expected"),
    [
        (
            "old\n",
            "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n",
            "new",
        ),
        (
            "old",
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n",
            "new\n",
        ),
    ],
)
def test_unified_diff_applies_eof_newline_metadata(tmp_path, source, patch, expected):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text(source, encoding="utf-8")

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch},
            _runtime(ws),
        )
    )

    assert "Applied unified_diff" in result
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (
            FileWriteTool(),
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "alpha\ntarget",
                "new_str": "alpha\nchanged",
            },
        ),
        (
            ApplyPatchTool(),
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "expected_str": "target",
                "new_str": "changed",
            },
        ),
        (
            ApplyPatchTool(),
            {
                "path": "f.py",
                "mode": "unified_diff",
                "patch": "@@ -1,3 +1,3 @@\n alpha\n-target\n+changed\n omega\n",
            },
        ),
    ],
    ids=("str-replace", "line-replace", "unified-diff"),
)
def test_edit_modes_match_logical_lines_and_preserve_crlf(tmp_path, tool, params):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_bytes(b"alpha\r\ntarget\r\nomega\r\n")

    result = run(tool.execute_with_runtime(params, _runtime(ws)))

    assert not result.startswith("Error:")
    assert target.read_bytes() == b"alpha\r\nchanged\r\nomega\r\n"


def test_line_edit_rejects_mixed_newlines_without_rewriting_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    original = b"alpha\r\ntarget\nomega\r\n"
    target.write_bytes(original)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "changed",
            },
            _runtime(ws),
        )
    )

    assert "mixed newline styles" in result
    assert target.read_bytes() == original


def test_apply_patch_reports_file_and_workspace_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ApplyPatchTool()
    runtime = _runtime(ws)

    missing = run(
        tool.execute_with_runtime(
            {
                "path": "missing.py",
                "mode": "line_replace",
                "start_line": 1,
                "end_line": 1,
                "new_str": "x",
            },
            runtime,
        )
    )
    with pytest.raises(PermissionError, match="Path escapes workspace"):
        run(
            tool.execute_with_runtime(
                {
                    "path": "/etc/passwd",
                    "mode": "line_replace",
                    "start_line": 1,
                    "end_line": 1,
                    "new_str": "x",
                },
                runtime,
            )
        )

    assert missing.startswith("Error: file not found:")
    assert missing.endswith("missing.py")


def test_str_replace_identical_old_and_new_is_explicit_noop_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "hello world",
                "new_str": "hello world",
            },
            _runtime(ws),
        )
    )

    assert "no-op" in result
    assert result.startswith("Error:")
    # File untouched.
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_str_replace_success_reports_content_changed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "world",
                "new_str": "there",
            },
            _runtime(ws),
        )
    )

    assert "content changed" in result
    assert target.read_text(encoding="utf-8") == "hello there\n"


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (
            FileWriteTool(),
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "target",
                "new_str": "changed",
            },
        ),
        (
            ApplyPatchTool(),
            {
                "path": "f.py",
                "mode": "unified_diff",
                "patch": "@@ -1 +1 @@\n-target\n+changed\n",
            },
        ),
    ],
)
def test_non_utf8_edit_attempt_is_explicit_and_preserves_bytes(tmp_path, tool, params):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    original = b"prefix\ntarget\xffkeep\n"
    target.write_bytes(original)
    before = sha256(original).hexdigest()

    result = run(tool.execute_with_runtime(params, _runtime(ws)))

    assert result == "Error: refusing to edit non-UTF-8 file: invalid UTF-8 at byte 13."
    assert target.read_bytes() == original
    assert sha256(target.read_bytes()).hexdigest() == before


def test_str_replace_preserves_legal_utf8_with_cjk(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("\u4f60\u597d\uff0c\u4e16\u754c\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "\u4e16\u754c",
                "new_str": "OpenCollab",
            },
            _runtime(ws),
        )
    )

    assert "Replaced in" in result
    assert target.read_text(encoding="utf-8") == "\u4f60\u597d\uff0cOpenCollab\n"


# ---------------------------------------------------------------------------
# Coordinates are hints; content is the check.
#
# Measured over 30 pilot runs, 34 of the 35 apply_patch failures were a model
# that had described its edit correctly and named the position wrongly: a
# miscounted hunk header, a header with no numbers at all, or a line range that
# had drifted under a correctly quoted expected_str. None of the three is
# information this applier uses -- it locates every edit by content -- so none
# of them rejects an edit any more. What still fails is text that does not
# match the file, or matches it in more than one place.
# ---------------------------------------------------------------------------


def _apply(ws, params):
    return run(ApplyPatchTool().execute_with_runtime(params, _runtime(ws)))


def _file(tmp_path, text, name="f.py"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    target = ws / name
    target.write_text(text, encoding="utf-8")
    return ws, target


def test_a_hunk_header_with_no_numbers_still_applies(tmp_path):
    ws, target = _file(tmp_path, "a\nb\nc\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "unified_diff",
        "patch": "@@\n a\n-b\n+x\n",
    })

    assert result.startswith("Applied unified_diff")
    assert target.read_text(encoding="utf-8") == "a\nx\nc\n"


def test_several_numberless_hunks_apply_in_the_order_they_are_written(tmp_path):
    # Without a position each hunk takes the first match after the previous
    # one, which is what a sequential diff means. "b" appears twice; the second
    # hunk must land on the later one.
    ws, target = _file(tmp_path, "b\nmiddle\nb\ntail\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "unified_diff",
        "patch": "@@\n-b\n+first\n@@\n-b\n+second\n",
    })

    assert result.startswith("Applied unified_diff")
    assert target.read_text(encoding="utf-8") == "first\nmiddle\nsecond\ntail\n"


def test_a_hunk_whose_context_is_not_in_the_file_is_still_refused(tmp_path):
    ws, target = _file(tmp_path, "a\nb\nc\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "unified_diff",
        "patch": "@@ -1,2 +1,2 @@\n zzz\n-nope\n+x\n",
    })

    assert "did not match the file" in result
    assert target.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_a_header_with_an_empty_body_is_still_refused(tmp_path):
    ws, target = _file(tmp_path, "a\nb\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "unified_diff",
        "patch": "@@\n@@\n a\n-b\n+x\n",
    })

    assert "has no body" in result
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_line_replace_lands_on_a_uniquely_matching_expected_str(tmp_path):
    ws, target = _file(tmp_path, "one\ntwo\nthree\nfour\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "line_replace",
        "start_line": 1,
        "end_line": 1,
        "expected_str": "three",
        "new_str": "THREE",
    })

    assert result.startswith("Applied line_replace")
    assert target.read_text(encoding="utf-8") == "one\ntwo\nTHREE\nfour\n"


def test_a_relocated_line_replace_says_where_it_actually_landed(tmp_path):
    # Silence here would leave the caller planning its next edit against line
    # numbers that were already wrong.
    ws, _ = _file(tmp_path, "one\ntwo\nthree\nfour\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "line_replace",
        "start_line": 1,
        "end_line": 1,
        "expected_str": "three",
        "new_str": "THREE",
    })

    assert "did not match lines 1-1" in result
    assert "matched lines 3-3 uniquely" in result


def test_line_replace_refuses_an_expected_str_that_matches_twice(tmp_path):
    ws, target = _file(tmp_path, "dup\nmiddle\ndup\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "line_replace",
        "start_line": 2,
        "end_line": 2,
        "expected_str": "dup",
        "new_str": "X",
    })

    assert "appears 2 times" in result
    assert target.read_text(encoding="utf-8") == "dup\nmiddle\ndup\n"


def test_line_replace_refuses_an_expected_str_that_is_not_in_the_file(tmp_path):
    ws, target = _file(tmp_path, "a\nb\n")

    result = _apply(ws, {
        "path": "f.py",
        "mode": "line_replace",
        "start_line": 1,
        "end_line": 1,
        "expected_str": "absent",
        "new_str": "X",
    })

    assert "does not appear anywhere in the file" in result
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_an_unknown_mode_says_where_str_replace_actually_lives(tmp_path):
    # Nine of the pilot rejections were `mode: "str_replace"` on this tool.
    # The enum error alone never said that the mode exists on file_write.
    ws, _ = _file(tmp_path, "a\n")

    result = _apply(ws, {"path": "f.py", "mode": "str_replace"})

    assert "file_write" in result


def test_the_description_does_not_send_the_model_looking_for_str_replace():
    description = ApplyPatchTool().description
    assert "str_replace is a mode of the" in description
    assert "`file_write` tool" in description

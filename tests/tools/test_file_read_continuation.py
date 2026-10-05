"""Character cuts and line pagination need distinct continuation advice."""

import asyncio
import re

import pytest

from opencollab.adapters._env_base import Environment
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.application.tool_execution import ToolRuntime


class _ReadFileOnly:
    def __init__(self, content):
        self.content = content

    async def read_file(self, path):
        return self.content


class _DefaultRangeEnvironment(_ReadFileOnly, Environment):
    pass


class _ReadFileOnlyLocalEnvironment:
    local_filesystem = True

    def __init__(self, environment):
        self.workspace = environment.workspace
        self.read_file = environment.read_file
        self.write_file = environment.write_file


class _DefaultRangeLocalEnvironment(_ReadFileOnlyLocalEnvironment, Environment):
    pass


def _runtime(tmp_path, content, backend):
    if backend == "local":
        (tmp_path / "source.txt").write_text(content, encoding="utf-8")
        environment = LocalEnvironment(str(tmp_path))
    elif backend == "default_range":
        environment = _DefaultRangeEnvironment(content)
    else:
        environment = _ReadFileOnly(content)
    return ToolRuntime(environment=environment, safety_policy=None, permission_policy=None)


async def _cleanup(runtime):
    if isinstance(runtime.environment, LocalEnvironment):
        await runtime.environment.cleanup()


def _rows(result):
    return [
        (int(match.group(1)), match.group(2))
        for line in result.splitlines()
        if (match := re.fullmatch(r"(\d+)\t(.*)", line))
    ]


def _displayed_body(result):
    body = result.split("\n", 1)[1]
    footer = re.search(r"\n\.\.\. (?:line \d+ was|(?:\d+ )?more lines below)", body)
    return body[:footer.start()] if footer else body


def test_long_single_line_read_explains_character_cut_without_next_line(tmp_path):
    (tmp_path / "minified.js").write_text("x" * 200, encoding="utf-8")
    runtime = ToolRuntime(environment=LocalEnvironment(str(tmp_path)), safety_policy=None, permission_policy=None)

    result = asyncio.run(FileReadTool(max_read_chars=100).execute_with_runtime({"path": "minified.js"}, runtime))

    assert "character" in result
    assert "max_read_chars" in result
    assert "more lines below" not in result
    assert "offset=2" not in result


def test_complete_line_window_keeps_working_line_pagination(tmp_path):
    (tmp_path / "lines.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")
    runtime = ToolRuntime(environment=LocalEnvironment(str(tmp_path)), safety_policy=None, permission_policy=None)
    tool = FileReadTool(max_read_chars=100)

    first = asyncio.run(tool.execute_with_runtime({"path": "lines.txt", "limit": 1}, runtime))
    second = asyncio.run(tool.execute_with_runtime({"path": "lines.txt", "offset": 2, "limit": 1}, runtime))

    assert "offset=2" in first
    assert "2\tsecond" in second
    assert "offset=3" in second


@pytest.mark.parametrize("backend", ["local", "default_range", "read_file_only"])
async def test_numbered_pages_deliver_every_line_without_exceeding_output_budget(tmp_path, backend):
    lines = [f"{number:04d} " + "x" * 51 for number in range(1, 502)]
    runtime = _runtime(tmp_path, "\n".join(lines) + "\n", backend)
    tool = FileReadTool()
    try:
        first = await tool.execute_with_runtime({"path": "source.txt"}, runtime)
        first_rows = _rows(first)
        assert 0 < len(first_rows) < 500
        assert first_rows == list(enumerate(lines[:len(first_rows)], 1))
        next_offset = len(first_rows) + 1
        assert f"offset={next_offset}" in first
        assert "chars truncated" not in first
        assert len(_displayed_body(first)) <= tool.max_read_chars

        second = await tool.execute_with_runtime({"path": "source.txt", "offset": next_offset}, runtime)
        assert first_rows + _rows(second) == list(enumerate(lines, 1))
        assert len(_displayed_body(second)) <= tool.max_read_chars
        assert "Continue with" not in second
    finally:
        await _cleanup(runtime)


@pytest.mark.parametrize("backend", ["local", "default_range", "read_file_only"])
@pytest.mark.parametrize(("offset", "budget"), [(8, 16), (98, 20), (998, 24)])
async def test_pagination_accounts_for_growing_line_numbers(tmp_path, backend, offset, budget):
    runtime = _runtime(tmp_path, "a\n" * (offset + 4), backend)
    try:
        result = await FileReadTool(max_read_chars=budget).execute_with_runtime(
            {"path": "source.txt", "offset": offset}, runtime,
        )
        assert _rows(result) == [(number, "a") for number in range(offset, offset + 3)]
        assert f"showing {offset}-{offset + 2}" in result
        assert f"offset={offset + 3}" in result
        assert len(_displayed_body(result)) <= budget
    finally:
        await _cleanup(runtime)


@pytest.mark.parametrize("backend", ["local", "default_range", "read_file_only"])
async def test_pagination_keeps_unterminated_final_line_at_exact_output_boundary(tmp_path, backend):
    runtime = _runtime(tmp_path, "first\nfinal", backend)
    try:
        result = await FileReadTool(max_read_chars=15).execute_with_runtime({"path": "source.txt"}, runtime)
        assert _displayed_body(result) == "1\tfirst\n2\tfinal"
        assert len(_displayed_body(result)) == 15
        assert "Continue with" not in result
        assert "character-truncated" not in result

        tool = FileReadTool(max_read_chars=7)
        first = await tool.execute_with_runtime({"path": "source.txt"}, runtime)
        assert _rows(first) == [(1, "first")]
        assert "offset=2" in first
        final = await tool.execute_with_runtime({"path": "source.txt", "offset": 2}, runtime)
        assert _rows(final) == [(2, "final")]
        assert "Continue with" not in final
    finally:
        await _cleanup(runtime)


@pytest.mark.parametrize("backend", ["local", "default_range", "read_file_only"])
async def test_partial_later_line_is_read_again_before_character_truncation(tmp_path, backend):
    runtime = _runtime(tmp_path, "a\n" + "x" * 200, backend)
    tool = FileReadTool(max_read_chars=100)
    try:
        first = await tool.execute_with_runtime({"path": "source.txt"}, runtime)
        assert _rows(first) == [(1, "a")]
        assert "offset=2" in first
        assert "character-truncated" not in first

        second = await tool.execute_with_runtime({"path": "source.txt", "offset": 2}, runtime)
        assert "line 2 was character-truncated" in second
        assert "max_read_chars" in second
        assert "offset=3" not in second
        assert len(_displayed_body(second)) <= tool.max_read_chars
    finally:
        await _cleanup(runtime)


@pytest.mark.parametrize("backend", ["local", "default_range", "read_file_only"])
async def test_single_line_truncation_includes_its_line_number_budget(tmp_path, backend):
    runtime = _runtime(tmp_path, "x" * 100, backend)
    try:
        result = await FileReadTool(max_read_chars=100).execute_with_runtime({"path": "source.txt"}, runtime)
        assert "line 1 was character-truncated" in result
        assert "offset=2" not in result
        assert len(_displayed_body(result)) <= 100
    finally:
        await _cleanup(runtime)


@pytest.mark.parametrize("backend", [_DefaultRangeLocalEnvironment, _ReadFileOnlyLocalEnvironment])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("trailing_newline", [False, True])
async def test_compatible_file_reader_preserves_unicode_line_coordinates(
    tmp_path, backend, newline, trailing_newline,
):
    target = tmp_path / "records.jsonl"
    lines = ['{"text":"\u4e2d\u6587\u2028caf\u00e9"}', '{"keep":"old"}', '{"tail":"safe"}']
    suffix = newline if trailing_newline else ""
    target.write_bytes((newline.join(lines) + suffix).encode("utf-8"))
    local = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=backend(local), safety_policy=None, permission_policy=None)
    reader = FileReadTool()
    try:
        displayed = await reader.execute_with_runtime({"path": str(target)}, runtime)
        displayed_keep_line = next(
            int(row.partition("\t")[0])
            for row in displayed.split("\n")
            if row.partition("\t")[2] == lines[1]
        )
        output = await ApplyPatchTool().execute_with_runtime({
            "path": str(target), "mode": "line_replace",
            "start_line": displayed_keep_line, "end_line": displayed_keep_line,
            "new_str": '{"keep":"new"}',
        }, runtime)

        assert output.startswith("Applied"), output
        expected = newline.join([lines[0], '{"keep":"new"}', lines[2]]) + suffix
        assert target.read_bytes() == expected.encode("utf-8"), (displayed, output)
        assert "3 -> 3 lines" in output
        assert displayed_keep_line == 2
        assert "3 lines total, showing 1-3" in displayed
        for offset, line in enumerate(lines, 1):
            page = await reader.execute_with_runtime(
                {"path": str(target), "offset": offset, "limit": 1}, runtime,
            )
            expected_line = '{"keep":"new"}' if offset == 2 else line
            assert page.split("\n")[1] == f"{offset}\t{expected_line}"
            assert ("Continue with" in page) == (offset < len(lines))
    finally:
        await local.cleanup()

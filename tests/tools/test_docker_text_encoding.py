"""Native Docker edits preserve file bytes when UTF-8 decoding fails."""

from __future__ import annotations

import os

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_process import PROCESS_OUTPUT_CAPTURE_BYTES, run_process
from opencollab.adapters.env import DockerEnvironment
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.application.tool_execution import ToolRuntime


@pytest.fixture
def docker_runtime(tmp_path, monkeypatch):
    """Run the normal Docker shell wrapper through the local process supervisor."""
    env = DockerEnvironment(
        workspace=str(tmp_path), container_id="a" * 64, exec_workdir=str(tmp_path),
        command_prefix="export OC_TEXT_ENCODING_PROBE=wrapped",
    )
    env._attached_bound = True
    calls = []

    async def local_transport(*argv, timeout=120, input_bytes=None):
        assert argv[0] == "exec"
        boundary = argv.index("--")
        command = list(argv[boundary + 2:])
        assert docker_module._EXEC_WRAPPER in command
        assert "OC_TEXT_ENCODING_PROBE=wrapped" in command[-1]
        assert env._active_execs
        command[4] = str(tmp_path / os.path.basename(command[4]))
        process_env = dict(os.environ)
        for index, arg in enumerate(argv[:boundary]):
            if arg == "-e":
                name, value = argv[index + 1].split("=", 1)
                process_env[name] = value
        calls.append({"command": command[-1], "write": input_bytes is not None})
        return await run_process(
            command, shell=False, cwd=argv[argv.index("-w") + 1],
            timeout=timeout, input_bytes=input_bytes, env=process_env,
        )

    monkeypatch.setattr(env, "_docker", local_transport)
    yield ToolRuntime(env, None, None), calls
    env.revoke()


def edit_call(mode, path):
    if mode == "str_replace":
        return FileWriteTool(), {
            "path": str(path), "mode": mode, "old_str": "flag = old", "new_str": "flag = new",
        }
    if mode == "unified_diff":
        return ApplyPatchTool(), {
            "path": str(path), "mode": mode,
            "patch": "@@ -2 +2 @@\n-flag = old\n+flag = new\n",
        }
    return ApplyPatchTool(), {
        "path": str(path), "mode": mode, "start_line": 2, "end_line": 2,
        "expected_str": "flag = old", "new_str": "flag = new",
    }


@pytest.mark.parametrize("mode", ["str_replace", "line_replace", "unified_diff"])
async def test_docker_native_edit_rejects_non_utf8_and_preserves_all_bytes(
    tmp_path, docker_runtime, mode,
):
    runtime, calls = docker_runtime
    target = tmp_path / "latin1.txt"
    original = "title = café\nflag = old\n".encode("latin-1")
    target.write_bytes(original)
    tool, params = edit_call(mode, target)

    output = await tool.execute_with_runtime(params, runtime)

    assert target.read_bytes() == original, (output, target.read_bytes().hex())
    assert output.startswith("Error: refusing to edit non-UTF-8 file"), output
    assert calls and not any(call["write"] for call in calls)
    assert not runtime.environment._active_execs


@pytest.mark.parametrize("title", ["café \u4e2d\u6587", "valid replacement �"])
@pytest.mark.parametrize("mode", ["str_replace", "line_replace", "unified_diff"])
async def test_docker_native_edit_retains_untouched_utf8_bytes(
    tmp_path, docker_runtime, title, mode,
):
    runtime, calls = docker_runtime
    target = tmp_path / "utf8.txt"
    original = f"title = {title}\nflag = old\n".encode("utf-8")
    target.write_bytes(original)
    tool, params = edit_call(mode, target)

    output = await tool.execute_with_runtime(params, runtime)
    read_output = await FileReadTool().execute_with_runtime({"path": str(target)}, runtime)

    assert not output.startswith("Error"), output
    assert title in read_output
    assert target.read_bytes() == original.replace(b"flag = old", b"flag = new")
    assert any(call["write"] for call in calls)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing_newline", [False, True])
async def test_docker_displayed_unicode_line_edits_preserve_other_records(
    tmp_path, docker_runtime, newline, trailing_newline,
):
    runtime, _calls = docker_runtime
    target = tmp_path / "records.jsonl"
    records = [
        '{"text":"\u7b2c\u4e00\u2028\u7b2c\u4e8c"}',
        '{"keep":"caf\u00e9 \u5b89\u5168"}',
        '{"tail":"\u672b\u5c3e"}',
    ]
    suffix = newline if trailing_newline else ""
    target.write_bytes((newline.join(records) + suffix).encode("utf-8"))
    reader = FileReadTool()
    displayed = await reader.execute_with_runtime({"path": str(target)}, runtime)
    displayed_keep_line = next(
        int(row.partition("\t")[0])
        for row in displayed.split("\n")
        if row.partition("\t")[2] == records[1]
    )
    replacement = '{"keep":"\u65b0\u503c"}'

    output = await ApplyPatchTool().execute_with_runtime({
        "path": str(target), "mode": "line_replace",
        "start_line": displayed_keep_line, "end_line": displayed_keep_line,
        "new_str": replacement,
    }, runtime)

    assert output.startswith("Applied"), output
    expected = newline.join([records[0], replacement, records[2]]) + suffix
    assert target.read_bytes() == expected.encode("utf-8"), (displayed, output)
    assert "3 -> 3 lines" in output
    assert displayed_keep_line == 2
    assert displayed.split("\n")[1:] == [
        f"{number}\t{record}" for number, record in enumerate(records, 1)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing_newlines", [0, 1, 2])
async def test_docker_single_line_pages_keep_inline_unicode_and_final_record(
    tmp_path, docker_runtime, newline, trailing_newlines,
):
    runtime, _calls = docker_runtime
    target = tmp_path / "pages.txt"
    lines = ["\u4e2d\u6587\u2028second\u2029third\u0085last", "caf\u00e9", "tail"]
    source = newline.join(lines) + newline * trailing_newlines
    target.write_bytes(source.encode("utf-8"))
    expected_lines = lines + ([""] if trailing_newlines == 2 else [])
    pages = [
        await runtime.environment.read_text_range(
            str(target), offset=offset, limit=1, max_chars=100,
        )
        for offset in range(1, len(expected_lines) + 2)
    ]

    assert [line for page in pages for line in page.lines] == expected_lines
    assert [page.has_more for page in pages] == [
        offset < len(expected_lines) for offset in range(1, len(expected_lines) + 2)
    ]
    assert all(not page.chars_truncated for page in pages)
    assert target.read_bytes() == source.encode("utf-8")


async def test_docker_file_read_rejects_non_utf8(tmp_path, docker_runtime):
    runtime, _calls = docker_runtime
    target = tmp_path / "latin1.txt"
    original = b"title = caf\xe9\nflag = old\n"
    target.write_bytes(original)

    with pytest.raises(UnicodeDecodeError):
        await runtime.environment.read_file(str(target))

    assert target.read_bytes() == original


@pytest.mark.parametrize("text", ["A" + "é" * 12, "€" * 12, "😀" * 12])
async def test_docker_text_range_accepts_utf8_character_split_by_byte_cap(
    tmp_path, docker_runtime, text,
):
    runtime, _calls = docker_runtime
    target = tmp_path / "long.txt"
    target.write_text(text, encoding="utf-8")

    window = await runtime.environment.read_text_range(
        str(target), offset=1, limit=1, max_chars=2,
    )

    assert window.lines == [text[:2]]
    assert window.chars_truncated
    assert window.has_more


async def test_docker_file_read_rejects_incomplete_utf8_at_file_end(
    tmp_path, docker_runtime,
):
    runtime, _calls = docker_runtime
    target = tmp_path / "incomplete.txt"
    target.write_bytes(b"flag = old\n\xc3")

    with pytest.raises(UnicodeDecodeError):
        await runtime.environment.read_file(str(target))


async def test_docker_file_read_keeps_capture_limit_error(tmp_path, docker_runtime):
    runtime, _calls = docker_runtime
    target = tmp_path / "large.txt"
    target.write_bytes(b"\xe9" + b"x" * PROCESS_OUTPUT_CAPTURE_BYTES)

    with pytest.raises(OSError, match="capture limit"):
        await runtime.environment.read_file(str(target))


async def test_docker_command_output_still_displays_non_utf8(docker_runtime):
    runtime, _calls = docker_runtime

    result = await runtime.environment.exec_cmd("printf '\\351'; printf '\\377' >&2")

    assert result.returncode == 0
    assert result.stdout == "�"
    assert result.stderr.startswith("�")

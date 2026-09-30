"""File delivery preserves complete evidence without a giant judge prompt."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencollab.builtin_workflows import _file_selection as new
from opencollab.builtin_workflows import _selection as contract
from opencollab.builtin_workflows import duo
from opencollab.builtin_workflows._candidate_evidence_files import CandidateEvidenceFiles, ReadCandidateEvidence
from opencollab.workflows import CandidateRun
from tests.support.duo_test_support import Context, candidate, decision


def with_diff(value):
    return CandidateRun(label="candidate", output="finished", diff=value, test_records=(), verified_targets=())


@pytest.mark.asyncio
async def test_all_text_and_binary_bytes_are_retained_with_exact_index_ranges(tmp_path):
    patch = (
        'diff --git "a/space name.txt" "b/space name.txt"\n'
        '--- "a/space name.txt"\n+++ "b/space name.txt"\n@@ -1 +1 @@\n-old\r\n+\u4f60\u597d\U0001f642\r\n'
        "diff --git a/data.bin b/data.bin\nGIT binary patch\nliteral 2\nAbCD"
    )
    files = CandidateEvidenceFiles(tmp_path)
    view = files.add_candidate("A", with_diff(patch))
    assert (files.directory / view["diff_path"]).read_bytes() == patch.encode()
    index = [json.loads(row) for row in (files.directory / view["index_path"]).read_text().splitlines()]
    assert index[0]["paths"] == [["space name.txt", "space name.txt"]]
    assert index[1]["kind"] == "binary_patch"
    assert "".join(patch[row["offset"]:row["offset"] + row["length"]] for row in index) == patch
    tool = ReadCandidateEvidence(files)
    chunks, offset = [], 0
    while True:
        out = json.loads(await tool.execute_with_runtime(
            {"path": view["diff_path"], "offset": offset, "limit": 7}, None,
        ))
        chunks.append(out["content"])
        if out["eof"]:
            break
        offset = out["next_offset"]
    assert "".join(chunks) == patch
    assert files.directory.exists()


@pytest.mark.asyncio
async def test_tool_reads_registered_evidence_only_and_bounds_each_response(tmp_path):
    files = CandidateEvidenceFiles(tmp_path)
    files.add_candidate("A", candidate("A", "a"))
    secret = tmp_path / "hidden-test.py"
    secret.write_text("PRIVATE TEST CONTENT")
    tool = ReadCandidateEvidence(files)
    for params in [
        {"path": str(secret)}, {"path": "../hidden-test.py"},
        {"path": "A/candidate.diff", "offset": -1},
        {"path": "A/candidate.diff", "offset": 10**20},
        {"path": "A/candidate.diff", "limit": 32769},
        {"path": "A/candidate.diff", "offset": True},
    ]:
        out = json.loads(await tool.execute_with_runtime(params, None))
        assert "error" in out and "PRIVATE TEST CONTENT" not in json.dumps(out)
    assert secret.read_text() == "PRIVATE TEST CONTENT"
    assert "command" not in tool.parameters["properties"]


class ReadingContext(Context):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.evidence_directories = []

    async def log(self, message):
        if message.startswith("Duo complete adjudication evidence directory: "):
            self.evidence_directories.append(Path(message.split(": ", 1)[1]))

    async def agent(self, prompt, **options):
        self.selector_calls.append((prompt, options))
        if not options["tools"]:
            return self.result
        tool, = options["tools"]
        for label in ["A", "B"]:
            index = json.loads(await tool.execute_with_runtime({"path": f"{label}/index.jsonl"}, None))
            entry = json.loads(index["content"].splitlines()[0])
            content = json.loads(await tool.execute_with_runtime({
                "path": entry["diff_path"], "offset": entry["offset"],
                "limit": min(32768, entry["length"]),
            }, None))
            assert "diff --git " in content["content"]
        return self.result


@pytest.mark.asyncio
async def test_real_oversize_trigger_is_moved_to_files_and_remains_readable(tmp_path):
    large = "diff --git a/index.lz4 b/index.lz4\nGIT binary patch\nliteral 11000000\n" + "B" * 11_000_000
    a, b = candidate("A", "a"), with_diff(large)
    ctx = ReadingContext(result=decision("index.lz4 contains the required metadata"))
    await new.adjudicate_candidate_files(
        ctx, goal="Keep public behavior", candidate_a=a, candidate_b=b, evidence_parent=str(tmp_path),
    )
    prompt, options = ctx.selector_calls[0]
    assert len(prompt) < 10000
    assert "B" * 100 not in prompt
    assert '"inline_comparison":' not in prompt
    tool, = options["tools"]
    result = json.loads(await tool.execute_with_runtime(
        {"path": "B/candidate.diff", "offset": len(large) - 40000, "limit": 32768}, None,
    ))
    assert len(result["content"]) == 32768 and result["next_offset"] == len(large) - 7232
    assert (ctx.evidence_directories[0] / "B/candidate.diff").read_bytes() == large.encode()


@pytest.mark.asyncio
async def test_duo_uses_file_evidence_and_retains_the_actual_selected_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    monkeypatch.setattr(new, "_INLINE_EVIDENCE_MAX_BYTES", 0)
    ctx = ReadingContext()
    result = await duo(ctx, {"goal": "Preserve behavior", "candidate_evidence_dir": str(tmp_path)})
    assert result["winner"] == result["adopted"] == "B"
    assert ctx.adoptions[0][0].label == "dual-coder-contract-b"
    assert [tool.name for tool in ctx.selector_calls[0][1]["tools"]] == ["read_candidate_evidence"]
    assert ctx.selector_calls[0][1]["budget"] is None
    assert ctx.evidence_directories[0].parent == tmp_path
    result_file = ctx.evidence_directories[0] / "B/result.json"
    report = json.loads(result_file.read_text())
    assert report["candidate_report"] == "Public repair completed"
    assert report["report_is_model_supplied"] is True


@pytest.mark.asyncio
async def test_duo_defaults_to_b_when_original_evidence_validation_fails(tmp_path):
    ctx = ReadingContext(result=decision("unsupported claim without original changed path"))
    result = await duo(
        ctx, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
    )
    assert result["winner"] == "B"
    assert result["selection_reason"] == "contract-evidence-insufficient-default-b"
    assert len(ctx.selector_calls) == 2


@pytest.mark.asyncio
async def test_small_inline_comparison_retains_complete_diffs_and_shared_public_records(tmp_path):
    public_record = {
        "target": "tests/test_handler.py", "runner": "pytest",
        "command": "pytest tests/test_handler.py", "exit_code": 0, "verified": True,
    }
    a, b = [
        CandidateRun(
            label=label,
            output={
                "coder_output": f"Candidate {label} repair completed",
                "public_test_records": [public_record, {
                    **public_record, "target": f"tests/test_{label.lower()}.py",
                    "command": f"pytest tests/test_{label.lower()}.py",
                }],
            },
            diff=candidate(label, value).diff,
            test_records=(), verified_targets=(),
        ) for label, value in [("A", "\u8fd4\u56de A"), ("B", "\u8fd4\u56de B")]
    ]
    ctx = ReadingContext()
    winner, result, reason = await new.adjudicate_candidate_files(
        ctx, goal="Preserve public behavior", candidate_a=a, candidate_b=b,
        selector_prompt="{candidates}", evidence_parent=str(tmp_path),
    )
    assert winner == "B" and result == ctx.result and reason == "contract-adjudicated"
    prompt, options = ctx.selector_calls[0]
    payload, _ = json.JSONDecoder().raw_decode(prompt)
    expected = json.loads(contract._judge_input(a, b)[0])
    for label, source in [("A", a), ("B", b)]:
        expected[label].update(
            public_test_records=source.output["public_test_records"],
            candidate_report=source.output["coder_output"], report_is_model_supplied=True,
        )
    assert payload["inline_comparison"] == expected
    assert payload["inline_comparison"]["A"]["diff"] == a.diff
    assert payload["inline_comparison"]["B"]["diff"] == b.diff
    assert len(payload["inline_comparison"]["shared_public_test_records"]) == 1
    assert options["tools"] == []
    assert len(ctx.selector_calls) == 1
    for label, source in [("A", a), ("B", b)]:
        assert payload[label]["index_path"] == f"{label}/index.jsonl"
        assert (ctx.evidence_directories[0] / payload[label]["diff_path"]).read_bytes() == source.diff.encode()
        inline = payload["inline_comparison"][label]
        assert len(inline["public_test_records"]) == 2
        assert inline["candidate_report"] == source.output["coder_output"]
        assert inline["report_is_model_supplied"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("large_field", ["candidate_report", "public_test_records"])
async def test_inline_limit_includes_reports_and_individual_public_records(tmp_path, large_field):
    a, source = candidate("A", "a"), candidate("B", "b")
    output = {"coder_output": "finished", "public_test_records": []}
    if large_field == "candidate_report":
        output["coder_output"] = "report detail " * 10000
    else:
        output["public_test_records"] = [{
            "target": "test_public.py", "runner": "pytest", "command": "argument " * 16000,
            "exit_code": 0, "verified": True,
        }]
    b = CandidateRun(label="B", output=output, diff=source.diff, test_records=(), verified_targets=())
    assert len(contract._judge_input(a, b)[0].encode("utf-8")) < 128000
    ctx = ReadingContext()
    await new.adjudicate_candidate_files(
        ctx, goal="Preserve public behavior", candidate_a=a, candidate_b=b,
        selector_prompt="{candidates}", evidence_parent=str(tmp_path),
    )
    prompt, options = ctx.selector_calls[0]
    payload, _ = json.JSONDecoder().raw_decode(prompt)
    assert "inline_comparison" not in payload
    tool, = options["tools"]
    assert tool.name == "read_candidate_evidence"
    saved_path = payload["B"]["result_path" if large_field == "candidate_report" else "public_evidence_path"]
    chunks, offset = [], 0
    while True:
        page = json.loads(await tool.execute_with_runtime({"path": saved_path, "offset": offset, "limit": 32768}, None))
        chunks.append(page["content"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    saved = json.loads("".join(chunks))
    assert saved[large_field] == output["coder_output" if large_field == "candidate_report" else large_field]


@pytest.mark.asyncio
async def test_inline_size_uses_utf8_bytes_and_complete_large_evidence_stays_readable(tmp_path):
    a = candidate("A", "a")
    b = candidate("B", "\u4f60" * 45000)
    encoded, _, _ = contract._judge_input(a, b)
    assert len(encoded) < 128000 < len(encoded.encode("utf-8"))
    ctx = ReadingContext()
    await new.adjudicate_candidate_files(
        ctx, goal="Preserve public behavior", candidate_a=a, candidate_b=b,
        selector_prompt="{candidates}", evidence_parent=str(tmp_path),
    )
    prompt, options = ctx.selector_calls[0]
    payload, _ = json.JSONDecoder().raw_decode(prompt)
    assert "inline_comparison" not in payload
    tool, = options["tools"]
    chunks, offset = [], 0
    while True:
        page = json.loads(await tool.execute_with_runtime({
            "path": payload["B"]["diff_path"], "offset": offset, "limit": 32768,
        }, None))
        chunks.append(page["content"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert "".join(chunks) == b.diff


@pytest.mark.asyncio
async def test_identical_candidates_skip_judge_and_concurrent_runs_keep_separate_files(tmp_path):
    identical = ReadingContext(identical=True)
    await duo(
        identical, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
    )
    assert not identical.selector_calls and not list(tmp_path.iterdir())
    left, right = ReadingContext(), ReadingContext()
    await asyncio.gather(*[
        duo(
            ctx, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
        ) for ctx in [left, right]
    ])
    assert left.evidence_directories[0] != right.evidence_directories[0]

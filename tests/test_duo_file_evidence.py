"""File delivery preserves complete evidence without a giant judge prompt."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from test_duo_selector import Context, candidate, decision

from opencollab.builtin_workflows import _file_selection as new
from opencollab.builtin_workflows._candidate_evidence_files import CandidateEvidenceFiles, ReadCandidateEvidence
from opencollab.workflows import CandidateRun

old = importlib.import_module("opencollab.builtin_workflows.duo")


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
    tool, = options["tools"]
    result = json.loads(await tool.execute_with_runtime(
        {"path": "B/candidate.diff", "offset": len(large) - 40000, "limit": 32768}, None,
    ))
    assert len(result["content"]) == 32768 and result["next_offset"] == len(large) - 7232
    assert (ctx.evidence_directories[0] / "B/candidate.diff").read_bytes() == large.encode()


@pytest.mark.asyncio
async def test_v3_keeps_coder_prompts_adoption_and_validated_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", "1")
    old_ctx, new_ctx = Context(), ReadingContext()
    args = {"goal": "Preserve the public return value", "candidate_evidence_dir": str(tmp_path)}
    expected = await old.duo(old_ctx, args)
    actual = await new.duo_v3(new_ctx, args)
    assert actual == expected
    assert new_ctx.phases == old_ctx.phases
    assert new_ctx.adoptions == old_ctx.adoptions
    for old_call, new_call in zip(old_ctx.coder_calls, new_ctx.coder_calls, strict=True):
        assert old_call[0] == new_call[0]
        assert {k: v for k, v in old_call[1].items() if k != "tools"} == {
            k: v for k, v in new_call[1].items() if k != "tools"
        }
    assert old_ctx.selector_calls[0][1]["tools"] == []
    assert new_ctx.selector_calls[0][1]["budget"] is None
    assert new_ctx.evidence_directories[0].parent == tmp_path


@pytest.mark.asyncio
async def test_v3_keeps_default_a_when_original_evidence_validation_fails(tmp_path):
    ctx = ReadingContext(result=decision("unsupported claim without original changed path"))
    result = await new.duo_v3(
        ctx, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
    )
    assert result["winner"] == "A"
    assert result["selection_reason"] == "contract-evidence-insufficient-default-a"


@pytest.mark.asyncio
async def test_identical_candidates_skip_judge_and_concurrent_runs_keep_separate_files(tmp_path):
    identical = ReadingContext(identical=True)
    await new.duo_v3(
        identical, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
    )
    assert not identical.selector_calls and not list(tmp_path.iterdir())
    left, right = ReadingContext(), ReadingContext()
    await asyncio.gather(*[
        new.duo_v3(
            ctx, {"goal": "Public behavior", "candidate_evidence_dir": str(tmp_path)},
        ) for ctx in [left, right]
    ])
    assert left.evidence_directories[0] != right.evidence_directories[0]



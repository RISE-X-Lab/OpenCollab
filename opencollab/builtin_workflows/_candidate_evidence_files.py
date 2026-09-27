"""Complete candidate evidence files with a narrowly scoped read-only tool."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from opencollab.patches import patch_entries, split_patch_blocks
from opencollab.workflows import CandidateRun

from . import _candidate_records as dual


class CandidateEvidenceFiles:
    """Retain exact diffs and expose character ranges through registered paths.

    Files live outside the candidates. The tool reads on the workflow host, so
    a judge in an isolated source container needs no mount into either coder.
    The caller owns retention of ``directory`` after adjudication finishes.
    """

    def __init__(self, parent: str | Path | None = None) -> None:
        if parent is not None:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="duo-evidence-", dir=parent))
        self._files: dict[str, tuple[Path, int]] = {}

    def _write(self, name: str, content: str) -> str:
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        self._files[name] = (path, len(content))
        return name

    def _json(self, name: str, value: Any) -> str:
        return self._write(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def add_candidate(self, label: str, candidate: CandidateRun) -> dict[str, Any]:
        diff_path = self._write(f"{label}/candidate.diff", candidate.diff)
        offset = 0
        entries = []
        for lines in split_patch_blocks(candidate.diff):
            block = "".join(lines)
            entries.append({
                "paths": [list(pair) for pair in patch_entries(block)],
                "kind": "binary_patch" if "GIT binary patch\n" in block else "text_or_metadata",
                "diff_path": diff_path,
                "offset": offset,
                "length": len(block),
            })
            offset += len(block)
        index_path = self._write(
            f"{label}/index.jsonl",
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        )
        records_path = self._json(f"{label}/public-evidence.json", {
            "public_command": dual._candidate_command(candidate),
            "public_test_records": dual._candidate_records(candidate),
        })
        result_path = self._json(f"{label}/result.json", {
            "candidate_report": (
                dual._candidate_output(candidate).get("coder_output")
                if isinstance(candidate.output, dict) else candidate.output
            ),
            "report_is_model_supplied": True,
        })
        return {
            "result_path": result_path,
            "index_path": index_path,
            "diff_path": diff_path,
            "public_evidence_path": records_path,
            "diff_characters": len(candidate.diff),
            "diff_bytes": len(candidate.diff.encode("utf-8")),
            "diff_blocks": len(entries),
        }

    def add_shared_records(self, records: list[dict[str, Any]]) -> str:
        return self._json("shared-public-test-records.json", records)

    def read(self, path: str, offset: int, limit: int) -> dict[str, Any]:
        if path not in self._files:
            raise ValueError("Unknown evidence path; use a path listed in the evidence index.")
        file, total = self._files[path]
        if not 0 <= offset <= total:
            raise ValueError(f"offset must be between 0 and {total} characters.")
        # newline="" preserves CRLF as well as arbitrary missing final newlines.
        # Character offsets keep successive pages safe across UTF-8 boundaries.
        with file.open(encoding="utf-8", newline="") as stream:
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, 65536))
                if not skipped:
                    raise OSError("Evidence file ended before its recorded offset.")
                remaining -= len(skipped)
            content = stream.read(min(limit, total - offset))
        end = offset + len(content)
        if end < min(total, offset + limit):
            raise OSError("Evidence file ended before its recorded length.")
        return {
            "path": path,
            "offset": offset,
            "content": content,
            "next_offset": end if end < total else None,
            "total_characters": total,
            "eof": end == total,
        }


class ReadCandidateEvidence:
    """Read registered evidence only, with no shell or candidate mutation access."""

    name = "read_candidate_evidence"
    description = (
        "Read the complete saved evidence for candidate A or B, including indexes, "
        "public test records, and exact text or binary diffs. Paths are relative to "
        "this adjudication's evidence directory. Offsets and limits count Unicode "
        "characters. Follow next_offset to read further; there is no total-read limit."
    )
    default_timeout = 60.0
    disable_outer_timeout = False
    max_read_characters = 32768
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "An evidence path from the prompt or index."},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset, default 0."},
            "limit": {
                "type": "integer", "minimum": 1, "maximum": max_read_characters,
                "description": "Characters to read per response, default 16000. Continue with next_offset.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, files: CandidateEvidenceFiles) -> None:
        self.files = files

    def to_openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        del runtime
        path = params.get("path")
        offset, limit = params.get("offset", 0), params.get("limit", 16000)
        if not isinstance(path, str) or type(offset) is not int or type(limit) is not int:
            return json.dumps({"error": "path must be text; offset and limit must be integers."})
        if not 1 <= limit <= self.max_read_characters:
            return json.dumps({"error": f"limit must be between 1 and {self.max_read_characters}."})
        try:
            result = await asyncio.to_thread(self.files.read, path, offset, limit)
        except (ValueError, OSError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

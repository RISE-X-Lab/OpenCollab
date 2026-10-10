"""Original competition prompts loaded from reviewable text resources."""

import json
from pathlib import Path

from arc_light.evidence import repair_frontier, summarize

_ROOT = Path(__file__).with_name("prompts")
SYSTEM_PROMPT = (_ROOT / "system.md").read_text(encoding="utf-8")
DELIVERY_RULES = (_ROOT / "delivery.md").read_text(encoding="utf-8")
PLUS_RULES = (_ROOT / "evolution.md").read_text(encoding="utf-8")
SPEC_DIR = ".arc/evolution-spec/by-id"


def workspace_context(output_dir: Path, baseline: str | None = None) -> str:
    text = (
        f"Application workspace: {output_dir.resolve()}\n"
        f"Requirement cards: {(output_dir / SPEC_DIR).resolve()}\n"
        "Tool paths are relative to this application workspace; shell cd does not persist.\n"
        "Browser checks resolve @playwright/test from backend/. Resolve that module and try\n"
        "chromium.launch before declaring browsers unavailable; do not reinstall browsers.\n"
    )
    if baseline:
        text += (
            f"Main work is already committed. Inspect git diff {baseline} HEAD --stat or git show HEAD; "
            "uncommitted git diff may be empty.\n"
        )
    return text + "\n"


def _repair_prompt(result):
    return (
        (_ROOT / "repair.md")
        .read_text(encoding="utf-8")
        .replace("@TARGETS@", json.dumps(repair_frontier(result), ensure_ascii=False))
        .replace("@COVERAGE@", json.dumps(summarize(result)["coverage"], ensure_ascii=False))
    )

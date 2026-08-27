"""Per-agent worktree changes must land on disk as a structured trace record.

The diff a finished child hands its parent is prose: it is wrapped in a
markdown code fence and, past ``WORKTREE_DIFF_MAX_CHARS``, has its middle cut
out. That string is the only place the "which agent touched which file"
evidence used to live, so it could not be used as evidence.

These tests pin a second, separate destination: a ``worktree_changes`` trace
record built from the *untruncated* diff, carrying one entry per changed file
with the sha256 of the file's post-change content. They drive the real
:class:`~opencollab.adapters.trace.Tracer` and read the JSONL back off disk, so
a field that never reaches the file fails here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase, SessionState


class _Env:
    """A diff-capable worktree stand-in with a readable file table."""

    def __init__(self, diff: str, files: dict[str, str]):
        self._diff = diff
        self._files = files
        self.diff_calls = 0

    async def get_diff(self) -> str:
        self.diff_calls += 1
        return self._diff

    async def read_file(self, path: str) -> str:
        return self._files[path]


class _UnreadableEnv(_Env):
    async def read_file(self, path: str) -> str:
        raise RuntimeError("no reading today")


class _ChildSession:
    def __init__(self, role: str, result: str, env: _Env):
        self.agent = type("_Agent", (), {"name": role})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = env
        self._result = result

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        self.state.set_phase(SessionPhase.DONE)
        return self._result


class _LeadSession:
    def __init__(self):
        self.agent = type("_Agent", (), {"name": "lead"})()
        self.state = SessionState(messages=[])
        self.used_tokens = 0
        self.env = None

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _Factory:
    def __init__(self, child: _ChildSession):
        self._child = child

    def build_spawn_session(
        self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""
    ):
        self._child.state.aid = aid
        return self._child


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_one_child(tmp_path, env, run_id: str) -> tuple[list[dict], PendingRow]:
    """Spawn one child that finishes with ``env``; return traces + parent row."""
    tracer = Tracer(run_id=run_id, output_dir=str(tmp_path))
    trace_path = tracer.path
    child = _ChildSession("coder", "implemented it", env)
    lead = _LeadSession()
    scheduler = Scheduler(
        session_factory=_Factory(child),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
        tracer=tracer,
    )
    scheduler.register_lead(lead)

    async def scenario() -> None:
        aid = await scheduler.spawn(0, "coder", "implement", tool_call_id="tc-1")
        lead.state.pending_events.add(
            PendingRow(
                tool_call_id="tc-1",
                kind=RowKind.CHILD_AGENT,
                order=0,
                ref=aid,
                status=RowStatus.PENDING,
            )
        )
        lead.state.set_phase(SessionPhase.AWAITING_EVENTS)
        await scheduler._tasks[aid]
        resume = scheduler._tasks.get(0)
        if resume is not None:
            await resume

    try:
        asyncio.run(scenario())
        tracer.flush()
    finally:
        tracer.close()

    return _payloads(trace_path, "worktree_changes"), lead.state.pending_events.rows["tc-1"]


ADDED = "print('brand new')\n"
MODIFIED = "print('changed')\n"

THREE_FILE_DIFF = """diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
index 0000000..3e75765
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+print('brand new')
diff --git a/pkg/mod.py b/pkg/mod.py
index 3367afd..b66ba06 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1 +1 @@
-print('old')
+print('changed')
diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
index b023018..0000000
--- a/pkg/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-print('bye')
"""


def test_added_modified_and_deleted_files_are_recorded_with_content_shas(tmp_path):
    env = _Env(THREE_FILE_DIFF, {"pkg/new.py": ADDED, "pkg/mod.py": MODIFIED})

    payloads, row = _run_one_child(tmp_path, env, "worktree-three")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["aid"] == 1
    assert payload["role"] == "coder"
    assert payload["diff_sha"] == _sha(THREE_FILE_DIFF)
    assert payload["diff_chars"] == len(THREE_FILE_DIFF)
    assert payload["truncated_in_result"] is False
    assert payload["files"] == [
        {"path": "pkg/new.py", "op": "added", "content_sha": _sha(ADDED)},
        {"path": "pkg/mod.py", "op": "modified", "content_sha": _sha(MODIFIED)},
        {"path": "pkg/gone.py", "op": "deleted", "content_sha": None},
    ]
    assert row.status is RowStatus.DONE


def _long_diff(file_count: int) -> tuple[str, dict[str, str]]:
    """A diff whose middle is guaranteed to be cut from the parent's copy."""
    blocks: list[str] = []
    files: dict[str, str] = {}
    for index in range(file_count):
        path = f"pkg/file_{index}.py"
        body = "".join(f"+line {index}-{n}\n" for n in range(120))
        blocks.append(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1,120 @@\n" + body
        )
        files[path] = body.replace("+", "")
    return "".join(blocks), files


def test_truncated_delivery_still_records_every_file_and_the_full_diff_length(tmp_path):
    from opencollab.application._scheduler_constants import WORKTREE_DIFF_MAX_CHARS

    diff, files = _long_diff(12)
    assert len(diff) > WORKTREE_DIFF_MAX_CHARS
    env = _Env(diff, files)

    payloads, row = _run_one_child(tmp_path, env, "worktree-truncated")

    # The parent's copy really is the mutilated one — that is what makes the
    # structured record necessary, and it must stay that way.
    assert "chars truncated" in row.result
    assert len(row.result) < len(diff)

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["truncated_in_result"] is True
    assert payload["diff_chars"] == len(diff)
    assert payload["diff_sha"] == _sha(diff)
    assert [entry["path"] for entry in payload["files"]] == list(files)
    assert all(entry["op"] == "added" for entry in payload["files"])
    assert all(
        entry["content_sha"] == _sha(files[entry["path"]]) for entry in payload["files"]
    )


def test_unreadable_file_records_a_null_sha_and_an_error_without_failing_the_run(tmp_path):
    env = _UnreadableEnv(THREE_FILE_DIFF, {})

    payloads, row = _run_one_child(tmp_path, env, "worktree-unreadable")

    assert row.status is RowStatus.DONE
    assert len(payloads) == 1
    entries = {entry["path"]: entry for entry in payloads[0]["files"]}
    assert entries["pkg/new.py"]["content_sha"] is None
    assert entries["pkg/new.py"]["sha_error"] == "RuntimeError"
    assert entries["pkg/mod.py"]["sha_error"] == "RuntimeError"
    # A deleted file is never read, so it carries no read error.
    assert entries["pkg/gone.py"]["content_sha"] is None
    assert "sha_error" not in entries["pkg/gone.py"]


# Verbatim `git diff --binary` output: a binary payload (which git emits with
# no ``---``/``+++`` pair), a path containing a space (git appends a tab), a
# rename, a mode-only change, and a non-ASCII path (git C-quotes it).
REAL_GIT_DIFF = (
    "diff --git a/bin.dat b/bin.dat\n"
    "index 366fd40..7a621bf 100644\n"
    "GIT binary patch\n"
    "literal 9\n"
    "QcmZQzO3KVjEUGjD01IIPx&QzG\n"
    "\n"
    "literal 8\n"
    "PcmZQzOv=nlEUE+m2%rLo\n"
    "\n"
    "diff --git a/has space.txt b/has space.txt\n"
    "new file mode 100644\n"
    "index 0000000..c68065a\n"
    "--- /dev/null\n"
    "+++ b/has space.txt\t\n"
    "@@ -0,0 +1 @@\n"
    "+space file\n"
    "diff --git a/keep.txt b/keep.txt\n"
    "old mode 100644\n"
    "new mode 100755\n"
    "diff --git a/ren_old.txt b/ren_new.txt\n"
    "similarity index 100%\n"
    "rename from ren_old.txt\n"
    "rename to ren_new.txt\n"
    'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n'
    "new file mode 100644\n"
    "index 0000000..4ae8ef0\n"
    "--- /dev/null\n"
    '+++ "b/caf\\303\\251.txt"\n'
    "@@ -0,0 +1 @@\n"
    "+u\n"
)


def test_parser_survives_the_header_shapes_real_git_actually_emits():
    from opencollab.application._scheduler_team import _parse_worktree_diff

    assert _parse_worktree_diff(REAL_GIT_DIFF) == [
        ("bin.dat", "modified"),
        ("has space.txt", "added"),
        ("keep.txt", "modified"),
        # A rename is split into its two endpoints.
        ("ren_old.txt", "deleted"),
        ("ren_new.txt", "added"),
        ("café.txt", "added"),
    ]


def test_parser_returns_nothing_for_input_that_is_not_a_diff():
    from opencollab.application._scheduler_team import _parse_worktree_diff

    assert _parse_worktree_diff("") == []
    assert _parse_worktree_diff("no headers here\n") == []


def test_an_agent_that_changed_nothing_still_gets_a_row(tmp_path):
    """Absence of a row must not be the only signal for "changed nothing".

    An empty diff is a real, reportable outcome — it says this agent produced
    no files. Skipping the record would make that indistinguishable from a
    recorder that broke, the same reason a turn with no compaction still emits
    ``rung="none"``.
    """
    env = _Env("", {})

    payloads, row = _run_one_child(tmp_path, env, "worktree-empty")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["aid"] == 1
    assert payload["role"] == "coder"
    assert payload["files"] == []
    assert payload["diff_chars"] == 0
    assert payload["truncated_in_result"] is False
    assert row.status is RowStatus.DONE

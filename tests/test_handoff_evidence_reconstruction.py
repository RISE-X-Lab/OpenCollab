"""The four steps of a git handoff, rebuilt from trace records and nothing else.

``configs/team.handoff.experiment.yaml`` seats three peers that can hand work to
each other inside one repository: the Analyst assigns with ``message_agent``, the
Coder edits and commits in its own worktree and sends the sha on, the Tester runs
``git checkout <sha>`` in its own worktree and tests what it got, and the Tester
reports back. The claim those runs are meant to support is that the assigned
organization actually carried the work — so each of the four steps has to be
recoverable from what a run leaves on disk, and recoverable *as a field*, not by
reading what a model wrote about itself.

So this drives the real scheduler, the real ``WorktreeEnvironment``, real ``git``,
the real ``message_agent`` tool and the real topology check with one scripted LLM
in place of a provider, and then rebuilds the four steps from the trajectory file
alone. Every assertion below reads a trace field. The one value the script does
not know in advance — the sha the Coder commits — is taken from git's own output
inside the run, exactly as a model would have to take it.

The negative case is the control: the same script with the Tester's checkout
removed. If the same reconstruction accepts both, the records cannot tell a
handoff from two agents working past each other, and the positive result means
nothing.

Nothing here reaches into the seated agents to give them their shell. A
prebuilt roster's peers are declared nodes, and the session factory hands them
the same shell answer it hands agent 0 — if that ever stops being true this
test stops passing, which is the point of running the real ``BashTool`` against
a real worktree instead of a stub.

One thing this test does arrange that a real run would not:
``PYTHONDONTWRITEBYTECODE`` keeps the Tester's own probe from writing a
``__pycache__`` file into the worktree it is measuring.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.trace import Tracer
from opencollab.bootstrap import build_runtime_context, build_scheduler, container
from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
TEAM_FILE = REPO_ROOT / "configs" / "team.handoff.experiment.yaml"

CONFIG = {
    "model": "gpt-4o",
    "provider": "openai",
    "api_key": "test-key",  # pragma: allowlist secret
    "base_url": None,
    "budget": 2_000_000,
}

ANALYST_AID, CODER_AID, TESTER_AID = 0, 1, 2
FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    """A committed repository — the precondition for linked worktrees."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "app.py").write_text(BROKEN, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _acts(tool_calls: list[dict]) -> LLMResponse:
    return LLMResponse(
        tool_calls=tool_calls,
        usage=Usage(input_tokens=10, output_tokens=5),
        finish_reason="tool_calls",
    )


def _says(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=Usage(input_tokens=10, output_tokens=5),
        finish_reason="stop",
    )


def _last_tool_result(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "tool":
            return str(message.get("content") or "")
    raise AssertionError("the script expected a tool result and found none")


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    raise AssertionError("the script expected a teammate message and found none")


def _scripted_llm(*, tester_checks_out: bool):
    """One scripted provider class; each agent gets its own instance.

    The role is read off the system prompt because the composition root builds a
    client per agent and tells it only about the model. Every branch is fixed in
    advance except the sha, which is read out of git's own output — the same
    place the model would have to read it.
    """

    class ScriptedLLM:
        def __init__(self, **_kwargs) -> None:
            self.role = "?"
            self.turn = 0

        def context_window(self) -> int:
            return 200_000

        async def close(self) -> None:
            return None

        async def complete(self, messages, **_kwargs) -> LLMResponse:
            system = messages[0].get("content") or ""
            for role in ("Analyst", "Coder", "Tester"):
                if f"You are the {role}" in str(system):
                    self.role = role.lower()
                    break
            self.turn += 1
            return getattr(self, f"_{self.role}")(messages)

        def _analyst(self, _messages: list[dict]) -> LLMResponse:
            if self.turn == 1:
                return _acts([
                    _call("a1", "message_agent", {
                        "to_aid": CODER_AID,
                        "summary": "fix add()",
                        "content": (
                            "app.py's add() subtracts. Fix it, commit it, and "
                            f"hand the commit to the tester (aid {TESTER_AID})."
                        ),
                    })
                ])
            return _says("handed the work to the coder")

        def _coder(self, messages: list[dict]) -> LLMResponse:
            if self.turn == 1:
                return _acts([
                    _call("c1", "bash", {
                        "command": (
                            f"printf '{FIXED}' > app.py && git add -A && "
                            "git commit -qm 'fix add' && git rev-parse HEAD"
                        )
                    })
                ])
            if self.turn == 2:
                sha = FULL_SHA.search(_last_tool_result(messages))
                assert sha is not None, "the coder's commit produced no sha"
                return _acts([
                    _call("c2", "message_agent", {
                        "to_aid": TESTER_AID,
                        "summary": "committed the fix",
                        "content": (
                            f"Fixed app.py and committed it as {sha.group(0)}. "
                            "Check it out and run the probe."
                        ),
                    })
                ])
            return _says("fix committed and handed to the tester")

        def _tester(self, messages: list[dict]) -> LLMResponse:
            probe = (
                "PYTHONDONTWRITEBYTECODE=1 python3 -c "
                "\"import app; print('add(1,2) =', app.add(1, 2))\""
            )
            if self.turn == 1 and tester_checks_out:
                sha = FULL_SHA.search(_last_user_text(messages))
                assert sha is not None, "the coder's message carried no sha"
                return _acts([
                    _call("t1", "bash", {"command": f"git checkout -q {sha.group(0)}"})
                ])
            if self.turn == (2 if tester_checks_out else 1):
                return _acts([_call("t2", "bash", {"command": probe})])
            if self.turn == (3 if tester_checks_out else 2):
                return _acts([
                    _call("t3", "message_agent", {
                        "to_aid": CODER_AID,
                        "summary": "ran the probe",
                        "content": _last_tool_result(messages),
                    })
                ])
            return _says("reported the probe result to the coder")

    return ScriptedLLM


async def _run_handoff(tmp_path, monkeypatch, *, tester_checks_out: bool) -> list[dict]:
    """Run the whole team once; return every trace record, in order."""
    workspace = _repo(tmp_path / "repo")
    traces = tmp_path / "traces"
    traces.mkdir()
    monkeypatch.setattr(
        container, "LLMClient", _scripted_llm(tester_checks_out=tester_checks_out)
    )
    tracer = Tracer(run_id="handoff", output_dir=str(traces))
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    ctx.tracer = tracer
    scheduler = build_scheduler(
        ctx,
        use_worktrees=True,
        interactive=True,
        auto_save=False,
        enable_hooks=False,
        prebuild_team=True,
        resolved_team_config=load_team_config(path=str(TEAM_FILE)),
    )
    try:
        await scheduler.ensure_team_prebuilt()
        await scheduler.run_turn(
            ANALYST_AID, "app.py's add() is wrong. Get it fixed and verified."
        )
        tracer.flush()
    finally:
        tracer.close()
        await scheduler.cleanup()

    with open(tracer.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --- the reconstruction ------------------------------------------------------
#
# Four readers over the trajectory file. None of them reads a model's prose:
# every value comes out of a recorded field, and the sha that ties two agents
# together is compared field to field.


def _payloads(records: list[dict], step_type: str) -> list[dict]:
    return [record["payload"] for record in records if record["type"] == step_type]


def _messages(records: list[dict], *, sender: str, recipient: str) -> list[dict]:
    return [
        payload
        for payload in _payloads(records, "message_sent")
        if payload["from_role"] == sender and payload["to_role"] == recipient
    ]


def _worktree_rows(records: list[dict], role: str) -> list[dict]:
    return [
        payload
        for payload in _payloads(records, "worktree_changes")
        if payload["role"] == role
    ]


def _commits_made_by(records: list[dict], role: str) -> set[str]:
    """Every commit that role put on top of the base it started from."""
    return {
        commit for row in _worktree_rows(records, role) for commit in row["commits"]
    }


def _bases_adopted_by(records: list[dict], role: str) -> set[str]:
    return {row["diff_base"] for row in _worktree_rows(records, role)}


async def test_every_step_of_the_handoff_is_recoverable_from_recorded_fields(
    tmp_path, monkeypatch
) -> None:
    records = await _run_handoff(tmp_path, monkeypatch, tester_checks_out=True)

    # The organization the run was assigned. Both handoff edges were declared,
    # so an edge that carried traffic below is an edge the config asked for.
    edges = _payloads(records, "assigned.topology_edges")[0]["edges"]
    assert {"from_role": "analyst", "to_role": "coder"} in edges
    assert {"from_role": "coder", "to_role": "tester"} in edges

    # Step 1 — the Analyst assigned the work to the Coder.
    assert len(_messages(records, sender="analyst", recipient="coder")) == 1

    # Step 2 — the Coder changed a file, committed it, and sent that commit on.
    coder_rows = _worktree_rows(records, "coder")
    assert [entry["path"] for entry in coder_rows[0]["files"]] == ["app.py"]
    assert coder_rows[0]["commit_count"] == 1
    assert coder_rows[0]["head_commit"] == coder_rows[0]["commits"][0]
    assert coder_rows[0]["head_commit"] != coder_rows[0]["diff_base"]

    committed = _commits_made_by(records, "coder")
    handoff = _messages(records, sender="coder", recipient="tester")
    assert len(handoff) == 1
    assert set(handoff[0]["commit_refs"]) & committed == committed

    # Step 3 — the Tester measured its own work from the Coder's commit, which
    # it can only have done by checking that commit out.
    assert _bases_adopted_by(records, "tester") & committed == committed

    # Step 4 — the Tester reported back over a declared return edge.
    assert len(_messages(records, sender="tester", recipient="coder")) == 1


async def test_a_tester_that_never_checked_out_reconstructs_as_not_having(
    tmp_path, monkeypatch
) -> None:
    """The control: same run, same records, one step removed.

    Everything else still happens — the Coder still commits, still sends the
    sha, and the Tester still runs a command and answers. Only the checkout is
    gone, and the step-3 reader has to say so on its own.
    """
    records = await _run_handoff(tmp_path, monkeypatch, tester_checks_out=False)

    committed = _commits_made_by(records, "coder")
    assert committed, "the control is only a control if the Coder still committed"
    assert _messages(records, sender="coder", recipient="tester")[0]["commit_refs"]
    assert _messages(records, sender="tester", recipient="coder")

    # The step-3 reader, unchanged.
    assert _bases_adopted_by(records, "tester") & committed == set()
    # And it says the honest thing about where the Tester actually stood: on the
    # commit its worktree was cut from, which is where the Coder started too.
    assert _bases_adopted_by(records, "tester") == _bases_adopted_by(records, "coder")

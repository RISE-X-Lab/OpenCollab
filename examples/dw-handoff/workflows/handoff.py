"""The handoff experiment with the exchange scripted instead of chosen.

This is the code-sequenced twin of ``configs/team.handoff.experiment.yaml``.
Both run the same three roles over the same repository, give them the same
tools, put each one in a git worktree of its own, and make a commit sha the
whole payload of a handoff. Exactly one thing differs, and it is the thing
under study: there, an agent decides whether and when to hand work over; here,
this script decides, and the agents have no say.

The team config says of itself that "scripting the exchange would mean
measuring the script". That is precisely what this file does, on purpose, so
that the two arms differ in who sequences the work and in nothing else. A
difference between the arms that comes from unequal tools, unequal isolation,
or unequal handoff mechanics would be a fact about the two configurations
rather than about the models, so all three are held equal here.

**Why every agent is isolated, including the two that mostly read.** Linked
worktrees share ``.git/objects``, so a sha committed in one is reachable from
the others the moment it exists. Isolation is therefore not a restriction on
what an agent may receive; it is what makes receiving observable. A tester that
shares the coder's directory has the coder's edits whether or not it was told
about them, and "the tester worked from the coder's output" is then not a
question the run can answer. With separate trees the tester holds the coder's
work only if it checked out the sha, which the run records.

**Why the analyst carries the working tools.** The team config gives its
analyst the tools to finish the task alone so that delegation is a choice the
model did not have to make, and so that the arm cannot be weaker than the
single agent it is compared against for a reason readable off the tool bundle.
Neither reason survives being dropped here just because this analyst is
sequenced: the second one is about comparability across arms, so the bundle
matches.

The coordination tools the team roles carry (``message_agent``, ``team_status``,
``ask_user``, ``use_skill``) are scheduler-owned and have no meaning in a
workflow: the script is the only channel between these agents. That absence is
the arm's definition, not a gap in the mirror.
"""

from __future__ import annotations

import json
from typing import Any

from opencollab import workflow
from opencollab.tools import builtin_tools
from opencollab.workflows import WorkflowContext

# One repair round after the first verdict, matching the bounded retry the
# scripted team baselines use. More rounds would change what is being compared.
MAX_REPAIR_ROUNDS = 1

ROLE_TOOLS = {
    # The team's analyst bundle minus the coordination tools, which is the
    # single agent's working set: it could finish the task without anyone.
    "analyst": ("apply_patch", "bash", "file_read", "grep", "run_tests"),
    "coder": ("apply_patch", "bash", "file_read", "grep", "run_tests"),
    # No ``apply_patch``: the team's tester carries ``git_diff`` in its place,
    # so this one does too.
    "tester": ("bash", "file_read", "git_diff", "grep", "run_tests"),
}

HANDOFF_RULES = """\
You are working in a git worktree of your own. Your teammates have their own,
and none of you can see another's files. Your worktree shares .git/objects with
theirs, so any commit you make is reachable from their trees immediately, by
sha. A sha is the only way your work can reach anyone else.
"""

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "files", "implementation_task", "verification_task"],
    "properties": {
        "root_cause": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "implementation_task": {"type": "string"},
        "verification_task": {"type": "string"},
    },
    "additionalProperties": False,
}

# ``commit`` is the handoff. It is required and it is a string because a coder
# that edited but never committed has produced nothing anyone else can read,
# and the run should record that as a coder that answered rather than as a
# script that forgot to ask.
PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["commit", "summary"],
    "properties": {
        "commit": {"type": "string"},
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["adopted_commit", "verdict", "findings"],
    "properties": {
        # What the tester actually checked out, in its own words. The run does
        # not have to trust it: the tester's worktree records the revision its
        # diff was measured against, and the two either agree or they do not.
        "adopted_commit": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "findings": {"type": "string"},
    },
    "additionalProperties": False,
}

ANALYST_PROMPT = """\
{rules}
You are the analyst. Read the repository and work out what is actually broken.

Task: {goal}

Report the root cause, the files involved, what implementing the fix requires,
and what verifying it requires. You have the tools to fix it yourself; whether
you use them is your decision and does not change what is asked of you here.
"""

CODER_PROMPT = """\
{rules}
You are the coder. Implement the fix in your worktree.

Task: {goal}

The analyst's brief:
{brief}

{findings}

When the edit is done, commit it in your worktree and report the commit sha.
Your teammates cannot see your files; the sha is what reaches them.
"""

TESTER_PROMPT = """\
{rules}
You are the tester. The coder committed {commit} in its worktree.

Task: {goal}

The analyst's brief:
{brief}

The coder reported:
{summary}

That sha is reachable from your worktree. Check it out, then verify the fix
against the task. Report which sha you actually adopted, your verdict, and what
you found.
"""


def _tools(role: str) -> list[Any]:
    return list(builtin_tools(*ROLE_TOOLS[role]))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


@workflow(
    name="dw-handoff",
    description="The handoff experiment with the exchange sequenced by a script.",
    phases=["analyze", "implement", "verify"],
)
async def dw_handoff(ctx: WorkflowContext, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal"'}

    await ctx.phase("analyze")
    brief = await ctx.agent(
        ANALYST_PROMPT.format(rules=HANDOFF_RULES, goal=goal),
        schema=BRIEF_SCHEMA,
        label="analyst",
        tools=_tools("analyst"),
        isolation=True,
    )
    if not isinstance(brief, dict):
        return {"status": "error", "error": "analyst produced no brief"}

    findings = "No tester findings yet."
    rounds: list[dict[str, Any]] = []
    for round_no in range(1, MAX_REPAIR_ROUNDS + 2):
        await ctx.phase("implement" if round_no == 1 else "verify")
        patch = await ctx.agent(
            CODER_PROMPT.format(
                rules=HANDOFF_RULES,
                goal=goal,
                brief=_dump(brief),
                findings=findings,
            ),
            schema=PATCH_SCHEMA,
            label=f"coder:{round_no}",
            tools=_tools("coder"),
            isolation=True,
        )
        if not isinstance(patch, dict):
            rounds.append({"round": round_no, "status": "coder produced no commit"})
            break

        await ctx.phase("verify")
        verdict = await ctx.agent(
            TESTER_PROMPT.format(
                rules=HANDOFF_RULES,
                goal=goal,
                brief=_dump(brief),
                commit=patch["commit"],
                summary=patch["summary"],
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:{round_no}",
            tools=_tools("tester"),
            isolation=True,
        )
        if not isinstance(verdict, dict):
            verdict = {
                "adopted_commit": "",
                "verdict": "FAIL",
                "findings": "Tester produced no verdict.",
            }
        rounds.append(
            {
                "round": round_no,
                "offered_commit": patch["commit"],
                "adopted_commit": verdict["adopted_commit"],
                # The handoff either happened or it did not, and the script
                # cannot make it happen: it hands over a sha, and the tester
                # chooses whether to work from it. This is the one thing the
                # scripted arm and the model-directed arm measure the same way.
                "handoff_taken": verdict["adopted_commit"] == patch["commit"],
                "verdict": verdict["verdict"],
            }
        )
        if verdict["verdict"] == "PASS":
            break
        findings = f"The tester reported: {verdict['findings']}"

    return {
        "status": "ok",
        "rounds": rounds,
        "tokens_spent": ctx.tokens_spent(),
    }

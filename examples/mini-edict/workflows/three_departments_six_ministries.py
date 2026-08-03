"""A compact Three Departments and Six Ministries collaboration workflow."""

from __future__ import annotations

from typing import Any

from opencollab import workflow
from opencollab.workflows import WorkflowContext

ZHONGSHU = "Zhongshu Secretariat"
DEPARTMENT_CARDS = {
    ZHONGSHU: "draft and revise an actionable policy proposal",
    "Menxia Chancellery": "veto weak work through feasibility, completeness, risk, and resource review",
    "Shangshu Department": "route approved policy to specialists and assemble an evidence-bearing memorial",
}
MINISTRY_CARDS = {
    "Personnel": "people, ownership, incentives, and agent responsibilities",
    "Revenue": "data, costs, resources, and measurable returns",
    "Rites": "documentation, stakeholders, communication, and institutional fit",
    "War": "engineering, competition, resilience, and operational risk",
    "Justice": "testing, rules, accountability, compliance, and failure handling",
    "Works": "infrastructure, deployment, tools, and delivery",
}
ROLE_CARDS = DEPARTMENT_CARDS | {f"Ministry of {name}": card for name, card in MINISTRY_CARDS.items()}
MAX_REVISIONS = 2

def _record(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


REVIEW_SCHEMA = _record(
    verdict={"type": "string", "enum": ["approve", "revise"]},
    findings={"type": "array", "items": {"type": "string"}},
)
ASSIGNMENT_SCHEMA = _record(
    ministry={"type": "string", "enum": list(MINISTRY_CARDS)},
    task={"type": "string"},
    acceptance={"type": "string"},
)
DISPATCH_SCHEMA = _record(
    summary={"type": "string"},
    assignments={"type": "array", "minItems": 1, "maxItems": len(MINISTRY_CARDS), "items": ASSIGNMENT_SCHEMA},
)


def _blocked(stage: str, **evidence: Any) -> dict[str, Any]:
    return {"status": "blocked", "stage": stage, **evidence}


async def _ask(
    ctx: WorkflowContext,
    role: str,
    material: str,
    *,
    schema: dict[str, Any] | None = None,
) -> Any:
    return await ctx.agent(
        f"You are the {role}. Your mandate is to {ROLE_CARDS[role]}.\n\n{material}",
        label=role,
        budget=6_000,
        schema=schema,
    )


async def _review(ctx: WorkflowContext, subject: str, material: str) -> Any:
    return await _ask(
        ctx,
        "Menxia Chancellery",
        f"Review the {subject} across feasibility, completeness, risk, and resources. Approve only when every material "
        f"issue is addressed. Otherwise request concrete revisions.\n\n{material}",
        schema=REVIEW_SCHEMA,
    )


@workflow(
    name="three-departments-six-ministries",
    description="Govern a task through veto, bounded re-planning, specialist routing, and final audit.",
    phases=("draft", "review", "dispatch", "execution", "synthesis", "audit"),
)
async def three_departments_six_ministries(ctx: WorkflowContext, inputs: dict[str, Any]) -> dict[str, Any]:
    task = inputs.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    task = task.strip()

    await ctx.phase("Zhongshu drafts")
    proposal = await _ask(
        ctx,
        ZHONGSHU,
        f"Draft a proposal with deliverables, constraints, dependencies, risks, and acceptance criteria.\n\n{task}",
    )
    if proposal is None:
        return _blocked("draft")

    await ctx.phase("Menxia reviews")
    reviews: list[dict[str, Any]] = []
    for attempt in range(MAX_REVISIONS + 1):
        review = await _review(ctx, "proposal", f"Task:\n{task}\n\nProposal:\n{proposal}")
        if not isinstance(review, dict):
            return _blocked("review")
        reviews.append(review)
        if review.get("verdict") == "approve":
            break
        if attempt == MAX_REVISIONS:
            return _blocked("review", proposal=proposal, reviews=reviews)
        await ctx.log(f"Menxia vetoed proposal round {attempt + 1}")
        proposal = await _ask(
            ctx,
            ZHONGSHU,
            "Revise every finding while preserving sound work.\n\n"
            f"Task:\n{task}\n\nProposal:\n{proposal}\n\nReview:\n{review}",
        )
        if proposal is None:
            return _blocked("revision")

    await ctx.phase("Shangshu dispatches")
    dispatch = await _ask(
        ctx,
        "Shangshu Department",
        f"Choose only materially relevant ministries. Give each a distinct task and acceptance criterion.\n\n"
        f"Task:\n{task}\n\nApproved proposal:\n{proposal}\n\nMinistry mandates:\n{MINISTRY_CARDS}",
        schema=DISPATCH_SCHEMA,
    )
    assignments = dispatch.get("assignments") if isinstance(dispatch, dict) else None
    if not isinstance(assignments, list) or not assignments:
        return _blocked("dispatch")
    selected = {item.get("ministry"): item for item in assignments if isinstance(item, dict)}
    if len(selected) != len(assignments) or any(name not in MINISTRY_CARDS for name in selected):
        return _blocked("dispatch")

    await ctx.phase("Selected ministries execute in parallel")
    reports = await ctx.parallel(
        [
            lambda name=name, assignment=assignment: _ask(
                ctx,
                f"Ministry of {name}",
                f"Complete the assignment and return deliverables, evidence, limitations, and risks.\n\n{assignment}",
            )
            for name, assignment in selected.items()
        ]
    )
    ministry_reports = dict(zip(selected, reports, strict=True))
    missing = [name for name, report in ministry_reports.items() if report is None]
    if missing:
        return _blocked("execution", missing=missing)

    await ctx.phase("Shangshu synthesizes")
    final = await _ask(
        ctx,
        "Shangshu Department",
        "Separate delivered work, contributions, evidence, unfinished items, and risks. "
        "Role narration is not evidence.\n\n"
        f"Task:\n{task}\n\nDispatch:\n{dispatch}\n\nReports:\n{ministry_reports}",
    )
    if final is None:
        return _blocked("synthesis")

    await ctx.phase("Menxia audits the memorial")
    audit = await _review(
        ctx,
        "final memorial",
        f"Task:\n{task}\n\nApproved proposal:\n{proposal}\n\nDispatch:\n{dispatch}\n\n"
        f"Ministry reports:\n{ministry_reports}\n\nMemorial:\n{final}",
    )
    if not isinstance(audit, dict) or audit.get("verdict") != "approve":
        return _blocked("audit", final=final, audit=audit)
    return {
        "status": "completed",
        "proposal": proposal,
        "reviews": reviews,
        "dispatch": dispatch,
        "ministries": ministry_reports,
        "final": final,
        "audit": audit,
        "tokens_spent": ctx.tokens_spent(),
    }

"""Reconstruct a hidden program from its input/output behaviour.

The workflow runs the active-exploration protocol of the FromIO paper
(arXiv 2605.15334, Section 4.6, ``run_active_dio_agent.py``): the workflow,
not the agent, decides when the hidden program is queried and how many inputs
each batch holds (``initial_batch``, then ``batch_step`` more per batch); a
batch is requested only after the current candidate passes every observed
example (a "promotion"); ``early_stop`` consecutive promotions end the task;
the agent proposes the inputs and the workflow validates them (exact count,
no repeats, accepted by the oracle) and retries a rejected proposal. One
coding-agent session replaces one DIO-Agent evolution iteration, and the
final program is the archived candidate with the best stage score on the
final example set.

Four optional disciplines borrow rea's verification rules: part of every
batch may come from a hypothesis-independent input generator
(``random_share``); a promotion may be withheld until the observed inputs
cover the structural case categories (``coverage_gate``); a pass produced by
memorised inputs or by an over-complex program may need more evidence
(``memorisation_gate``, ``complexity_gate``); proposed inputs outside the
task's numeric range may be rejected (``domain_check``).

The benchmark binding is a :class:`ReconstructionTask`: how to query the
hidden program, how to run a candidate, which tools the coding agent gets.
Workflow arguments must stay JSON-serializable, so ``args["task"]`` is a
handle: either the token returned by :func:`register_task` (SDK callers in
the same process) or a ``module:callable`` factory that builds the task from
``args["task_args"]`` (CLI callers).
"""

from __future__ import annotations

import importlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from opencollab.workflows import workflow

from ._reconstruct_coverage import describe
from ._reconstruct_coverage import missing as missing_categories
from ._reconstruct_verification import (
    COMPLEXITY_THRESHOLD,
    MEMORISED_INPUTS_THRESHOLD,
    complexity_penalty,
    in_domain,
    jsonable,
    memorised_inputs,
    outputs_match,
    stage_score,
)

# Defaults of the paper's run_active_dio_agent.py.
INITIAL_BATCH = 2
BATCH_STEP = 2
MAX_ITERATIONS = 50
PROMOTION_EARLY_STOP = 5
PROPOSE_MAX_ATTEMPTS = 3
EXAMPLES_SHOWN_TO_PROPOSER = 12
# How many batches the coverage gate may keep blocking on the same missing
# categories before it stops counting them (they may be unreachable).
COVERAGE_GATE_PATIENCE = 3
# A memorising candidate gets this many rewrite rounds on the same evidence
# before the next batch is granted anyway (more evidence may help).
MEMORISATION_PATIENCE = 2
# A high-complexity pass is granted its batch but not counted, this many times.
COMPLEXITY_PATIENCE = 3
# Below this many remaining budget tokens another coder round cannot do useful work.
MIN_TOKENS_FOR_ANOTHER_ROUND = 30_000
CANDIDATES_DIR = ".oc_candidates"

INPUTS_SCHEMA = {
    "type": "object",
    "properties": {"inputs": {"type": "array", "items": {}}},
    "required": ["inputs"],
}

PROPOSE_PROMPT = """\
You are probing a hidden program to discover its rule from input/output
behaviour. You cannot see its code.

Input type: {input_type}. {constraints}

Known examples so far (latest {shown} shown):
{examples}

All inputs already used, which you must NOT repeat:
{used}

This round's exact budget: {n} NEW inputs.
{coverage_hint}
Rules:
- Propose exactly {n} inputs, each valid for the input type above.
- None may repeat a known input, and they must not repeat each other.
- Choose diverse, informative inputs that best separate competing hypotheses
  about the rule (edge cases, boundaries, structure-revealing cases).
- The hidden program's outputs for them will be returned to you. Once your
  candidate program passes all known examples you will get another, larger
  batch, so do not try to cover everything now.

Finish by calling structured_output with {{"inputs": [...]}}.
"""

CODER_PROMPT = """\
You are reverse-engineering a hidden program from input/output examples only.
You cannot see its code and you cannot run it.

Input type: {input_type}. {constraints}

Known examples (all {count}):
{examples}

{feedback}

Write or revise `{candidate}` in the workspace defining
`{function_name}(input_data)` so that it returns exactly what the hidden
program returns for ALL valid inputs, not only these examples. Use only the
Python standard library (no numpy or other third-party packages).

Prefer the simplest hypothesis consistent with all examples: constants, then
arithmetic, then conditionals, then loops. Never hard-code the examples.
Run `check_candidate` before finishing. If all current examples pass, the
runner will query {next_batch} more examples from the hidden program and give
you another turn.

Reply with one sentence stating the rule you implemented.
"""


@dataclass
class ReconstructionTask:
    """Everything the protocol needs to know about one hidden program.

    ``oracle`` returns the hidden program's output for an input (raise for an
    invalid input). ``evaluate`` runs a candidate file on inputs and returns
    one ``{"ok": True, "output": ...}`` or ``{"ok": False, "error": ...}``
    per input; it should execute the candidate where the agent's own code
    runs, never in the harness process. ``tools`` are handed to every coding
    session. ``random_inputs(k, exclude)`` draws fresh inputs from a source
    independent of the agent (the benchmark's generator); it is required
    when ``random_share`` is positive. ``on_example`` is told about every
    observed example and its source (``agent`` or ``random``). ``kind``,
    ``lo``, ``hi`` and ``is_float`` describe the input for the coverage gate
    and the domain check (kinds: list, two_lists, int, pair, string, points).
    """

    input_type: str
    input_constraints: str
    function_name: str
    workspace: Path
    oracle: Callable[[Any], Any]
    evaluate: Callable[[Path, list[Any]], Awaitable[list[dict[str, Any]]]]
    tools: Sequence[Any] = ()
    random_inputs: Callable[[int, set[str]], list[Any]] | None = None
    outputs_match: Callable[[Any, Any], bool] = outputs_match
    on_example: Callable[[Any, Any, str], None] | None = None
    candidate_file: str = "candidate.py"
    kind: str = "other"
    lo: float | None = None
    hi: float | None = None
    is_float: bool = False


_REGISTRY: dict[str, ReconstructionTask] = {}
_TOKEN_PREFIX = "reconstruct-task:"


def register_task(task: ReconstructionTask) -> str:
    """Register a task built in this process; the returned token goes into ``args["task"]``.

    Workflow arguments are recorded with the run's artifacts and must be
    JSON-serializable, so live objects (oracle functions, Docker-backed
    evaluators, tools) travel through this registry instead. A token is
    consumed by the workflow that resolves it.
    """
    if not isinstance(task, ReconstructionTask):
        raise TypeError("register_task expects a ReconstructionTask")
    token = _TOKEN_PREFIX + uuid.uuid4().hex
    _REGISTRY[token] = task
    return token


def resolve_task(args: dict[str, Any]) -> ReconstructionTask:
    """``args["task"]`` is a registered token, a ReconstructionTask, or a ``module:callable`` factory."""
    spec = args.get("task")
    if isinstance(spec, ReconstructionTask):
        return spec
    if isinstance(spec, str) and spec.startswith(_TOKEN_PREFIX):
        try:
            return _REGISTRY.pop(spec)
        except KeyError:
            raise ValueError(f"unknown or already consumed reconstruct task token {spec!r}") from None
    if isinstance(spec, str) and ":" in spec:
        module_name, _, attr = spec.partition(":")
        factory = getattr(importlib.import_module(module_name), attr)
        task = factory(**(args.get("task_args") or {}))
        if isinstance(task, ReconstructionTask):
            return task
        raise TypeError(f"{spec} returned {type(task).__name__}, expected ReconstructionTask")
    raise ValueError('reconstruct needs args["task"]: a register_task() token or a "module:callable" factory')


def _positive_int(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _share(args: dict[str, Any]) -> float:
    value = args.get("random_share", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError("random_share must be a number between 0 and 1")
    return float(value)


def _fmt(examples: list[tuple[Any, Any]]) -> str:
    return "\n".join(f"  {i!r} -> {o!r}" for i, o in examples) or "  (none)"


def _jsonable_ok(value: Any) -> bool:
    try:
        jsonable(value)
        return True
    except (TypeError, ValueError):
        return False


@workflow(
    name="reconstruct",
    description="Reconstruct a hidden program from input/output observations with batched active exploration",
    phases=["probe", "code", "select"],
)
async def reconstruct(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run the active-exploration protocol for one hidden program (see the module docstring)."""
    task = resolve_task(args)
    initial_batch = _positive_int(args, "initial_batch", INITIAL_BATCH)
    batch_step = _positive_int(args, "batch_step", BATCH_STEP)
    max_iterations = _positive_int(args, "max_iterations", MAX_ITERATIONS)
    early_stop = _positive_int(args, "early_stop", PROMOTION_EARLY_STOP)
    propose_attempts = _positive_int(args, "propose_attempts", PROPOSE_MAX_ATTEMPTS)
    examples_shown = _positive_int(args, "examples_shown", EXAMPLES_SHOWN_TO_PROPOSER)
    random_share = _share(args)
    coverage_gate = bool(args.get("coverage_gate", False))
    memorisation_gate = bool(args.get("memorisation_gate", False))
    complexity_gate = bool(args.get("complexity_gate", False))
    domain_check = bool(args.get("domain_check", False))
    if random_share > 0 and task.random_inputs is None:
        raise ValueError("random_share > 0 needs ReconstructionTask.random_inputs")

    workspace = Path(task.workspace)
    program = workspace / task.candidate_file
    candidates_dir = workspace / CANDIDATES_DIR
    candidates_dir.mkdir(parents=True, exist_ok=True)
    examples: list[tuple[Any, Any]] = []
    sources: list[str] = []
    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    gate_misses: dict[frozenset[str], int] = {}
    memorisation_rounds = 0
    complexity_rounds = 0
    archived: list[Path] = []  # this run's candidates, in iteration order
    stop_reason: str | None = None

    async def call_agent(**kwargs: Any) -> Any:
        # Budget exhaustion surfaces as an exception before the call; any
        # other session failure returns None (OpenCollab's contract).
        nonlocal stop_reason
        try:
            return await ctx.agent(**kwargs)
        except Exception as exc:
            if "budget" in type(exc).__name__.lower():
                stop_reason = "budget_exhausted"
                return None
            raise

    def uncovered() -> set[str]:
        if not coverage_gate:
            return set()
        return missing_categories([i for i, _ in examples], task.kind, task.lo, task.hi, task.is_float)

    def accept(batch: list[tuple[Any, Any]], source: str) -> None:
        for inp, out in batch:
            seen.add(repr(inp))
            examples.append((inp, out))
            sources.append(source)
            if task.on_example is not None:
                task.on_example(inp, out, source)

    async def failures(path: Path) -> list[str]:
        if not path.is_file():
            return [f"{task.candidate_file} is missing"]
        results = await task.evaluate(path, [i for i, _ in examples])
        out = []
        for (inp, expected), res in zip(examples, results):
            if not res.get("ok"):
                out.append(f"{inp!r}: expected {expected!r}, got {res.get('error')}")
            elif not task.outputs_match(res.get("output"), jsonable(expected)):
                out.append(f"{inp!r}: expected {expected!r}, got {res.get('output')!r}")
        return out

    async def request_examples(n: int, round_no: int) -> bool:
        """Fill a batch of n new inputs: a random share first, the rest proposed by the agent."""
        n_random = int(round(n * random_share)) if random_share > 0 else 0
        if n_random and task.random_inputs is not None:
            batch = []
            for inp in task.random_inputs(n_random, set(seen)):
                try:
                    batch.append((inp, task.oracle(inp)))
                except Exception:  # noqa: BLE001 - the generator may produce inputs the oracle rejects
                    continue
            accept(batch, "random")
            events.append({"propose": round_no, "random": len(batch)})
            n -= len(batch)
        if n <= 0:
            return True
        miss = uncovered()
        hint = ""
        if miss:
            hint = (
                "The known inputs do not yet cover these case types; include at least one input for each:\n  "
                + describe(miss)
                + "\n"
            )
        if domain_check and task.lo is not None and task.hi is not None:
            hint += f"Valid numeric values are in the range [{task.lo}, {task.hi}]; inputs outside it are rejected.\n"
        base_prompt = PROPOSE_PROMPT.format(
            input_type=task.input_type,
            constraints=task.input_constraints,
            shown=examples_shown,
            examples=_fmt(examples[-examples_shown:]),
            used="\n".join(f"  {i!r}" for i, _ in examples) or "  (none)",
            n=n,
            coverage_hint=hint,
        )
        rejection = ""
        for attempt in range(1, propose_attempts + 1):
            prompt = (f"Your previous proposal was rejected: {rejection}\n\n" if rejection else "") + base_prompt
            reply = await call_agent(
                prompt=prompt, schema=INPUTS_SCHEMA, label=f"propose:{round_no}.{attempt}", tools=[]
            )
            if stop_reason:
                return False
            proposed = reply.get("inputs") if isinstance(reply, dict) else None
            if not isinstance(proposed, list) or len(proposed) != n:
                got = len(proposed) if isinstance(proposed, list) else "no list"
                rejection = f"it had {got} inputs, exactly {n} are required."
                events.append({"propose": round_no, "attempt": attempt, "rejected": rejection})
                continue
            keys = [repr(jsonable(p)) if _jsonable_ok(p) else None for p in proposed]
            dups = sorted({k for k in keys if k is not None and (k in seen or keys.count(k) > 1)})
            if None in keys or dups:
                rejection = (
                    ("these inputs repeat known or proposed ones: " + ", ".join(dups[:10]))
                    if dups
                    else "some inputs are not valid JSON values."
                )
                events.append({"propose": round_no, "attempt": attempt, "rejected": rejection})
                continue
            if domain_check:
                bad = [(p, in_domain(jsonable(p), task.kind, task.lo, task.hi)) for p in proposed]
                bad = [(p, v) for p, v in bad if v]
                if bad:
                    rejection = f"values outside the valid range [{task.lo}, {task.hi}]: " + "; ".join(
                        f"{p!r} has {v[:3]}" for p, v in bad[:4]
                    )
                    events.append({"propose": round_no, "attempt": attempt, "rejected": rejection})
                    continue
            batch: list[tuple[Any, Any]] | None = []
            for inp in proposed:
                inp = jsonable(inp)
                try:
                    batch.append((inp, task.oracle(inp)))
                except Exception as exc:  # noqa: BLE001 - the paper retries the whole batch too
                    rejection = f"the hidden program raised {type(exc).__name__} on {inp!r}; propose only valid inputs."
                    events.append({"propose": round_no, "attempt": attempt, "rejected": rejection})
                    batch = None
                    break
            if batch is None:
                continue
            accept(batch, "agent")
            events.append({"propose": round_no, "attempt": attempt, "accepted": n})
            return True
        return False

    await ctx.phase("probe")
    if not await request_examples(initial_batch, 0):
        stop_reason = stop_reason or "proposer_failed"
    next_batch = initial_batch + batch_step
    consecutive = 0
    feedback = "No candidate yet."
    iteration = 0
    while stop_reason is None and iteration < max_iterations:
        iteration += 1
        await ctx.phase("code")
        prompt = CODER_PROMPT.format(
            input_type=task.input_type,
            constraints=task.input_constraints,
            count=len(examples),
            examples=_fmt(examples),
            feedback=feedback,
            candidate=task.candidate_file,
            function_name=task.function_name,
            next_batch=next_batch,
        )
        await call_agent(prompt=prompt, label=f"code:{iteration}", tools=list(task.tools))
        if stop_reason:
            break
        fails = await failures(program)
        passed = len(examples) - len(fails)
        score = None
        source = ""
        if program.is_file():
            archived.append(candidates_dir / f"iter_{iteration:03d}.py")
            shutil.copy(program, archived[-1])
            source = program.read_text(encoding="utf-8")
            score = round(stage_score(source, passed / len(examples) if examples else 0.0, examples), 4)
        promoted = not fails
        miss = uncovered() if promoted else set()
        memorised = memorised_inputs(source, [i for i, _ in examples]) if (promoted and memorisation_gate) else 0
        complexity = round(complexity_penalty(source), 3) if (promoted and complexity_gate) else None
        events.append(
            {
                "iteration": iteration,
                "known": len(examples),
                "passed": passed,
                "score": score,
                "promoted": promoted,
                "uncovered": sorted(miss),
                "memorised": memorised,
                "complexity": complexity,
            }
        )
        if ctx.tokens_remaining() < MIN_TOKENS_FOR_ANOTHER_ROUND:
            # OpenCollab reports exhaustion as a session that ends before its
            # first model call rather than as an exception; without this the
            # loop would spin through the remaining iterations doing nothing.
            # The check runs after the candidate is evaluated and archived so
            # the last program the agent wrote still competes in selection.
            stop_reason = "budget_exhausted"
            break
        if not promoted:
            consecutive = 0
            feedback = "Your current candidate fails these known examples:\n" + "\n".join(fails[:10])
            continue
        if memorised >= MEMORISED_INPUTS_THRESHOLD:
            # A pass produced by embedding the known inputs is not evidence of
            # the rule. No promotion; rewrite on the same evidence first.
            consecutive = 0
            memorisation_rounds += 1
            feedback = (
                f"Your candidate passes the known examples only by embedding {memorised} of them "
                "verbatim (lookup table or per-input special cases). That is not accepted: "
                "implement the general rule that produces these outputs, with no hard-coded inputs."
            )
            if memorisation_rounds <= MEMORISATION_PATIENCE:
                continue  # same evidence, try again
            memorisation_rounds = 0  # fall through: grant a batch, maybe more evidence helps
        if complexity is not None and complexity >= COMPLEXITY_THRESHOLD:
            complexity_rounds += 1
            feedback = (
                "Your current candidate passes all known examples but is far more complex than this "
                "kind of rule needs; prefer the simplest program consistent with the evidence. "
                "New examples follow."
            )
            if complexity_rounds <= COMPLEXITY_PATIENCE:
                consecutive = 0
                if not await request_examples(next_batch, iteration):
                    stop_reason = stop_reason or "proposer_failed"
                    break
                next_batch += batch_step
                new_fails = await failures(program)
                if new_fails:
                    feedback += "\nYour candidate fails these new examples:\n" + "\n".join(new_fails[:10])
                continue
        if miss:
            # The obligation stays open: the batch is still granted (the
            # protocol's timing is unchanged) but it does not count as a
            # promotion until the required case kinds are covered.
            gate_misses[frozenset(miss)] = gate_misses.get(frozenset(miss), 0) + 1
            if gate_misses[frozenset(miss)] <= COVERAGE_GATE_PATIENCE:
                consecutive = 0
            else:
                consecutive += 1  # unreachable categories: stop blocking
        else:
            consecutive += 1
        if consecutive >= early_stop:
            stop_reason = "early_stop"
            break
        if not await request_examples(next_batch, iteration):
            stop_reason = stop_reason or "proposer_failed"
            break
        next_batch += batch_step
        new_fails = await failures(program)
        feedback = (
            "Your current candidate fails these known examples:\n" + "\n".join(new_fails[:10])
            if new_fails
            else "Your current candidate passes all known examples."
        )
    if stop_reason is None:
        stop_reason = "max_iterations"

    # Final program = the candidate archived by this run with the best stage
    # score on the full observed-example set (later candidates win ties).
    # Only this run's files are considered: a reused workspace may still hold
    # iter_*.py files from an earlier run, and those are not evidence here.
    await ctx.phase("select")
    best_path: Path | None = None
    best_score = -1.0
    for path in archived:
        fails = await failures(path) if examples else []
        acc = (len(examples) - len(fails)) / len(examples) if examples else 0.0
        score = stage_score(path.read_text(encoding="utf-8"), acc, examples)
        if score >= best_score:
            best_path, best_score = path, score
    if best_path is not None:
        shutil.copy(best_path, program)
    return {
        "stop_reason": stop_reason,
        "iterations": iteration,
        "promotions": sum(1 for e in events if e.get("promoted")),
        "known_examples": len(examples),
        "random_inputs": sum(1 for s in sources if s == "random"),
        "coverage_uncovered_at_end": sorted(uncovered()),
        "final_program": best_path.name if best_path else None,
        "final_score": round(best_score, 4) if best_path else None,
        "tokens_spent": ctx.tokens_spent(),
        "events": events,
        "examples": [[inp, out, src] for (inp, out), src in zip(examples, sources)],
        "candidate_exists": program.is_file(),
    }

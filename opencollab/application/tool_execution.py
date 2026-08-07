from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import Any, Awaitable, Callable

from opencollab.application.async_timeout import (
    CallerTimeoutError as CallerTimeoutError,
)
from opencollab.application.async_timeout import (
    abandon_on_timeout as abandon_on_timeout,
)
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    AskUserPort,
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SafetyPolicyPort,
    TracePort,
)
from opencollab.application.schema_validate import validate
from opencollab.application.tool_execution_runtime import (
    DEFAULT_TOOL_CANCELLATION_CLEANUP_TIMEOUT,
    DEFAULT_TOOL_CANCELLATION_FORCE_TIMEOUT,
    DEFAULT_TOOL_ENVIRONMENT_ABORT_TIMEOUT,
    ToolExecutionRuntimeMixin,
)
from opencollab.application.tool_execution_runtime import (
    DeferredCall as DeferredCall,
)
from opencollab.application.tool_execution_runtime import (
    ToolRuntime as ToolRuntime,
)
from opencollab.domain.session import SessionState
from opencollab.domain.tools import MAX_CALL_HASH_WINDOW, LoopDetection, ToolProcessingResult

logger = logging.getLogger(__name__)


# Loop detection (ref: opencode doom_loop detection — 3 identical calls)
MAX_SIMILAR_CALLS = 3
MAX_TOOL_CALLS_PER_BATCH = 32


def _require_positive_finite_timeout(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return timeout

# Read-only, range-parameterized tools whose loop key is the FILE PATH alone, not
# the full args. A model thrashing on one file re-reads it with SHIFTING line
# ranges (sympy-11400 read ccode.py ~135 times), so each exact-arg hash is unique
# and the MAX_SIMILAR_CALLS counter never trips. Collapsing these tools to a
# path-only hash makes the re-reads collide so the loop is caught — at the more
# lenient MAX_SAME_FILE_READS, since a file is legitimately re-read a handful of
# times during distill-as-you-read but dozens of times is a stall.
_PATH_NORMALIZED_TOOLS = frozenset({"file_read"})
MAX_SAME_FILE_READS = 8

# Read-only vs edit tools, for the reads-without-write steering signal. Reads
# accumulate ``reads_since_last_edit``; a successful write zeroes it. bash is
# excluded (it can read OR write — counting it would misfire both ways).
_READ_TOOLS = frozenset({"file_read", "grep"})
_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
# A tool result is an error (not a real edit/read) when it starts with one of
# these prefixes — see execute_tool's PermissionError/Exception mapping and the
# tools' own "Error: ..." returns.
_TOOL_ERROR_PREFIXES = ("Error", "Tool execution error", "Permission denied")
TERMINAL_CAPTURE_SKIP_MESSAGE = (
    "Skipped: terminal structured output accepted earlier in this batch."
)

# Information-gain sensor (STEP 1). Each EXECUTED tool result is classified as
# informative vs low-yield so later brakes can key on information GAIN, not raw
# tokens. Low-yield = an exact duplicate (same result CONTENT hash OR same
# path-normalized (tool, args) call hash seen before — novelty is the PRIMARY
# signal), an empty/zero-byte read, or a "No matches found"-class result. Keying
# on the content/call HASH (not the literal "No matches" string) means a model
# re-reading a known file to dodge a string match still scores zero gain. Novelty
# itself is decided by SessionState (it owns the seen-hash memory); this module
# only computes the two cheap, stateless inputs below. STEP 1 adds NO braking —
# behavior gating lives in a later step; here the counters are observational.
#
# "No matches"-class markers, matched case-insensitively at the start of the
# stripped output. A SECONDARY refinement catching a FIRST-occurrence no-result
# that would otherwise look novel; the grep tool emits
# "No matches found for pattern: ...".
_NO_MATCH_MARKERS = (
    "no matches found",
    "no files found",
    "no results found",
)


def _result_content_hash(output: str) -> str:
    """Stable hash of a tool result's content, for novelty detection."""
    return hashlib.md5((output or "").encode()).hexdigest()


# Evidence ledger (STEP 2). The harness records one compact card per executed
# scout tool call, built purely from the (tool, args, output) envelope. ``target``
# is the thing the call examined (a file path, a grep pattern, a bash command);
# ``snippet`` is a short, single-lined slice of the result so the planner /
# dead-scout synthesizer sees WHAT was found without re-reading the raw transcript.
_LEDGER_SNIPPET_CHARS = 240


def _card_target(tool_name: str, args: dict) -> str:
    """The salient subject of a tool call for the evidence ledger, off the args
    envelope: a read's path, a grep's pattern, a bash command, else a compact
    args dump. Never raises on odd args (defensive str())."""
    args = args or {}
    if tool_name == "grep":
        target = args.get("pattern") or args.get("query") or ""
        path = args.get("path") or args.get("dir")
        return f"{target} in {path}" if path else str(target)
    if tool_name in _READ_TOOLS:
        return str(args.get("path") or args.get("file") or "")
    if tool_name == "bash":
        return str(args.get("command") or "")[:_LEDGER_SNIPPET_CHARS]
    path = args.get("path")
    return str(path) if path else json.dumps(args, default=str, sort_keys=True)[:_LEDGER_SNIPPET_CHARS]


def _card_snippet(output: str) -> str:
    """A short, whitespace-collapsed slice of a tool result for the ledger card."""
    norm = " ".join((output or "").split())
    return norm[:_LEDGER_SNIPPET_CHARS]


def _intrinsic_low_yield(tool_name: str, output: str) -> bool:
    """Low-yield regardless of novelty: an empty read or a 'No matches'-class
    result. The empty check is restricted to read-class tools — an empty
    write/bash result is not inherently low-yield."""
    norm = (output or "").strip()
    if any(norm.lower().startswith(marker) for marker in _NO_MATCH_MARKERS):
        return True
    if tool_name in _READ_TOOLS:
        if not norm:
            return True
        # file_read of an empty / zero-line file returns only its header line.
        if "(0 lines total" in norm:
            return True
    return False


def _bash_likely_mutates(args: dict) -> bool:
    """Heuristic: does this bash command write to a file on disk? Narrow allow-list so
    repros/reads do not falsely reset the reads-without-write counter.

    The coder lands real source edits via bash (``python -c`` read-modify-write,
    ``sed -i``), which never trips the ``_WRITE_TOOLS`` reset, so the
    reads-without-write counter climbs forever and the hard "STOP reading" nudge
    mis-fires at a model already writing. Detecting these mutating shapes lets the
    existing reset path zero the counter. Pure stdlib string inspection — no I/O,
    no re-parsing/executing bash.
    """
    cmd = (args or {}).get("command", "") or ""
    if not isinstance(cmd, str) or not cmd:
        return False
    # ``sed -i`` (in-place edit) and ``tee <path>`` both write to disk.
    if "sed -i" in cmd:
        return True
    if "tee " in cmd:
        return True
    # Output redirection to a path (``>`` / ``>>``), but NOT the read-only shapes
    # ``2>&1`` (fd duplication) or a redirect to ``/dev/null`` (discard).
    for token in (">>", ">"):
        idx = cmd.find(token)
        while idx != -1:
            # ``2>&1`` style fd-dup: the char after ``>`` is ``&``.
            after = cmd[idx + len(token):].lstrip()
            prev = cmd[idx - 1] if idx > 0 else ""
            is_fd_dup = after.startswith("&") or (token == ">" and prev.isdigit() and after.startswith("&"))
            is_devnull = after.startswith("/dev/null")
            if not is_fd_dup and not is_devnull:
                return True
            idx = cmd.find(token, idx + len(token))
    # A python ``-c`` / heredoc body that opens a file for writing or calls a
    # write method — the read-modify-write source-edit shape. Covers the file
    # ``.write(`` call and the idiomatic pathlib ``Path(...).write_text(...)`` /
    # ``.write_bytes(...)`` shapes.
    if ".write(" in cmd or ".write_text(" in cmd or ".write_bytes(" in cmd:
        return True
    # builtin ``open(..., 'w')`` and pathlib ``Path(...).open('w')`` (``open(``
    # is a substring of ``.open(``) with a write/append mode token present.
    if "open(" in cmd and ("'w'" in cmd or '"w"' in cmd or "'a'" in cmd or '"a"' in cmd):
        return True
    return False


class CallbackPermissionPolicy:
    def __init__(self, confirm_fn: Callable[[str], Awaitable[bool]]):
        self._confirm_fn = confirm_fn

    async def confirm(self, prompt: str) -> bool:
        return await self._confirm_fn(prompt)


class ToolExecutionUseCase(ToolExecutionRuntimeMixin):
    def __init__(
        self,
        *,
        agent: Any,
        environment: EnvironmentPort | None,
        state: SessionState,
        event_publisher: EventPublisherPort,
        event_factory: SessionEventFactory | None = None,
        tracer: TracePort | None = None,
        permission_policy: PermissionPort | None = None,
        ask_policy: AskUserPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
        cancellation_cleanup_timeout: float = DEFAULT_TOOL_CANCELLATION_CLEANUP_TIMEOUT,
        cancellation_force_timeout: float = DEFAULT_TOOL_CANCELLATION_FORCE_TIMEOUT,
        environment_abort_timeout: float = DEFAULT_TOOL_ENVIRONMENT_ABORT_TIMEOUT,
    ):
        self.agent = agent
        self.environment = environment
        self.state = state
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(state.aid)
        self.tracer = tracer
        self.permission_policy = permission_policy
        self.ask_policy = ask_policy
        self.safety_policy = safety_policy
        self._cancellation_cleanup_timeout = _require_positive_finite_timeout(
            cancellation_cleanup_timeout,
            name="cancellation_cleanup_timeout",
        )
        self._cancellation_force_timeout = _require_positive_finite_timeout(
            cancellation_force_timeout,
            name="cancellation_force_timeout",
        )
        self._environment_abort_timeout = _require_positive_finite_timeout(
            environment_abort_timeout,
            name="environment_abort_timeout",
        )
        self._pending_cleanup_tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Tasks still unwinding after a timed-out or cancelled tool call."""
        return tuple(
            task for task in self._pending_cleanup_tasks if not task.done()
        )

    async def process(self, tool_calls: list[dict]) -> ToolProcessingResult:
        """Execute a batch of tool calls and collect their result messages.

        Every call gets a ``role:"tool"`` answer — including failures (bad JSON
        args, unknown tool, loop detection), which answer with an error string
        instead of raising — so no ``tool_call_id`` is ever left unanswered.
        Repeated identical calls past ``MAX_SIMILAR_CALLS`` short-circuit with
        a loop warning rather than executing. Nothing is applied to session
        state here; the caller applies the returned ``ToolProcessingResult``.
        """
        result = ToolProcessingResult()
        preflight_errors = (
            self._preflight_tool_batch(tool_calls)
            if len(tool_calls) > 1
            else [""] * len(tool_calls)
        )
        if any(preflight_errors):
            summary = "; ".join(
                f"call {index}: {error}"
                for index, error in enumerate(preflight_errors)
                if error
            )[:2_000]
            for index, tc in enumerate(tool_calls):
                tool_id = (
                    tc.get("id")
                    if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                    else f"invalid-tool-call-{index}"
                )
                result.messages_to_append.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": (
                            preflight_errors[index]
                            if len(tool_calls) == 1
                            else "Error: entire tool-call batch rejected before execution: "
                            + summary
                        ),
                    }
                )
            return result
        recent_call_hashes = list(self.state.turn.recent_call_hashes)

        for index, tc in enumerate(tool_calls):
            func = tc["function"]
            tool_name = func["name"]
            tool_id = tc["id"]

            try:
                args = self.parse_tool_args(func)
            except json.JSONDecodeError:
                raw_args = str(func.get("arguments", ""))
                self._trace_short_circuit(
                    "tool_error",
                    tool_name,
                    {"error": "invalid_json_args", "args": raw_args[:200]},
                )
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: invalid JSON arguments: {raw_args[:200]}",
                })
                continue
            except ValueError:
                raw_args = str(func.get("arguments", ""))
                self._trace_short_circuit(
                    "tool_error",
                    tool_name,
                    {"error": "non_object_args", "args": raw_args[:200]},
                )
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: tool arguments must be a JSON object: {raw_args[:200]}",
                })
                continue

            call_hash = self.tool_call_hash(tool_name, args)
            result.recent_hash_updates.append(call_hash)
            recent_call_hashes.append(call_hash)
            if len(recent_call_hashes) > MAX_CALL_HASH_WINDOW:
                recent_call_hashes = recent_call_hashes[-MAX_CALL_HASH_WINDOW:]

            recent_same = self.count_recent_similar_calls(recent_call_hashes, call_hash)
            # Read tools collide on the path alone, so they get a more lenient
            # threshold than the exact-arg loop limit (a few re-reads are normal).
            limit = (
                MAX_SAME_FILE_READS
                if tool_name in _PATH_NORMALIZED_TOOLS
                else MAX_SIMILAR_CALLS
            )
            if recent_same >= limit:
                detail = (
                    "on the same file (any line range)"
                    if tool_name in _PATH_NORMALIZED_TOOLS
                    else "with identical arguments"
                )
                warning = (
                    f"[Loop detected: tool '{tool_name}' called {recent_same} times {detail}. "
                    f"You are stuck in a loop. Try a completely different approach or ask for help.]"
                )
                self._trace_short_circuit(
                    "loop_blocked", tool_name, {"args": args, "count": recent_same}
                )
                result.messages_to_append.append({"role": "tool", "tool_call_id": tool_id, "content": warning})
                result.loop_detections.append(LoopDetection(tool=tool_name, count=recent_same))
                await self._emit_observation(
                    lambda: self.event_factory.loop_detected(tool_name, recent_same),
                    label="loop_detected",
                )
                continue

            tool = self.find_tool(tool_name)
            if not tool:
                self._trace_short_circuit(
                    "tool_error", tool_name, {"error": "unknown_tool", "args": args}
                )
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: unknown tool '{tool_name}'. Available: {[t.name for t in self.agent.tools]}",
                })
                continue

            await self._emit_observation(
                lambda: self.event_factory.tool_start(tool_name, args),
                label="tool_start",
            )

            tool_output, tool_latency = await self.execute_tool(tool, args, tool_id=tool_id)
            # The full result is persisted; a per-tool-result budget shaper caps
            # what the model sees at call time (see application.shaping).

            # Closed-loop steering signal: a successful read accumulates the
            # reads-without-write counter; a successful edit resets it (recorded on
            # the result; applied to state by ToolProcessingResult.apply_to).
            if tool_name in _READ_TOOLS:
                result.reads_executed += 1
            elif tool_name in _WRITE_TOOLS and not tool_output.startswith(_TOOL_ERROR_PREFIXES):
                result.write_succeeded = True
            elif (
                tool_name == "bash"
                and _bash_likely_mutates(args)
                and not tool_output.startswith(_TOOL_ERROR_PREFIXES)
            ):
                # bash that writes to disk (sed -i, redirect, python RMW) lands a
                # real edit the same as file_write/apply_patch — reuse the reset
                # path so the reads-without-write counter zeroes (Bug B, OPTION 2).
                result.write_succeeded = True

            # Information-gain sensor (STEP 1): record this result's novelty
            # signals for the caller to fold into SessionState's counters. The
            # call hash (path-normalized for re-reads) and the content hash both
            # gate novelty; an empty read / "No matches"-class result is
            # intrinsically low-yield. Observational — no behavior change here.
            result.evidence_signals.append(
                (
                    _result_content_hash(tool_output),
                    call_hash,
                    _intrinsic_low_yield(tool_name, tool_output),
                )
            )
            # STEP 2 evidence ledger: the index-aligned raw card (tool/target/
            # snippet). Its outcome (hit | NO-MATCH | duplicate) is decided at fold
            # time from the SAME novelty signals above, so capture cannot disagree
            # with the sensor. Always recorded (durable capture floor), like the
            # STEP 1 signals — purely observational here.
            result.evidence_cards.append(
                {
                    "tool": tool_name,
                    "target": _card_target(tool_name, args),
                    "snippet": _card_snippet(tool_output),
                }
            )

            if self.tracer:
                self._trace_observation(
                    step_type="tool_exec",
                    payload=self.trace_payload(tool_name, args, tool_output),
                    tokens=0,
                    latency=tool_latency,
                )

            result.messages_to_append.append(self.tool_result_message(tool_id, tool_output))
            await self._emit_observation(
                lambda: self.event_factory.tool_end(tool_name, tool_latency),
                label="tool_end",
            )
            if getattr(tool, "terminal_capture_accepted", False):
                result.terminal_capture_accepted = True
                for skipped in tool_calls[index + 1 :]:
                    result.messages_to_append.append(
                        {
                            "role": "tool",
                            "tool_call_id": skipped["id"],
                            "content": TERMINAL_CAPTURE_SKIP_MESSAGE,
                        }
                    )
                break

        return result

    def _preflight_tool_batch(self, tool_calls: object) -> list[str]:
        if not isinstance(tool_calls, list):
            raise ValueError("tool_calls must be a list")
        if len(tool_calls) > MAX_TOOL_CALLS_PER_BATCH:
            return [
                f"batch has {len(tool_calls)} calls; maximum is {MAX_TOOL_CALLS_PER_BATCH}"
            ] * len(tool_calls)
        errors = [""] * len(tool_calls)
        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tool_id = tc.get("id")
            if isinstance(tool_id, str) and tool_id:
                if tool_id in seen_ids:
                    duplicate_ids.add(tool_id)
                seen_ids.add(tool_id)
        for index, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                errors[index] = "call envelope must be an object"
                continue
            tool_id = tc.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                errors[index] = "id must be non-empty text"
                continue
            if tool_id in duplicate_ids:
                errors[index] = f"duplicate tool_call id {tool_id!r}"
                continue
            func = tc.get("function")
            if not isinstance(func, dict):
                errors[index] = "function must be an object"
                continue
            tool_name = func.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                errors[index] = "function.name must be non-empty text"
                continue
            try:
                args = self.parse_tool_args(func)
            except json.JSONDecodeError:
                raw_args = str(func.get("arguments", ""))
                errors[index] = f"Error: invalid JSON arguments: {raw_args[:200]}"
                continue
            except ValueError:
                raw_args = str(func.get("arguments", ""))
                errors[index] = (
                    "Error: tool arguments must be a JSON object: "
                    f"{raw_args[:200]}"
                )
                continue
            tool = self.find_tool(tool_name)
            if tool is None:
                errors[index] = (
                    f"Error: unknown tool '{tool_name}'. Available: "
                    f"{[t.name for t in self.agent.tools]}"
                )
                continue
            schema = getattr(tool, "parameters", None)
            if isinstance(schema, dict):
                schema_errors = validate(args, schema)
                if schema_errors:
                    errors[index] = "schema validation failed: " + "; ".join(
                        schema_errors
                    )[:1_000]
        return errors

    def parse_tool_args(self, func: dict) -> dict:
        args_str = func.get("arguments", "")
        if isinstance(args_str, dict):
            return dict(args_str)
        if not isinstance(args_str, str):
            raise ValueError("tool arguments must be a JSON object")
        args = json.loads(args_str) if args_str else {}
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
        return args

    def tool_call_hash(self, tool_name: str, args: dict) -> str:
        # Path-normalized read tools key on the file path alone so re-reads of one
        # file with different line ranges collide (see _PATH_NORMALIZED_TOOLS);
        # every other tool keys on its full args.
        key_args = (
            {"path": args.get("path")} if tool_name in _PATH_NORMALIZED_TOOLS else args
        )
        return hashlib.md5(
            json.dumps({"name": tool_name, "args": key_args}, sort_keys=True).encode()
        ).hexdigest()

    def count_recent_similar_calls(self, recent_call_hashes: list[str], call_hash: str) -> int:
        # Count identical calls across the WHOLE per-turn window (already capped at
        # MAX_CALL_HASH_WINDOW and reset each turn), not just the last few. A model
        # thrashing in a multi-call CYCLE — read A,B,C,…,A,B,C,… because cleared
        # tool outputs forced re-reads — repeats each hash only once per cycle
        # (10-17 calls apart in observed 100-step stalls), so the old last-6 slice
        # never saw three of them and the loop ran to the step cap. Scanning the
        # full window catches cyclic re-reads, not only back-to-back spam.
        # (ref: opencode doom_loop)
        return sum(1 for h in recent_call_hashes if h == call_hash)

    def find_tool(self, tool_name: str):
        return self.agent.find_tool(tool_name)

"""Pure public-test output parsing for observed shell commands."""

from __future__ import annotations

import posixpath
import re
import shlex

from ._go_evidence import go_has_pass_proof as _go_has_pass_proof
from ._go_evidence import is_go_runner as _is_go_runner
from .django_evidence import django_has_pass_proof, is_django_runner

DEFAULT_RUNNER = "python -m pytest"

# Count tokens pytest prints in its final summary line, e.g.
# "===== 1 failed, 2 passed, 1 skipped in 0.12s =====".
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:,\s*)?)+\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)
_PYTEST_NO_TESTS_RE = re.compile(
    r"no tests ran in \d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)
_PYTEST_MISSING_RE = re.compile(
    r"(?:(?:\S*/)?(?:python(?:\d+(?:\.\d+)*)?|pypy\d*): )?"
    r"No module named pytest"
)


def _normalize_verification_target(target: str) -> str:
    path, separator, selectors = target.partition("::")
    normalized_path = posixpath.normpath(path)
    if separator:
        return f"{normalized_path}::{selectors}"
    return normalized_path


def _verification_target_covers(parent: str, child: str) -> bool:
    normalized_parent = _normalize_verification_target(parent)
    normalized_child = _normalize_verification_target(child)
    if normalized_parent == ".":
        return True
    if normalized_parent == normalized_child:
        return True
    if normalized_child.startswith(f"{normalized_parent}::"):
        return True
    parent_path, parent_separator, _ = normalized_parent.partition("::")
    child_path, _, _ = normalized_child.partition("::")
    return not parent_separator and child_path.startswith(f"{parent_path}/")


def _verification_targets_overlap(left: str, right: str) -> bool:
    return _verification_target_covers(left, right) or _verification_target_covers(right, left)


_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON_EXECUTABLE_RE = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)")
_PYTHON_FLAG_OPTIONS = frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v"})


def _python_invokes_pytest(parts: list[str]) -> bool:
    executable = parts[0].rsplit("/", 1)[-1]
    if _PYTHON_EXECUTABLE_RE.fullmatch(executable) is None:
        return False
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "-m":
            return index + 1 < len(parts) and parts[index + 1] == "pytest"
        if part in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
            continue
        if part.startswith(("-W", "-X")) and len(part) > 2:
            index += 1
            continue
        if part in _PYTHON_FLAG_OPTIONS:
            index += 1
            continue
        return False
    return False


def _parts_invoke_pytest(parts: list[str], *, depth: int = 0) -> bool:
    """Recognize supported direct wrappers without accepting shell interpreters."""
    if not parts or depth > 3:
        return False
    executable = parts[0].rsplit("/", 1)[-1]
    if executable == "pytest":
        return True
    if _python_invokes_pytest(parts):
        return True
    if executable == "env":
        index = 1
        while index < len(parts) and _ENV_ASSIGNMENT_RE.fullmatch(parts[index]):
            index += 1
        if index < len(parts) and parts[index] == "--":
            index += 1
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    if executable in {"uv", "poetry", "pipenv"} and len(parts) >= 3 and parts[1] == "run":
        index = 3 if parts[2] == "--" else 2
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    return executable in {"coverage", "coverage3"} and len(parts) >= 4 and parts[1:4] == ["run", "-m", "pytest"]


def _is_pytest_runner(runner: str) -> bool:
    """Whether ``runner`` directly invokes pytest (so pytest flags are safe)."""
    if any(token in runner for token in ("\n", "\r", ";", "&", "|", "<", ">", "`", "$(")):
        return False
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return _parts_invoke_pytest(parts)


def _pytest_no_tests(returncode: int, output: str) -> bool:
    """Whether pytest ran successfully enough to report an empty selection."""
    return returncode == 5 and "no tests ran" in output.lower()


def _summary_lines(output: str) -> list[str]:
    """Return every complete pytest result summary in emission order."""
    summaries = []
    for line in output.splitlines():
        candidate = line.strip().strip("= ").strip()
        if _PYTEST_SUMMARY_RE.fullmatch(candidate) or _PYTEST_NO_TESTS_RE.fullmatch(candidate):
            summaries.append(candidate)
    return summaries


def _summary_line(output: str) -> str | None:
    """The last complete pytest result summary, with or without ``====``."""
    summaries = _summary_lines(output)
    return summaries[-1] if summaries else None


def _parse_counts(summary: str | None) -> tuple[dict[str, int], int]:
    """Parse the summary line into (decision-relevant counts, warnings).

    Warnings are kept parseable but pulled OUT of the returned counts: the
    pass/fail decision is exit-code + failed/error counts only, so a noisy
    warning must never masquerade as a regression. They are returned separately
    for a 'Warnings: N (not failures)' line.
    """
    counts: dict[str, int] = {}
    warnings = 0
    if not summary:
        return counts, warnings
    for m in _COUNT_RE.finditer(summary):
        token = m.group(2)
        if token.startswith("warning"):
            warnings += int(m.group(1))
            continue
        key = token.rstrip("s") if token.startswith("error") else token
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts, warnings


def _target_has_pass_proof(target: str, passed_lines: list[str]) -> bool:
    """Whether pytest's per-test summary proves the requested target ran."""
    if not target:
        return True
    normalized = target.removeprefix("./")
    target_path, has_selector, _selector = normalized.partition("::")
    target_path = target_path.rstrip("/")
    for line in passed_lines:
        node_id = line.removeprefix("PASSED ").strip()
        candidate = node_id.removeprefix("./")
        if has_selector and not target_path:
            candidate_selector = candidate.partition("::")[2]
            if candidate_selector == _selector or candidate_selector.startswith(_selector + "["):
                return True
            continue
        if candidate == normalized or candidate.startswith(normalized + "::"):
            return True
        if has_selector and candidate.startswith(normalized + "["):
            return True
        candidate_path = candidate.partition("::")[0]
        if not has_selector and (
            target_path in {"", "."} or candidate_path == target_path or candidate_path.startswith(target_path + "/")
        ):
            return True
    return False


def has_pass_evidence(
    returncode: int,
    output: str,
    *,
    runner: str = DEFAULT_RUNNER,
    target: str = "",
    output_truncated: bool = False,
) -> bool:
    """The GREEN verdict's positive-proof specification.

    A run is GREEN only on *positive evidence* that a requested test actually
    executed and passed — never on a bare exit code, which a no-op command or a
    zero-test mode can forge. Each runner family plugs in its own proof adapter
    (pytest: exactly one summary line carrying a passing count plus per-target
    pass proof; go: parsed ``PASS`` lines); a runner with no parser-backed
    adapter cannot authorize GREEN and returns ``False`` by construction.
    """
    if output_truncated:
        # A bounded capture may have dropped the only pass/failure evidence;
        # a retained summary cannot certify the complete run.
        return False
    if is_django_runner(runner):
        return returncode == 0 and django_has_pass_proof(target, output)
    summaries = _summary_lines(output)
    if _is_pytest_runner(runner) and len(summaries) != 1:
        # One tool invocation represents one pytest session. Multiple result
        # summaries are ambiguous and let an appended passing summary hide an
        # earlier failure; no summary proves no pytest session completed.
        return False
    summary = summaries[0] if summaries else None
    counts, _ = _parse_counts(summary)
    if counts.get("failed", 0) or counts.get("error", 0):
        return False
    if returncode != 0:
        return False
    if _is_pytest_runner(runner):
        if counts.get("passed", 0) <= 0:
            return False
        return _target_has_pass_proof(target, _passed_tests(output))
    if _is_go_runner(runner):
        return _go_has_pass_proof(target, output)
    # Native runners need an explicit, parser-backed proof adapter before their
    # output can authorize a GREEN verdict. A bare exit code is forgeable via
    # no-op commands and zero-test modes.
    return False


def _passed_tests(output: str) -> list[str]:
    """Node-ids reported as PASSED in the -rA short summary (default pytest)."""
    passes = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("PASSED "):
            passes.append(s)
    return passes

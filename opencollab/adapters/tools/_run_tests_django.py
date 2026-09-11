"""Django source-tree runner discovery and unittest execution evidence."""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any

DJANGO_RUNNER = "python tests/runtests.py"
_LABEL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_RAN = re.compile(r"Ran (\d+) tests? in [\d.]+s")
_RESULT = re.compile(
    r"^(test\S*) \(([^()]+)\)(?:\n[^\n]*)? \.\.\. "
    r"(ok|FAIL|ERROR|skipped .+|expected failure|unexpected success)$",
    re.MULTILINE,
)
_OK = re.compile(r"OK(?: \((?:skipped=\d+|expected failures=\d+)(?:, (?:skipped=\d+|expected failures=\d+))*\))?")


def is_django_runner(runner: str) -> bool:
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return (
        len(parts) == 2
        and re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)", parts[0].rsplit("/", 1)[-1]) is not None
        and parts[1] in {"tests/runtests.py", "./tests/runtests.py"}
    )


async def detect_django_runner(env: Any) -> str | None:
    """Prefer Django's own entry point only in its source repository."""
    read = getattr(env, "read_text_range", None)
    if not callable(read):
        return None
    try:
        for path in ("tests/runtests.py", "django/__init__.py"):
            await read(posixpath.join(env.workspace, path), offset=1, limit=1, max_chars=256)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return DJANGO_RUNNER


class InvalidDjangoTargetError(ValueError):
    """The requested selector cannot be represented by one Django label."""


def django_target(target: str) -> str:
    if target in {"", ".", "./", "tests", "tests/", "./tests", "./tests/"}:
        return ""
    if target.startswith("/"):
        raise InvalidDjangoTargetError("Django target must be relative to the repository")
    label = target.removeprefix("./").removeprefix("tests/")
    path, separator, selector = label.partition("::")
    label = path.removesuffix(".py").strip("/").replace("/", ".")
    if separator:
        label += "." + selector.replace("::", ".")
    if _LABEL.fullmatch(label) is None:
        raise InvalidDjangoTargetError("Django target must be one dotted test label or repository test path")
    return label


def django_command(runner: str, target: str) -> str:
    label = django_target(target)
    return f"{runner} --verbosity 2 --parallel 1" + (f" {shlex.quote(label)}" if label else "")


def django_evidence(output: str) -> tuple[dict[str, int], list[str], list[str], int | None, bool]:
    """Require one complete unittest run, not just a successful exit status."""
    lines = [line.strip() for line in output.splitlines()]
    ran = [_RAN.fullmatch(line) for line in lines]
    totals = [int(match[1]) for match in ran if match]
    terminals = [line for line in lines if _OK.fullmatch(line) or line.startswith("FAILED (")]
    passed, failed = [], []
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    seen = set()
    for match in _RESULT.finditer(output):
        method, parent, status = match.groups()
        node = parent if parent.endswith("." + method) else parent + "." + method
        if node in seen:
            return counts, passed, failed, None, False
        seen.add(node)
        if status == "ok":
            counts["passed"] += 1
            passed.append(node)
        elif status.startswith("skipped") or status == "expected failure":
            counts["skipped"] += 1
        else:
            counts["error" if status == "ERROR" else "failed"] += 1
            failed.append(node)
    total = totals[0] if len(totals) == 1 else None
    valid = (
        total is not None and total > 0 and total == sum(counts.values())
        and len(terminals) == 1 and _OK.fullmatch(terminals[0]) is not None
        and not counts["failed"] and not counts["error"]
    )
    return counts, passed, failed, total, valid


def django_has_pass_proof(target: str, output: str) -> bool:
    try:
        label = django_target(target)
    except ValueError:
        return False
    _counts, passed, _failed, _total, valid = django_evidence(output)
    return valid and any(not label or node == label or node.startswith(label + ".") for node in passed)

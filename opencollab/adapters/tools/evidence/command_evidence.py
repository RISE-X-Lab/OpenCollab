"""Interpret native commands for evidence without changing their execution."""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from ._test_results import _is_pytest_runner
from .django_evidence import is_django_runner


@dataclass(frozen=True)
class TestCommand:
    runner: str
    targets: tuple[str, ...]
    workspace: str | None
    provable: bool = True


def relative_target(target: str, workspace: str | None) -> str:
    path, separator, selector = target.partition("::")
    if workspace and posixpath.isabs(path):
        path = posixpath.relpath(path, workspace)
    return posixpath.normpath(path) + separator + selector


def _targets(arguments: list[str], value_options: set[str]) -> tuple[str, ...] | None:
    targets = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in value_options:
            if index + 1 == len(arguments):
                return None
            index += 2
            continue
        if item == "--":
            targets.extend(arguments[index + 1:])
            break
        if not item.startswith("-"):
            targets.append(item)
        index += 1
    return tuple(targets) or (".",)


def parse_test_command(command: str, workspace: str | None) -> TestCommand | None:
    """Recognize direct pytest, Go and Django invocations, never shell output replay."""
    if not isinstance(command, str) or any(char in command for char in "\n\r;|<>`"):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if parts[:1] == ["cd"] and "&&" in parts:
        split = parts.index("&&")
        directory = parts[1:split]
        if directory[:1] == ["--"]:
            directory = directory[1:]
        if len(directory) != 1 or directory[0].startswith("~"):
            return None
        workspace = posixpath.normpath(posixpath.join(workspace or ".", directory[0]))
        parts = parts[split + 1:]
    if not parts or any("&" in part for part in parts):
        return None
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts = parts[1:]
    for index, item in enumerate(parts):
        if item.rsplit("/", 1)[-1] != "pytest":
            continue
        runner = shlex.join(parts[:index + 1])
        if not _is_pytest_runner(runner):
            return None
        targets = _targets(parts[index + 1:], {
            "-k", "-m", "-p", "-c", "-o", "--override-ini", "--rootdir", "--confcutdir",
            "--junitxml", "--junit-xml", "--basetemp", "--tb", "--maxfail", "--color",
            "--capture", "--import-mode", "--deselect", "--ignore", "--ignore-glob",
        })
        return TestCommand(runner, targets or (".",), workspace, targets is not None and not any(
            part in {"--collect-only", "--co", "--help", "-h", "--version"} for part in parts
        ))
    if len(parts) >= 2 and is_django_runner(shlex.join(parts[:2])):
        targets = _targets(parts[2:], {"--verbosity", "-v", "--parallel", "--settings", "--exclude-tag", "--tag"})
        return TestCommand(shlex.join(parts[:2]), targets or (".",), workspace, targets is not None and not any(
            part in {"--help", "-h", "--version"} for part in parts
        ))
    if len(parts) < 2 or parts[0].rsplit("/", 1)[-1] != "go" or parts[1] != "test":
        return None
    packages, selectors = [], []
    arguments = iter(parts[2:])
    try:
        for item in arguments:
            if item in {"-run", "-count", "-timeout", "-tags", "-p", "-parallel", "-cpu"}:
                value = next(arguments)
                if item == "-run":
                    selectors.append(value)
            elif item.startswith("-run="):
                selectors.append(item[5:])
            elif not item.startswith("-"):
                packages.append(item)
    except StopIteration:
        return None
    if len(selectors) > 1:
        return TestCommand(shlex.join(parts[:2]), (".",), workspace, False)
    selector = selectors[0].removeprefix("^").removesuffix("$") if selectors else ""
    if selector and re.fullmatch(r"\w+(?:/\w+)*", selector) is None:
        return TestCommand(shlex.join(parts[:2]), (".",), workspace, False)
    targets = tuple(package + ("::" + selector if selector else "") for package in packages or ["."])
    return TestCommand(shlex.join(parts[:2]), targets, workspace, "-json" in parts)

"""Go target parsing, command translation, and executed-test proof."""

from __future__ import annotations

import json
import shlex

GO_PATH_PREFIX = "PATH=/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:$PATH"


class InvalidGoTargetError(ValueError):
    """Raised when a Go target cannot be verified by one exact invocation."""


def is_go_runner(runner: str) -> bool:
    """Whether ``runner`` invokes ``go test``."""
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return len(parts) >= 2 and (parts[0] == "go" or parts[0].endswith("/go")) and parts[1] == "test"


def go_runner_command(runner: str) -> str:
    """Return a Go runner command with common Go install paths visible."""
    return f"{GO_PATH_PREFIX} {runner}" if runner.strip().startswith("go ") else runner


def normalize_go_package(package: str) -> str:
    package = package.strip()
    if not package:
        package = "./..."
    if package.endswith(".go"):
        package = package.rsplit("/", 1)[0] if "/" in package else "."
    if package not in {".", "./..."} and not package.startswith(("./", "../", "/")):
        package = "./" + package.strip("/")
    return package


def go_target_specs(target: str) -> list[tuple[str, str | None]]:
    """Return Go packages and optional exact selectors for one invocation."""
    if not target:
        return [("./...", None)]
    try:
        raw_targets = shlex.split(target)
    except ValueError as exc:
        raise InvalidGoTargetError("target has invalid shell quoting") from exc
    if not raw_targets:
        return [("./...", None)]

    specs: list[tuple[str, str | None]] = []
    for raw_target in raw_targets:
        if raw_target.startswith("-"):
            raise InvalidGoTargetError(
                "target entries cannot be Go command flags; use extra_args for flags"
            )
        if raw_target.count("::") > 1:
            raise InvalidGoTargetError(
                f"{raw_target!r} contains more than one '::' selector separator"
            )
        package, sep, node = raw_target.partition("::")
        if not package.strip():
            raise InvalidGoTargetError("a target has an empty package")
        if sep and not node.strip():
            raise InvalidGoTargetError(f"{raw_target!r} has an empty test selector")
        specs.append((normalize_go_package(package), node.strip() or None))

    if len(specs) > 1 and any(test_name is not None for _, test_name in specs):
        raise InvalidGoTargetError(
            "multiple targets containing a test selector are unsupported; "
            "run each package::Test target in a separate run_tests call"
        )
    return specs


def translate_go_target_args(target: str) -> list[str]:
    """Map pytest-like targets to safe ``go test`` package and ``-run`` args."""
    args: list[str] = []
    for package, test_name in go_target_specs(target):
        args.append(shlex.quote(package))
        if test_name:
            args.extend(["-run", shlex.quote(test_name)])
    return args


def has_multiple_go_selector_tokens(target: str) -> bool:
    """Detect an unpinned multi-target string that is unambiguously Go-shaped."""
    try:
        tokens = shlex.split(target)
    except ValueError:
        return False
    if len(tokens) <= 1 or not any("::" in token for token in tokens):
        return False

    def is_go_package(token: str) -> bool:
        package = token.partition("::")[0]
        return (
            package in {".", "./..."}
            or package.startswith(("./", "../", "/"))
            or package.endswith(".go")
        )

    return all(is_go_package(token) for token in tokens)


def go_package_matches(requested: str, reported: str) -> bool:
    """Match a CLI package path to ``go test -json``'s import path."""
    requested = requested.rstrip("/")
    reported = reported.rstrip("/")
    if requested in {".", "./..."}:
        return True
    if requested.endswith("/..."):
        prefix = requested[:-4].removeprefix("./").strip("/")
        return not prefix or reported == prefix or f"/{prefix}/" in f"/{reported}/"
    relative = requested.removeprefix("./").strip("/")
    if not relative or requested.startswith(("../", "/")):
        return reported == requested
    return reported == relative or reported.endswith("/" + relative)


def _go_pass_events(output: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("Action") != "pass":
            continue
        package = event.get("Package")
        test_name = event.get("Test")
        if isinstance(package, str) and package and isinstance(test_name, str) and test_name:
            events.append((package, test_name))
    return events


def _spec_has_pass_event(
    requested_package: str,
    requested_test: str | None,
    passed_events: list[tuple[str, str]],
) -> bool:
    for package, test_name in passed_events:
        if not go_package_matches(requested_package, package):
            continue
        if requested_test and not (
            test_name == requested_test or test_name.startswith(requested_test + "/")
        ):
            continue
        return True
    return False


def go_has_pass_proof(target: str, output: str) -> bool:
    """Require a matching JSON pass event for every requested Go target."""
    try:
        target_specs = go_target_specs(target)
    except InvalidGoTargetError:
        return False
    passed_events = _go_pass_events(output)
    return bool(passed_events) and all(
        _spec_has_pass_event(package, test_name, passed_events)
        for package, test_name in target_specs
    )

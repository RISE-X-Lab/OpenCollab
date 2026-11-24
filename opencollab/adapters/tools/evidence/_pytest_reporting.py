"""Read the reporting options of a supported pytest command."""

from __future__ import annotations

import configparser
import posixpath
import shlex
import sys

from opencollab.application.ports import EnvironmentPort

from .command_evidence import _PYTEST_VALUE_OPTIONS, TestCommand

if sys.version_info >= (3, 11):
    import tomllib  # deptry: ignore[DEP001]

_OPTIONS = {
    "-c": "config", "--config-file": "config",
    "-o": "addopts", "--override-ini": "addopts",
    "-r": "report", "--report-chars": "report",
    "--show-capture": "capture",
}


def _split_addopts(value: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(value))
    except ValueError:
        return ()


def _report_options(arguments: tuple[str, ...]) -> dict[str, str]:
    options = {}
    arguments_iter = iter(arguments)
    for argument in arguments_iter:
        if argument == "--":
            break
        option, equals, value = argument.partition("=")
        if option.startswith("-") and not option.startswith("--"):
            # Pytest accepts bundled switches such as -qrA.
            short = option[1:].lstrip("qsvxl")
            if short and short[0] in "cor":
                option = f"-{short[0]}"
                if len(short) > 1:
                    value = short[1:] + equals + value
                    equals = "="
        if option not in _PYTEST_VALUE_OPTIONS:
            continue
        if not equals:
            value = next(arguments_iter, "")
        key = _OPTIONS.get(option)
        if key == "addopts":
            name, separator, value = value.partition("=")
            if separator and name.strip() == "addopts":
                options[key] = value
        elif key is not None:
            options[key] = value
    return options


def _explicit_report_options(spec: TestCommand) -> dict[str, str]:
    options = _report_options(_split_addopts(spec.pytest_addopts) + spec.pytest_arguments)
    return _report_options(_split_addopts(options.get("addopts", ""))) | options


def _wants_passed_output(options: dict[str, str]) -> bool:
    if options.get("capture") == "no":
        return False
    if "report" not in options:
        return "capture" in options
    passed_output = False
    for char in options["report"]:
        # These report flags replace earlier flags in the same -r value.
        if char in "aN":
            passed_output = False
        elif char in "AP":
            passed_output = True
    return passed_output


async def _selected_config_addopts(path: str, environment: EnvironmentPort) -> tuple[str, ...]:
    suffix = posixpath.splitext(path)[1]
    if suffix not in {".ini", ".cfg", ".toml"} or (suffix == ".toml" and sys.version_info < (3, 11)):
        return ()
    try:
        content = await environment.read_file(path)
        if suffix == ".toml":
            config = tomllib.loads(content)
            if posixpath.basename(path) in {"pytest.toml", ".pytest.toml"}:
                table = config.get("pytest", {})
            else:
                table = config.get("tool", {}).get("pytest", {})
                table = table.get("ini_options", table)
            value = table.get("addopts", "")
            if isinstance(value, str):
                return _split_addopts(value)
            return tuple(value) if isinstance(value, list) and all(isinstance(flag, str) for flag in value) else ()
        parser = configparser.ConfigParser(interpolation=None, default_section="")
        parser.optionxform = str
        parser.read_string(content)
        section = "tool:pytest" if suffix == ".cfg" else "pytest"
        return _split_addopts(parser.get(section, "addopts", fallback=""))
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError, configparser.Error):
        # Pytest diagnoses configuration errors during the actual check.
        return ()


async def _workspace_wants_passed_output(spec: TestCommand, environment: EnvironmentPort) -> bool:
    explicit = _explicit_report_options(spec)
    path = explicit.get("config")
    if path and "addopts" not in explicit and "report" not in explicit and explicit.get("capture") != "no":
        config_addopts = await _selected_config_addopts(posixpath.join(spec.workspace or ".", path), environment)
        explicit = _report_options(config_addopts) | explicit
    return _wants_passed_output(explicit)

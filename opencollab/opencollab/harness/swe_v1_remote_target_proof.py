"""Parser-backed exact-target proof helpers for ProLite adapters."""

# ruff: noqa: E501

from __future__ import annotations

import json
import pathlib
import re
import shlex


def normalize_python_test_target(target):
    target = str(target)
    if "[" in target and not target.endswith("]"):
        return target.split("[", 1)[0]
    return target

def python_test_command(targets, max_args=40, max_chars=12000):
    batches = []
    current = []
    current_chars = 0
    for target in targets:
        quoted = shlex.quote(target)
        if current and (
            len(current) >= max_args or current_chars + len(quoted) + 1 > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += len(quoted) + 1
    if current:
        batches.append(current)
    commands = [
        "python3 -m pytest -vv "
        + " ".join(shlex.quote(target) for target in batch)
        for batch in batches
    ]
    return " && ".join(commands)

def python_batch_test_command(target_file, repo):
    batch_runner = """import json
import shutil
import subprocess
import sys

targets = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not isinstance(targets, list) or not targets:
    print("missing Python test targets", file=sys.stderr)
    raise SystemExit(127)
compacted = []
for value in targets:
    target = str(value)
    if "[" in target and not target.endswith("]"):
        target = target.split("[", 1)[0]
    if target and target not in compacted:
        compacted.append(target)
targets = compacted
status = 0
for offset in range(0, len(targets), 40):
    batch = [str(item) for item in targets[offset:offset + 40]]
    command = [sys.executable, "-m", "pytest", "-vv", *batch]
    if %r and shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a", sys.executable, "-m", "pytest", "--no-xvfb", "-vv", *batch]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
""" % (repo == "qutebrowser/qutebrowser")
    return "python3 -c " + shlex.quote(batch_runner) + " " + shlex.quote(target_file)

def js_runner_command(binary, package_script, target, extra_args=""):
    local_binary = f"./node_modules/.bin/{binary}"
    target_part = f" {target}" if target else ""
    extra_part = f" {extra_args}" if extra_args else ""
    package_script = shlex.quote(package_script)
    return "\n".join([
        "if [ -x " + shlex.quote(local_binary) + " ]; then",
        "  " + shlex.quote(local_binary) + extra_part + target_part,
        "elif command -v yarn >/dev/null 2>&1; then",
        f"  yarn {package_script}{extra_part}{target_part}",
        "elif command -v npx >/dev/null 2>&1; then",
        f"  npx {shlex.quote(binary)}{extra_part}{target_part}",
        "elif command -v pnpm >/dev/null 2>&1; then",
        f"  pnpm {package_script} --{extra_part}{target_part}",
        "elif command -v corepack >/dev/null 2>&1; then",
        f"  corepack pnpm {package_script} --{extra_part}{target_part}",
        "else",
        f"  echo 'No supported JS test runner found for {binary}' >&2",
        "  exit 127",
        "fi",
    ])

def canonical_js_test_files(tests, selected):
    selected_files = [str(item) for item in selected if str(item)]
    requested = [
        str(item).split(" | ", 1)[0]
        for item in tests
        if str(item) and ("/" in str(item) or "." in str(item))
    ]
    if not requested:
        requested = list(selected_files)
    canonical = []
    for item in requested:
        matches = [
            candidate
            for candidate in selected_files
            if candidate == item or candidate.endswith("/" + item)
        ]
        resolved = max(matches, key=len) if matches else item
        if resolved not in canonical:
            canonical.append(resolved)
    return canonical

def js_workspace_root(test_file):
    parts = pathlib.PurePosixPath(test_file).parts
    if len(parts) >= 3 and parts[0] in {"applications", "packages"}:
        return "/".join(parts[:2])
    return ""

def jest_test_command(test_files):
    grouped = {}
    for test_file in test_files:
        grouped.setdefault(js_workspace_root(test_file), []).append(test_file)
    commands = []
    for workspace, files in grouped.items():
        target = " ".join(shlex.quote(item) for item in files)
        extra_args = "--json --coverage=false --runInBand --verbose --runTestsByPath"
        if workspace:
            config = shlex.quote(workspace + "/jest.config.js")
            extra_args = "--config " + config + " " + extra_args
        commands.append(js_runner_command("jest", "test", target, extra_args))
    return " &&\n".join(commands)

def mocha_test_command(tests, selected, target_file=""):
    if target_file:
        launcher = """import json
import pathlib
import re
import shutil
import subprocess
import sys

tests = json.loads(open(sys.argv[1], encoding="utf-8").read())
grouped = {}
for value in tests:
    item = str(value)
    if " | " not in item:
        continue
    test_file, title = item.split(" | ", 1)
    grouped.setdefault(test_file, []).append(title)
if not grouped:
    print("missing declared Mocha titles", file=sys.stderr)
    raise SystemExit(127)
status = 0
for test_file in sorted(grouped):
    selector = "^(?:" + "|".join(re.escape(title) for title in grouped[test_file]) + ")$"
    if pathlib.Path("./node_modules/.bin/mocha").is_file():
        command = ["./node_modules/.bin/mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("yarn"):
        command = ["yarn", "test", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("npx"):
        command = ["npx", "mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("pnpm"):
        command = ["pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("corepack"):
        command = ["corepack", "pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    else:
        print("No supported JS test runner found for mocha", file=sys.stderr)
        raise SystemExit(127)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
"""
        return "python3 -c " + shlex.quote(launcher) + " " + shlex.quote(target_file)
    files = canonical_js_test_files(tests, selected)
    requested_by_file = {}
    for item in tests:
        if " | " not in str(item):
            continue
        declared_file, title = str(item).split(" | ", 1)
        matches = [
            candidate
            for candidate in files
            if candidate == declared_file or candidate.endswith("/" + declared_file)
        ]
        resolved = max(matches, key=len) if matches else declared_file
        requested_by_file.setdefault(resolved, []).append(title)
    commands = []
    for test_file in files:
        titles = requested_by_file.get(test_file) or []
        if not titles:
            commands.append(
                js_runner_command(
                    "mocha", "test", shlex.quote(test_file), "--timeout 30000 --reporter json-stream"
                )
            )
            continue
        selector = "^(?:" + "|".join(re.escape(title) for title in titles) + ")$"
        commands.append(
            js_runner_command(
                "mocha",
                "test",
                shlex.quote(test_file),
                "--timeout 30000 --reporter json-stream --grep " + shlex.quote(selector),
            )
        )
    return " &&\n".join(commands)

def tutanota_test_command(tests):
    suite_names = []
    for item in tests:
        file_name = str(item).split(" | ", 1)[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        suite_name = file_name[:-4] if file_name.endswith("Test") else file_name
        if suite_name and suite_name not in suite_names:
            suite_names.append(suite_name)
    suites_json = json.dumps(suite_names, ensure_ascii=True)
    reporter_patch = """from pathlib import Path
path = Path("test/tests/Suite.ts")
text = path.read_text(encoding="utf-8")
needle = "\tconst errCount = o.report(results, stats)"
injected = "\tconst errCount = o.report(results, stats)\\n\tconst opencollabSuites = " + %r + "\\n\tconst opencollabResults = results.filter((result) => opencollabSuites.some((suite) => JSON.stringify({task: result.task, context: result.context}).includes(suite)))\\n\tconsole.log(\\\"OPENCOLLAB_OSPEC_RESULTS \\\" + JSON.stringify(opencollabResults.map((result) => ({task: result.task, context: result.context, pass: result.pass}))))"
if needle not in text:
    raise SystemExit("missing ospec reporter insertion point")
path.write_text(text.replace(needle, injected, 1), encoding="utf-8")
""" % suites_json
    return (
        "python3 -c "
        + shlex.quote(reporter_patch)
        + " && npm_config_nodedir=/usr/local npm run test:app"
    )

def go_test_packages_from_patch(row):
    packages = []
    patch = str(row.get("test_patch") or "")
    for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        path = match.group(2)
        if not path.endswith("_test.go"):
            continue
        parent = pathlib.PurePosixPath(path).parent.as_posix()
        package = "." if parent == "." else "./" + parent
        if package not in packages:
            packages.append(package)
    return packages or ["./..."]

def go_test_command(tests):
    names = []
    for item in tests:
        name = str(item).split("/", 1)[0]
        if name and name not in names:
            names.append(name)
    discovery = """import json
import pathlib
import re
import subprocess
import sys

names = json.loads(%r)
packages = {}
for path in pathlib.Path(".").rglob("*_test.go"):
    if ".git" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    matched = [
        name for name in names
        if re.search(r"(?m)^func\\s+" + re.escape(name) + r"\\s*\\(", text)
    ]
    if not matched:
        continue
    parent = path.parent.as_posix()
    package = "." if parent == "." else "./" + parent
    packages.setdefault(package, set()).update(matched)
found = set().union(*packages.values()) if packages else set()
missing = [name for name in names if name not in found]
if missing:
    print("unable to map Go tests to packages: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(127)
status = 0
for package in sorted(packages):
    selected = sorted(packages[package])
    pattern = "^(" + "|".join(re.escape(name) for name in selected) + ")(/.*)?$"
    result = subprocess.run(["go", "test", "-count=1", "-json", package, "-run", pattern])
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
""" % json.dumps(names)
    return "python3 -c " + shlex.quote(discovery)

def ansible_python_test_command(targets, target_file=""):
    probe = """from pathlib import Path
import ansible

loaded = Path(ansible.__file__).resolve()
expected = (Path.cwd() / "lib" / "ansible").resolve()
if expected not in loaded.parents:
    raise SystemExit(f"wrong ansible import root: {loaded}")
"""
    return (
        'export PYTHONPATH="$PWD/lib${PYTHONPATH:+:$PYTHONPATH}" && '
        + "python3 -c "
        + shlex.quote(probe)
        + " && "
        + (python_batch_test_command(target_file, "ansible/ansible") if target_file else python_test_command(targets))
    )

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def fail_to_pass_execution_proof(row, tests, exit_status, log_text):
    expected = [str(item) for item in tests if str(item)]
    proof = {
        "required": bool(expected),
        "ok": False,
        "exit_status": exit_status,
        "expected": expected,
        "observed": [],
        "missing": list(expected),
        "passed": [],
        "failed": [],
    }
    if not expected:
        return proof
    text = ANSI_ESCAPE_RE.sub("", str(log_text or ""))
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        executed = set()
        passed = set()
        failed = set()
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            test_name = str(event.get("Test") or "")
            if not test_name:
                continue
            if event.get("Action") == "run":
                executed.add(test_name)
            elif event.get("Action") == "pass":
                passed.add(test_name)
            elif event.get("Action") == "fail":
                failed.add(test_name)
        observed = executed | passed | failed
        proof["observed"] = sorted(observed)
        proof["passed"] = sorted(passed)
        proof["failed"] = sorted(failed)
        proof["missing"] = [item for item in expected if item not in observed]
        not_passed = [item for item in expected if item not in passed]
    elif repo == "tutao/tutanota":
        results = []
        marker = "OPENCOLLAB_OSPEC_RESULTS "
        for line in text.splitlines():
            if marker not in line:
                continue
            payload = line.split(marker, 1)[1].strip()
            try:
                parsed = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list):
                results.extend(item for item in parsed if isinstance(item, dict))
        observed = []
        passed = []
        failed = []
        for item in expected:
            file_name = item.split(" | ", 1)[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            suite_name = file_name[:-4] if file_name.endswith("Test") else file_name
            matching = [
                result
                for result in results
                if suite_name in str(result.get("task") or "")
                or suite_name in json.dumps(result.get("context"), ensure_ascii=False)
            ]
            if not matching:
                continue
            observed.append(item)
            if all(result.get("pass") is True for result in matching):
                passed.append(item)
            else:
                failed.append(item)
        proof["observed"] = observed
        proof["missing"] = [item for item in expected if item not in observed]
        proof["passed"] = passed
        proof["failed"] = failed
        not_passed = [item for item in expected if item not in proof["passed"]]
    elif language in {"js", "javascript", "typescript", "ts"} or repo == "nodebb/nodebb":
        expected_titles = {
            item: " ".join(part.strip() for part in item.split(" | ")[1:] if part.strip())
            if " | " in item
            else item
            for item in expected
        }
        expected_title_parts = {
            item: [part.strip() for part in item.split(" | ")[1:] if part.strip()]
            if " | " in item
            else [item]
            for item in expected
        }
        passed_items = set()
        failed_items = set()
        jest_passed_fragments = set()
        jest_failed_fragments = set()

        def title_part_matches(expected_part, observed_part):
            expected_value = " ".join(str(expected_part).split())
            observed_value = " ".join(str(observed_part).split())
            if expected_value == observed_value:
                return True
            if not observed_value.startswith(expected_value):
                return False
            suffix = observed_value[len(expected_value) :]
            return bool(suffix) and suffix[0] in " ([:—-"

        def contiguous_title_parts_match(expected_parts, observed_parts):
            expected_values = [part for part in expected_parts if str(part).strip()]
            observed_values = [part for part in observed_parts if str(part).strip()]
            if not expected_values or len(expected_values) > len(observed_values):
                return False
            width = len(expected_values)
            return any(
                all(
                    title_part_matches(expected_part, observed_part)
                    for expected_part, observed_part in zip(
                        expected_values,
                        observed_values[offset : offset + width],
                    )
                )
                for offset in range(len(observed_values) - width + 1)
            )

        def canonical_expected_item(fragment, test_file=""):
            fragment_parts = (
                [" ".join(str(part).split()) for part in fragment]
                if isinstance(fragment, list)
                else []
            )
            normalized = " ".join(str(fragment).split()) if not fragment_parts else ""
            candidates = []
            for item, title in expected_titles.items():
                expected_title = " ".join(str(title).split())
                if (
                    fragment_parts
                    and contiguous_title_parts_match(
                        expected_title_parts[item],
                        fragment_parts,
                    )
                ) or (
                    not fragment_parts
                    and (
                        expected_title == normalized
                        or expected_title.endswith(" " + normalized)
                        or normalized.endswith(" " + expected_title)
                    )
                ):
                    candidates.append(item)
            normalized_file = str(test_file or "").replace("\\", "/")
            if normalized_file:
                file_candidates = []
                for item in candidates:
                    expected_file = item.split(" | ", 1)[0].replace("\\", "/")
                    if (
                        normalized_file == expected_file
                        or normalized_file.endswith("/" + expected_file)
                        or expected_file.endswith("/" + normalized_file)
                    ):
                        file_candidates.append(item)
                candidates = file_candidates
            if len(candidates) == 1:
                return candidates[0]
            return ""

        for line in text.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                stripped = line.strip()
                match = re.match(
                    r"^[✓√]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?$",
                    stripped,
                )
                if match:
                    jest_passed_fragments.add(match.group(1).strip())
                    continue
                match = re.match(
                    r"^[✕×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?$",
                    stripped,
                )
                if match:
                    jest_failed_fragments.add(match.group(1).strip())
                    continue
                match = re.match(r"^●\s+(.+)$", stripped)
                if match:
                    jest_failed_fragments.add(
                        re.sub(r"\s*[›>]\s*", " ", match.group(1)).strip()
                    )
                continue
            if isinstance(event, dict) and isinstance(event.get("testResults"), list):
                for test_result in event["testResults"]:
                    if not isinstance(test_result, dict):
                        continue
                    for assertion in test_result.get("assertionResults") or []:
                        if not isinstance(assertion, dict):
                            continue
                        ancestor_titles = (
                            assertion.get("ancestorTitles")
                            if isinstance(assertion.get("ancestorTitles"), list)
                            else []
                        )
                        assertion_title = assertion.get("title") or ""
                        title_value = (
                            [*ancestor_titles, assertion_title]
                            if ancestor_titles or assertion_title
                            else assertion.get("fullName") or ""
                        )
                        item = canonical_expected_item(
                            title_value,
                            test_result.get("name") or "",
                        )
                        status = str(assertion.get("status") or "")
                        if item and status == "passed":
                            passed_items.add(item)
                        elif item and status in {"failed", "pending", "todo"}:
                            failed_items.add(item)
                continue
            if not isinstance(event, list) or len(event) != 2 or not isinstance(event[1], dict):
                continue
            item = canonical_expected_item(event[1].get("fullTitle") or "")
            if not item:
                continue
            if event[0] == "pass":
                passed_items.add(item)
            elif event[0] == "fail":
                failed_items.add(item)

        for fragment in jest_passed_fragments:
            item = canonical_expected_item(fragment)
            if item:
                passed_items.add(item)
        for fragment in jest_failed_fragments:
            item = canonical_expected_item(fragment)
            if item:
                failed_items.add(item)
        observed_items = passed_items | failed_items
        proof["observed"] = [item for item in expected if item in observed_items]
        proof["missing"] = [item for item in expected if item not in proof["observed"]]
        proof["passed"] = [
            item for item in expected if item in passed_items and item not in failed_items
        ]
        proof["failed"] = [item for item in expected if item in failed_items]
        not_passed = [item for item in expected if item not in proof["passed"]]
    elif language == "python" or any("::" in item for item in expected):
        statuses = {}
        base_statuses = {}
        rank = {"PASSED": 0, "SKIPPED": 1, "XFAIL": 1, "XPASS": 2, "FAILED": 3, "ERROR": 4}
        for line in text.splitlines():
            match = re.match(
                r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[|\s*$)",
                line.strip(),
            )
            if not match:
                continue
            nodeid = normalize_python_test_target(match.group(1))
            status = match.group(2)
            previous = statuses.get(nodeid)
            if previous is None or rank[status] > rank[previous]:
                statuses[nodeid] = status
            base_nodeid = nodeid.split("[", 1)[0]
            base_previous = base_statuses.get(base_nodeid)
            if base_previous is None or rank[status] > rank[base_previous]:
                base_statuses[base_nodeid] = status

        def python_expected_status(item):
            value = str(item)
            normalized = normalize_python_test_target(value)
            if "[" in value and not value.endswith("]"):
                return base_statuses.get(normalized)
            return statuses.get(normalized)

        observed = [item for item in expected if python_expected_status(item) is not None]
        proof["observed"] = observed
        proof["missing"] = [item for item in expected if item not in observed]
        proof["passed"] = [
            item
            for item in observed
            if python_expected_status(item) == "PASSED"
        ]
        proof["failed"] = [item for item in observed if item not in proof["passed"]]
        not_passed = [item for item in expected if item not in proof["passed"]]
    else:
        observed = []
        missing = []
        for item in expected:
            parts = [part.strip() for part in item.split(" | ")[1:] if part.strip()]
            if parts and all(part in text for part in parts):
                observed.append(item)
            else:
                missing.append(item)
        proof["observed"] = observed
        proof["missing"] = missing
        proof["passed"] = observed if exit_status == 0 else []
        proof["failed"] = observed if exit_status != 0 else []
        not_passed = [item for item in expected if item not in proof["passed"]]
    proof["not_passed"] = not_passed
    proof["ok"] = exit_status == 0 and not not_passed
    return proof

__all__ = [name for name in globals() if not name.startswith("__")]

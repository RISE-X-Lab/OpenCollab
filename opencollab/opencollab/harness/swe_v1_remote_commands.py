"""Generation state and ProLite test-command derivation."""

# ruff: noqa: E501, F403, F405

from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_records import *
from opencollab.harness.swe_v1_remote_state import *
from opencollab.harness.swe_v1_remote_target_proof import *


def _pytest_target_matches_node(target, node):
    if "::" in target:
        return node == target or node.startswith(target + "[")
    prefix = target.rstrip("/")
    return node == prefix or node.startswith(prefix + "::") or node.startswith(prefix + "/")


def _pytest_structured_proof_matches(targets, proof_text, log_text):
    try:
        events = [json.loads(line) for line in proof_text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    if not events or any(not isinstance(event, dict) for event in events):
        return False
    if events[0].get("event") != "session_start" or events[-1].get("event") != "session_finish":
        return False
    if sum(event.get("event") == "session_start" for event in events) != 1:
        return False
    collections = [event for event in events if event.get("event") == "collection_finish"]
    finishes = [event for event in events if event.get("event") == "session_finish"]
    if len(collections) != 1 or len(finishes) != 1 or finishes[0].get("exitstatus") != 0:
        return False
    nodeids = collections[0].get("nodeids")
    if not isinstance(nodeids, list) or not nodeids or any(not isinstance(node, str) or not node for node in nodeids):
        return False
    if any(not any(_pytest_target_matches_node(target, node) for target in targets) for node in nodeids):
        return False
    reports = {}
    for event in events:
        if event.get("event") != "runtest_logreport":
            continue
        node = event.get("nodeid")
        phase = event.get("when")
        outcome = event.get("outcome")
        if (
            not isinstance(node, str)
            or node not in nodeids
            or phase not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
            or phase in reports.get(node, {})
        ):
            return False
        reports.setdefault(node, {})[phase] = outcome
    for target in targets:
        matching = [node for node in nodeids if _pytest_target_matches_node(target, node)]
        if not matching:
            return False
        if any(
            reports.get(node) != {"setup": "passed", "call": "passed", "teardown": "passed"}
            for node in matching
        ):
            return False
    if any(
        line.strip().startswith(("FAILED ", "ERROR "))
        for line in log_text.splitlines()
    ) or re.search(r"\bno tests (?:ran|collected)\b", log_text, flags=re.IGNORECASE):
        return False
    return True


def _plan_log_proof_matches(proof, log_text, proof_text=""):
    """Require positive per-target evidence from a completed test command."""
    if not proof:
        return True
    if proof.get("kind") == "pytest_structured_reports":
        targets = proof.get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        if any(not isinstance(target, str) or not target for target in targets):
            return False
        return _pytest_structured_proof_matches(targets, proof_text, log_text)
    if proof.get("kind") == "js_parser_backed_targets":
        targets = proof.get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        result = fail_to_pass_execution_proof(
            {
                "repo_language": proof.get("repo_language") or "",
                "repo": proof.get("repo") or "",
            },
            targets,
            0,
            log_text,
        )
        return result.get("ok") is True
    if proof.get("kind") != "go_json_test_pass":
        return False
    expected_test = proof.get("test")
    if not isinstance(expected_test, str) or not expected_test:
        return False
    matched = False
    for raw_line in log_text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return False
        if not isinstance(event, dict):
            return False
        if event.get("Action") == "pass" and event.get("Test") == expected_test:
            matched = True
    return matched


def task_session(task):
    issue = task.split("__", 1)[1] if "__" in task else task
    issue = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue.replace("-", "_").replace("/", "_"))
    return f"{session_prefix}_{issue}"


def generation_state_path(run_dir):
    return run_dir / "generation.state.json"


def load_json(path):
    try:
        context = open_regular_binary(path)
        handle = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_size > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        raw = handle.read(MAX_JSON_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        context.__exit__(None, None, None)


def start_count(run_dir):
    state = load_json(generation_state_path(run_dir))
    if not isinstance(state, dict):
        return 0
    starts = state.get("starts")
    if isinstance(starts, list):
        current_runtime = generation_runtime_identity()
        return sum(
            1
            for event in starts
            if isinstance(event, dict)
            and (
                not {
                    "workflow",
                    "model_name",
                    "runtime_identity",
                }.issubset(event)
                or (
                    event.get("workflow") == workflow
                    and event.get("model_name") == model_name
                    and event.get("runtime_identity") == current_runtime
                )
            )
        )
    try:
        return int(state.get("start_count") or 0)
    except Exception:
        return 0


def write_start_state(run_dir, task, session):
    if RUNNER_LOCK_FD is None:
        raise RuntimeError("runner directory ownership lock is not held")
    with RUNNER_STATE_THREAD_LOCK:
        state = load_json(generation_state_path(run_dir))
        if not isinstance(state, dict):
            state = {}
        starts = state.get("starts") if isinstance(state.get("starts"), list) else []
        try:
            previous_count = int(state.get("start_count") or 0)
        except (TypeError, ValueError):
            previous_count = 0
        count = previous_count + 1
        event = {
            "started_at": now(),
            "session": session,
            "workflow": workflow,
            "model_name": model_name,
            "runtime_identity": generation_runtime_identity(),
        }
        starts.append(event)
        state.update(
            {
                "schema": "opencollab.generation_state.v1",
                "task": task,
                "start_count": count,
                "last_started_at": event["started_at"],
                "last_session": session,
                "workflow": workflow,
                "model_name": model_name,
                "runtime_identity": generation_runtime_identity(),
                "starts": starts[-20:],
            }
        )
        write_json(generation_state_path(run_dir), state)
        return state


def write_fifo_with_timeout(path, text, timeout=45):
    data = text.encode("utf-8")
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue
        try:
            offset = 0
            while offset < len(data):
                if time.time() >= deadline:
                    return {
                        "ok": False,
                        "error": "timed out while writing complete fifo payload",
                    }
                try:
                    written = os.write(fd, data[offset:])
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if written <= 0:
                    return {"ok": False, "error": "zero-byte fifo write"}
                offset += written
            return {"ok": True}
        except OSError as exc:
            last_error = str(exc)
        finally:
            os.close(fd)
    return {"ok": False, "error": last_error or "timed out waiting for fifo reader"}


def _bounded_command_batches(items, command_prefix, max_args=80, max_chars=24000):
    """Split exact targets across commands without broadening their meaning."""
    batches = []
    current = []
    for item in items:
        candidate = [*current, item]
        candidate_command = command_prefix + " ".join(shlex.quote(value) for value in candidate)
        if current and (len(candidate) > max_args or len(candidate_command) > max_chars):
            batches.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def python_test_target_batches(tests, selected, max_args=80, max_chars=24000):
    targets = [str(item) for item in (tests or selected) if str(item)]
    return _bounded_command_batches(
        targets,
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= ",
        max_args=max_args,
        max_chars=max_chars,
    )


def compact_python_test_targets(tests, selected, max_args=80, max_chars=24000):
    """Normalize truncated parameter selectors and retain each exact target once."""
    compacted = []
    for raw in tests or selected:
        target = normalize_python_test_target(str(raw))
        if target and target not in compacted:
            compacted.append(target)
    return compacted


def go_test_packages(tests, selected):
    packages = []
    for raw in tests or selected:
        item = str(raw or "").split(" | ", 1)[0].split("::", 1)[0].strip()
        if not item:
            continue
        if item.endswith(".go"):
            package = str(pathlib.Path(item).parent).replace("\\", "/")
        elif "/" in item:
            package = item.strip("/")
            if package and not package.endswith("..."):
                package = package.rstrip("/") + "/..."
        else:
            continue
        if package in {"", "."}:
            target = "./..."
        elif package.startswith("./"):
            target = package
        else:
            target = "./" + package
        if target not in packages:
            packages.append(target)
    return packages


def go_exact_test_spec(raw):
    """Map one declared Go node to an exact package and test event."""
    declared = str(raw or "").split(" | ", 1)[0].strip()
    if "::" not in declared:
        return None
    path, test_name = (part.strip() for part in declared.split("::", 1))
    if not path.endswith(".go") or re.fullmatch(
        r"Test[A-Za-z0-9_]*(?:/[A-Za-z0-9_.-]+)*",
        test_name,
    ) is None:
        return None
    parent = str(pathlib.PurePosixPath(path.replace("\\", "/")).parent)
    if parent in {"", "."}:
        package = "."
    elif parent.startswith("./"):
        package = parent
    else:
        package = "./" + parent.strip("/")
    return {
        "declared_target": str(raw),
        "package": package,
        "test": test_name,
        "run_pattern": "^" + re.escape(test_name) + "$",
    }


_NOOP_TEST_COMMANDS = {"", "true", ":", "/bin/true"}


def _is_runnable_test_command(cmd):
    """Recognize command forms emitted by the verified adapters above."""
    if not cmd or cmd.strip() in _NOOP_TEST_COMMANDS:
        return False
    return bool(
        re.match(r"^pytest -p opencollab_pytest_proof -q -rA -o addopts= \S", cmd)
        or re.match(
            r"^xvfb-run -a python -m pytest --no-xvfb "
            r"-p opencollab_pytest_proof -q -rA -o addopts= \S",
            cmd,
        )
        or re.match(r"^go test -count=1 -json \S+ -run \S+$", cmd)
        or re.match(r"^if \[ -x \./node_modules/\.bin/(?:jest|mocha) \]; then\n", cmd)
        or cmd.startswith("python3 -c ") and "npm run test:app" in cmd
        or cmd.startswith("python3 -c ")
        and "missing declared Mocha titles" in cmd
        and "json-stream" in cmd
    )


def _test_plan(
    adapter,
    declared_targets,
    target_batches,
    commands,
    coverage,
    proofs=None,
):
    declared_targets = [str(item) for item in declared_targets if str(item)]
    commands = [str(item) for item in commands if _is_runnable_test_command(str(item))]
    target_batches = [[str(item) for item in batch] for batch in target_batches]
    flattened_targets = [item for batch in target_batches for item in batch]
    proof_batches = list(proofs or [])
    return {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": adapter,
        "coverage": coverage,
        "coverage_verified": bool(
            declared_targets
            and commands
            and len(commands) == len(target_batches)
            and flattened_targets == declared_targets
            and (not proof_batches or len(proof_batches) == len(target_batches))
        ),
        "declared_targets": declared_targets,
        "target_batches": target_batches,
        "commands": commands,
        "proofs": proof_batches,
    }


def _unsupported_test_plan(tests):
    return _test_plan("unsupported", tests, [], [], "none")


def _targets_with_paths(tests):
    mapped = []
    for raw in tests:
        declared = str(raw or "")
        path = declared.split(" | ", 1)[0].strip()
        if not path or not ("/" in path or "." in path):
            return []
        mapped.append((declared, path))
    return mapped


def prolite_test_plan(
    row,
    tests,
    max_args=80,
    max_chars=24000,
    target_file="",
):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if not tests:
        return _unsupported_test_plan(tests)
    python_targets = language == "python" or (
        not language and any("::" in item or item.endswith(".py") for item in tests)
    )
    if python_targets:
        tests = compact_python_test_targets(
            tests,
            selected,
            max_args=max_args,
            max_chars=max_chars,
        )
        target_batches = python_test_target_batches(
            tests,
            selected,
            max_args=max_args,
            max_chars=max_chars,
        )
        pytest_prefix = "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        if repo == "qutebrowser/qutebrowser":
            pytest_prefix = (
                "xvfb-run -a python -m pytest --no-xvfb "
                "-p opencollab_pytest_proof -q -rA -o addopts= "
            )
        commands = [
            pytest_prefix + " ".join(shlex.quote(item) for item in batch)
            for batch in target_batches
        ]
        proofs = [
            {
                "kind": "pytest_structured_reports",
                "targets": list(batch),
            }
            for batch in target_batches
        ]
        return _test_plan(
            "pytest",
            tests,
            target_batches,
            commands,
            "exact_targets",
            proofs=proofs,
        )
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        specs = [go_exact_test_spec(item) for item in tests]
        if any(spec is None for spec in specs):
            return _unsupported_test_plan(tests)
        exact_specs = [spec for spec in specs if spec is not None]
        target_batches = [[spec["declared_target"]] for spec in exact_specs]
        commands = [
            "go test -count=1 -json "
            + shlex.quote(spec["package"])
            + " -run "
            + shlex.quote(spec["run_pattern"])
            for spec in exact_specs
        ]
        proofs = [
            {
                "kind": "go_json_test_pass",
                "test": spec["test"],
            }
            for spec in exact_specs
        ]
        return _test_plan(
            "go-test-json",
            tests,
            target_batches,
            commands,
            "exact_test_events",
            proofs=proofs,
        )
    if language in {"js", "javascript", "typescript", "ts"} or repo in {
        "nodebb/nodebb",
        "protonmail/webclients",
        "element-hq/element-web",
        "tutao/tutanota",
    }:
        files = canonical_js_test_files(tests, selected)
        if not files:
            return _unsupported_test_plan(tests)
        if repo == "nodebb/nodebb":
            command = mocha_test_command(tests, selected, target_file)
            adapter = "mocha-json-stream"
        elif repo == "tutao/tutanota":
            command = tutanota_test_command(tests)
            adapter = "ospec-structured-results"
        else:
            command = jest_test_command(files)
            adapter = "jest-json-verbose"
        return _test_plan(
            adapter,
            tests,
            [tests],
            [command],
            "parser_backed_exact_targets",
            proofs=[
                {
                    "kind": "js_parser_backed_targets",
                    "targets": tests,
                    "repo_language": language,
                    "repo": repo,
                }
            ],
        )
    # Dataset-provided shell snippets have no machine-checkable relationship to
    # declared targets. A successful arbitrary command therefore cannot prove
    # FAIL_TO_PASS execution.
    return _unsupported_test_plan(tests)


def prolite_test_command(row, tests, target_file=""):
    plan = prolite_test_plan(row, tests, target_file=target_file)
    return " && ".join(plan["commands"])


def prolite_test_plan_script(plan, evidence_prefix, proof_nonce="proof"):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(evidence_prefix)):
        raise ValueError("invalid test evidence prefix")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(proof_nonce)):
        raise ValueError("invalid pytest proof nonce")
    lines = ["#!/usr/bin/env bash", "set +e", "overall_status=0"]
    for index, command in enumerate(plan.get("commands") or [], 1):
        stem = f"/eval_output/{evidence_prefix}.batch_{index:03d}"
        proofs = plan.get("proofs") or []
        proof = proofs[index - 1] if index <= len(proofs) else None
        command_prefix = ""
        if isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports":
            proof_path = f"{stem}.proof.{proof_nonce}.jsonl"
            command_prefix = (
                "OPENCOLLAB_PYTEST_PROOF_PATH="
                + shlex.quote(proof_path)
                + " PYTHONPATH=/eval_input${PYTHONPATH:+:$PYTHONPATH} "
            )
        lines.extend(
            [
                f"printf '%s\\n' {shlex.quote(command)} > {stem}.command",
                f"bash -c {shlex.quote(command_prefix + command)} > {stem}.log 2>&1",
                "batch_status=$?",
                f"printf '%s\\n' \"$batch_status\" > {stem}.exit",
                f"cat {stem}.log",
                'if [ "$overall_status" -eq 0 ] && [ "$batch_status" -ne 0 ]; then',
                "  overall_status=$batch_status",
                "fi",
            ]
        )
    lines.extend(['exit "$overall_status"', ""])
    return "\n".join(lines)


def prolite_pytest_proof_plugin_source():
    """Return the read-only pytest plugin used inside evaluation containers."""
    return r'''import json
import os
import stat

_fd = None


def _emit(event):
    global _fd
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if _fd is None:
        path = os.environ["OPENCOLLAB_PYTEST_PROOF_PATH"]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        _fd = os.open(path, flags, 0o600)
        opened = os.fstat(_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("pytest proof output is not regular")
    view = memoryview(payload)
    while view:
        written = os.write(_fd, view)
        if written <= 0:
            raise OSError("pytest proof write made no progress")
        view = view[written:]
    os.fsync(_fd)


def pytest_sessionstart(session):
    _emit({"event": "session_start"})


def pytest_collection_finish(session):
    _emit({"event": "collection_finish", "nodeids": [item.nodeid for item in session.items]})


def pytest_runtest_logreport(report):
    _emit({"event": "runtest_logreport", "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome})


def pytest_sessionfinish(session, exitstatus):
    global _fd
    _emit({"event": "session_finish", "exitstatus": exitstatus})
    os.close(_fd)
    _fd = None
'''


def prolite_eval_spec_sha256(row, f2p_plan, p2p_plan):
    payload = {
        "schema": "opencollab.prolite_eval_spec.v2",
        "f2p_plan": f2p_plan,
        "p2p_plan": p2p_plan,
        "test_patch_sha256": hashlib.sha256(str(row.get("test_patch") or "").encode()).hexdigest(),
        "before_repo_sha256": hashlib.sha256(str(row.get("before_repo_set_cmd") or "").encode()).hexdigest(),
        "service_bootstrap_sha256": hashlib.sha256(prolite_service_bootstrap(row).encode()).hexdigest(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def prolite_service_bootstrap(row):
    repo = str(row.get("repo") or "").lower()
    hints = " ".join(
        str(row.get(key) or "") for key in ("database", "before_repo_set_cmd", "test_cmd", "eval_cmd")
    ).lower()
    needs_redis = repo == "nodebb/nodebb" or "redis" in hints
    if not needs_redis:
        return ""
    return r"""
redis_ready() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG && return 0
  fi
  (echo > /dev/tcp/127.0.0.1/6379) >/dev/null 2>&1 && return 0
  return 1
}

if redis_ready; then
  echo "redis already ready on 127.0.0.1:6379"
  exit 0
fi

if command -v redis-server >/dev/null 2>&1; then
  mkdir -p /tmp/opencollab-redis
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /tmp/opencollab-redis --save "" --appendonly no >/tmp/prolite_redis_server.log 2>&1 || true
elif command -v service >/dev/null 2>&1; then
  service redis-server start >/tmp/prolite_redis_server.log 2>&1 || service redis start >>/tmp/prolite_redis_server.log 2>&1 || true
else
  echo "redis-server not found and service command unavailable" >&2
  exit 42
fi

for _attempt in $(seq 1 100); do
  if redis_ready; then
    echo "redis ready on 127.0.0.1:6379"
    exit 0
  fi
  sleep 0.1
done

echo "redis did not become ready on 127.0.0.1:6379" >&2
cat /tmp/prolite_redis_server.log 2>/dev/null || true
exit 42
"""


__all__ = [name for name in globals() if not name.startswith("__")]

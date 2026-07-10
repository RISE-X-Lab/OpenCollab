"""Generation state and ProLite test-command derivation."""

# ruff: noqa: E501, F403, F405

from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_records import *
from opencollab.harness.swe_v1_remote_state import *


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
        event = {"started_at": now(), "session": session, "workflow": workflow}
        starts.append(event)
        state.update(
            {
                "schema": "opencollab.generation_state.v1",
                "task": task,
                "start_count": count,
                "last_started_at": event["started_at"],
                "last_session": session,
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
        "python3 -m pytest -q ",
        max_args=max_args,
        max_chars=max_chars,
    )


def compact_python_test_targets(tests, selected, max_args=80, max_chars=24000):
    """Return every exact target; retained as a compatibility facade."""
    return [
        target
        for batch in python_test_target_batches(
            tests,
            selected,
            max_args=max_args,
            max_chars=max_chars,
        )
        for target in batch
    ]


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


def js_runner_command(binary, package_script, target, extra_args=""):
    local_binary = f"./node_modules/.bin/{binary}"
    target_part = f" {target}" if target else ""
    extra_part = f" {extra_args}" if extra_args else ""
    package_script = shlex.quote(package_script)
    return "\n".join(
        [
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
        ]
    )


_NOOP_TEST_COMMANDS = {"", "true", ":", "/bin/true"}


def _is_runnable_test_command(cmd):
    """Recognize command forms emitted by the verified adapters above."""
    if not cmd or cmd.strip() in _NOOP_TEST_COMMANDS:
        return False
    return bool(
        re.match(r"^python3 -m pytest -q \S", cmd)
        or re.match(r"^go test \S", cmd)
        or re.match(r"^if \[ -x \./node_modules/\.bin/(?:jest|mocha) \]; then\n", cmd)
    )


def _test_plan(adapter, declared_targets, target_batches, commands, coverage):
    declared_targets = [str(item) for item in declared_targets if str(item)]
    commands = [str(item) for item in commands if _is_runnable_test_command(str(item))]
    target_batches = [[str(item) for item in batch] for batch in target_batches]
    flattened_targets = [item for batch in target_batches for item in batch]
    return {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": adapter,
        "coverage": coverage,
        "coverage_verified": bool(
            declared_targets
            and commands
            and len(commands) == len(target_batches)
            and flattened_targets == declared_targets
        ),
        "declared_targets": declared_targets,
        "target_batches": target_batches,
        "commands": commands,
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


def prolite_test_plan(row, tests, max_args=80, max_chars=24000):
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
        target_batches = python_test_target_batches(
            tests,
            selected,
            max_args=max_args,
            max_chars=max_chars,
        )
        commands = [
            "python3 -m pytest -q " + " ".join(shlex.quote(item) for item in batch)
            for batch in target_batches
        ]
        return _test_plan("pytest", tests, target_batches, commands, "exact_targets")
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        packages = go_test_packages(tests, selected)
        mapped_targets = _targets_with_paths(tests)
        if not packages or len(mapped_targets) != len(tests):
            return _unsupported_test_plan(tests)
        package_for_target = []
        for _declared, path in mapped_targets:
            package = go_test_packages([path], [])
            if not package:
                return _unsupported_test_plan(tests)
            package_for_target.append(package[0])
        target_batches = _bounded_command_batches(
            tests,
            "go test ",
            max_args=max_args,
            max_chars=max_chars,
        )
        commands = []
        offset = 0
        for batch in target_batches:
            batch_packages = []
            for package in package_for_target[offset : offset + len(batch)]:
                if package not in batch_packages:
                    batch_packages.append(package)
            offset += len(batch)
            commands.append("go test " + " ".join(shlex.quote(package) for package in batch_packages))
        return _test_plan("go-test", tests, target_batches, commands, "containing_packages")
    if language in {"js", "javascript", "typescript"} or repo in {
        "nodebb/nodebb",
        "protonmail/webclients",
        "element-hq/element-web",
    }:
        mapped_targets = _targets_with_paths(tests)
        if len(mapped_targets) != len(tests):
            return _unsupported_test_plan(tests)
        target_batches = _bounded_command_batches(
            tests,
            "node-test ",
            max_args=max_args,
            max_chars=max_chars,
        )
        commands = []
        offset = 0
        for batch in target_batches:
            files = []
            for _declared, path in mapped_targets[offset : offset + len(batch)]:
                if path not in files:
                    files.append(path)
            offset += len(batch)
            target = " ".join(shlex.quote(item) for item in files)
            if repo == "nodebb/nodebb":
                commands.append(js_runner_command("mocha", "test", target, "--timeout 30000"))
            else:
                commands.append(js_runner_command("jest", "test", target))
        return _test_plan("javascript-test-file", tests, target_batches, commands, "containing_files")
    # Dataset-provided shell snippets have no machine-checkable relationship to
    # declared targets. A successful arbitrary command therefore cannot prove
    # FAIL_TO_PASS execution.
    return _unsupported_test_plan(tests)


def prolite_test_command(row, tests):
    plan = prolite_test_plan(row, tests)
    return " && ".join(plan["commands"])


def prolite_test_plan_script(plan, evidence_prefix):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(evidence_prefix)):
        raise ValueError("invalid test evidence prefix")
    lines = ["#!/usr/bin/env bash", "set +e", "overall_status=0"]
    for index, command in enumerate(plan.get("commands") or [], 1):
        stem = f"/eval_output/{evidence_prefix}.batch_{index:03d}"
        lines.extend(
            [
                f"printf '%s\\n' {shlex.quote(command)} > {stem}.command",
                f"bash -c {shlex.quote(command)} > {stem}.log 2>&1",
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

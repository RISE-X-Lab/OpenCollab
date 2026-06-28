#!/usr/bin/env bash
# Start one SWE-bench team prediction run.
#
# Drives the OpenCollab team inside the official sweb.eval container by running
# the *existing* `opencollab` CLI inside the container (via docker exec -it), so
# team progress renders live in the TUI. The host's repo and uv-managed Python
# are mounted into the container at the same paths the venv shebangs reference,
# so the venv's `opencollab` binary works out of the box — no in-container
# install. After the team finishes (or hits --timeout), `git diff` from
# /testbed is appended to the predictions JSONL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$REPO_ROOT/opencollab"
VENV_DIR="$PROJECT_DIR/.venv"
DEFAULT_TEAM_FILE="$REPO_ROOT/configs/team.self.collab.yaml"
DEFAULT_ENV_FILE="$REPO_ROOT/configs/.env"

usage() {
    cat <<'EOF'
Usage:
  scripts/start_team_run.sh \
      --instance-file <instance.json> \
      --output <predictions.jsonl> \
      [--team-file <team.yaml>] \
      [--timeout <seconds>] \
      [--model <name>] \
      [--model-name <preds tag>] \
      [--arch <x86_64|arm64>] \
      [--image <override>] \
      [--network <docker-network-mode>] \
      [--session-root <host-dir>] \
      [--mount <host[:container[:ro|rw]]>] \
      [--mount-home-ro] \
      [--no-hints] \
      [--keep-container]

Runs the OpenCollab team inside the official sweb.eval container, with TUI
progress rendered live (via docker exec -it). The team is driven by the
`opencollab` CLI directly — no Python wrapper on the host.

Defaults:
  --team-file  configs/team.self.collab.yaml
  --timeout    1800 seconds
  --arch       x86_64
  --network    host

Configuration:
  configs/.env is mounted into the container as OPENCOLLAB_CONFIG_FILE so the
  same API key + model resolution applies inside.
  /testbed/.opencollab is mounted to .opencollab/swebench/<instance_id> by
  default, so agent session history survives container cleanup.

Extra mounts:
  --mount defaults to read-only and may be repeated. Use --mount-home-ro only
  when the provider or tools really need host-level config/cache files.

Ablation:
  --no-hints omits the issue's maintainer hints_text from the task prompt
  (for measuring how much hints contribute to resolve rate).
EOF
}

die() { echo "error: $*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

find_timeout_bin() {
    if command -v timeout >/dev/null 2>&1; then
        command -v timeout
    elif command -v gtimeout >/dev/null 2>&1; then
        command -v gtimeout
    else
        printf ''
    fi
}

normalize_mount() {
    local spec="$1"
    local host=""
    local dest=""
    local mode=""
    local extra=""

    IFS=: read -r host dest mode extra <<< "$spec"
    [ -z "$extra" ] || die "invalid --mount '$spec' (expected host[:container[:ro|rw]])"
    [ -n "$host" ] || die "invalid --mount '$spec' (missing host path)"
    [ -e "$host" ] || die "mount source not found: $host"

    host="$(readlink -f "$host")"
    dest="${dest:-$host}"
    mode="${mode:-ro}"
    case "$mode" in
        ro|rw) ;;
        *) die "invalid mount mode '$mode' for $spec (expected ro or rw)" ;;
    esac

    printf '%s:%s:%s\n' "$host" "$dest" "$mode"
}

main() {
    local instance_file=""
    local output=""
    local team_file="$DEFAULT_TEAM_FILE"
    local timeout=1800
    local model=""
    local model_name=""
    local arch="x86_64"
    local image=""
    local network="host"
    local session_root=""
    local keep=0
    local mount_home_ro=0
    local include_hints=1
    local -a extra_mounts=()

    while (($#)); do
        case "$1" in
            -h|--help|help) usage; exit 0 ;;
            --instance-file) instance_file="${2:?}"; shift 2 ;;
            --output) output="${2:?}"; shift 2 ;;
            --team-file) team_file="${2:?}"; shift 2 ;;
            --timeout) timeout="${2:?}"; shift 2 ;;
            --model) model="${2:?}"; shift 2 ;;
            --model-name) model_name="${2:?}"; shift 2 ;;
            --arch) arch="${2:?}"; shift 2 ;;
            --image) image="${2:?}"; shift 2 ;;
            --network) network="${2:?}"; shift 2 ;;
            --session-root) session_root="${2:?}"; shift 2 ;;
            --mount) extra_mounts+=("${2:?}"); shift 2 ;;
            --mount-home-ro) mount_home_ro=1; shift ;;
            --no-hints) include_hints=0; shift ;;
            --keep-container) keep=1; shift ;;
            *) die "unknown argument: $1 (use --help)" ;;
        esac
    done

    [ -n "$instance_file" ] || die "--instance-file is required"
    [ -n "$output" ]        || die "--output is required"
    [ -f "$instance_file" ] || die "instance file not found: $instance_file"
    [ -f "$team_file" ]     || die "team file not found: $team_file"

    # The team file is read INSIDE the container, where opencollab runs with cwd
    # /testbed and the repo is bind-mounted at its absolute host path. A relative
    # path would resolve to /testbed/<rel> (which doesn't exist) and silently
    # fall back to the lead-only default team. Force an absolute, mounted path.
    team_file="$(readlink -f "$team_file")"
    case "$team_file" in
        "$REPO_ROOT"/*) ;;
        *) die "team file must live under $REPO_ROOT (it's mounted into the container): $team_file" ;;
    esac

    require_cmd docker
    local timeout_bin
    timeout_bin="$(find_timeout_bin)"
    if [ -z "$timeout_bin" ]; then
        echo "warn: neither timeout nor gtimeout is installed; running without a wall-clock wrapper" >&2
    fi

    local oc_bin="$VENV_DIR/bin/opencollab"
    [ -x "$oc_bin" ] || die "missing $oc_bin — run scripts/start_opencollab.sh once to build the venv."
    local py_bin="$VENV_DIR/bin/python"
    [ -x "$py_bin" ] || die "missing $py_bin — run scripts/start_opencollab.sh once to build the venv."

    # uv-managed Python: the venv's bin/python is a symlink into ~/.local/share/uv/python/...
    # The container needs that directory mounted so the symlink resolves inside.
    local py_real
    py_real="$(readlink -f "$py_bin")"
    [ -x "$py_real" ] || die "cannot resolve venv interpreter at $VENV_DIR/bin/python"
    local py_root
    py_root="$(dirname "$(dirname "$py_real")")"

    local iid
    iid="$("$py_bin" - "$instance_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    instance = json.load(f)

print(instance.get("instance_id") or "")
PY
)"
    [ -n "$iid" ] && [ "$iid" != "null" ] || die "instance.json has no instance_id"

    [ -n "$image" ] || image="sweb.eval.${arch}.${iid}:latest"
    [ -n "$model_name" ] || model_name="opencollab-team"
    [ -n "$session_root" ] || session_root="$REPO_ROOT/.opencollab/swebench/$iid"
    mkdir -p "$session_root"
    session_root="$(readlink -f "$session_root")"

    local task_file
    task_file="$(mktemp -t oc_task.XXXXXX)"
    OC_INCLUDE_HINTS="$include_hints" "$py_bin" - "$instance_file" > "$task_file" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    instance = json.load(f)

include_hints = os.environ.get("OC_INCLUDE_HINTS", "1") == "1"
repo_name = instance.get("repo") or ""
problem_statement = instance.get("problem_statement") or ""
hints_text = (instance.get("hints_text") or "").strip()
if not include_hints:
    hints_text = ""
fail_to_pass = instance.get("FAIL_TO_PASS") or []
if isinstance(fail_to_pass, str):
    try:
        fail_to_pass = json.loads(fail_to_pass)
    except json.JSONDecodeError:
        fail_to_pass = []
if not isinstance(fail_to_pass, list):
    fail_to_pass = []

print(f"# Issue to fix in `{repo_name}`")
print()
print(problem_statement)
print()
if hints_text:
    print("## Maintainer hints from the issue thread")
    print()
    print("These are real comments from project maintainers / triagers on the")
    print("upstream issue. They often name the exact file or class to change.")
    print("Read them carefully BEFORE searching the codebase.")
    print()
    print(hints_text)
    print()
print("## Tests that must pass after your fix")
if fail_to_pass:
    for test_name in fail_to_pass:
        print(f"- {test_name}")
else:
    print("- (project test suite)")
print()
print(
    "Note: a FAIL_TO_PASS test that doesn't exist in the repo yet is normal — "
    "the graders add it as part of the test patch. Do NOT spend time grepping "
    "for the test definition; focus on the source fix."
)
print()
print("Locate the root cause in the source, apply a minimal fix, and ensure the behavior described above is satisfied.")
PY

    local name
    name="oc-team-${iid}-$(date +%s%N | tail -c 7)"
    name="${name:0:60}"

    echo "Instance:  $iid"
    echo "Image:     $image"
    echo "Team file: $team_file"
    echo "Timeout:   ${timeout}s"
    echo "Network:   $network"
    echo "Hints:     $([ "$include_hints" = "1" ] && echo on || echo off)"
    echo "Sessions:  $session_root"

    local -a docker_args=(
        run -d
        --name "$name"
        --network "$network"
        --entrypoint ""
        -v "$session_root:/testbed/.opencollab:rw"
    )
    if [ "$mount_home_ro" = "1" ]; then
        [ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "cannot --mount-home-ro: HOME is not a directory"
        docker_args+=(-v "$HOME:$HOME:ro" -e HOME="$HOME")
    fi
    docker_args+=(
        -v "$REPO_ROOT:$REPO_ROOT:ro"
        -v "$py_root:$py_root:ro"
        -e PYTHONDONTWRITEBYTECODE=1
        -e OPENCOLLAB_TEAM_FILE="$team_file"
        -e OPENCOLLAB_CONFIG_FILE="$DEFAULT_ENV_FILE"
        -e TERM="${TERM:-xterm-256color}"
    )
    # Allow secret-bearing runtime config to be supplied by the caller's
    # environment instead of writing configs/.env into the repository.
    for _ocv in \
        OPENCOLLAB_PROVIDER OPENCOLLAB_BASE_URL OPENCOLLAB_MODEL \
        OPENCOLLAB_API_KEY OPENCOLLAB_BUDGET OPENCOLLAB_TEMPERATURE \
        OPENCOLLAB_TOP_P OPENCOLLAB_THINKING OPENCOLLAB_THINKING_PARAMS \
        OPENCOLLAB_LLM_TIMEOUT OPENAI_API_KEY OPENAI_BASE_URL \
        ANTHROPIC_API_KEY ANTHROPIC_BASE_URL DASHSCOPE_API_KEY; do
        if [ -n "${!_ocv:-}" ]; then
            docker_args+=(-e "${_ocv}=${!_ocv}")
        fi
    done
    # Forward proxy settings so openai/anthropic SDKs can reach the API
    # even when the host's direct route is down.  With --network host the
    # container shares the host network namespace, so 127.0.0.1 proxies work.
    for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY; do
        if [ -n "${!_pv:-}" ]; then
            docker_args+=(-e "${_pv}=${!_pv}")
        fi
    done
    local mount_spec
    if [ "${#extra_mounts[@]}" -gt 0 ]; then
        for mount_spec in "${extra_mounts[@]}"; do
            docker_args+=(-v "$(normalize_mount "$mount_spec")")
        done
    fi
    docker_args+=("$image" tail -f /dev/null)

    local cid
    cid="$(docker "${docker_args[@]}")"
    cid="${cid:0:12}"
    echo "Container: $cid ($name)"

    local host_uid host_gid
    host_uid="$(id -u)"
    host_gid="$(id -g)"
    local patch_file=""
    local cleaned=0
    cleanup() {
        [ "${cleaned:-0}" = "1" ] && return
        cleaned=1
        if [ -n "${cid:-}" ]; then
            docker exec "$cid" bash -lc \
                "chown -R '$host_uid:$host_gid' /testbed/.opencollab" >/dev/null 2>&1 || true
        fi
        if [ "${keep:-0}" = "1" ]; then
            echo "(container kept running: ${name:-unknown} / ${cid:-unknown})"
        else
            [ -z "${cid:-}" ] || docker rm -f "$cid" >/dev/null 2>&1 || true
        fi
        [ -z "${task_file:-}" ] || rm -f "$task_file"
        [ -z "${patch_file:-}" ] || rm -f "$patch_file"
    }
    trap cleanup EXIT INT TERM

    # /testbed is owned by root in the image — let git operate on it.
    docker exec "$cid" bash -lc \
        "git config --global --add safe.directory /testbed" >/dev/null
    # Keep opencollab's autosave folder out of the prediction patch.
    docker exec "$cid" bash -lc \
        "printf '/.opencollab/\n' >> /testbed/.git/info/exclude" >/dev/null

    docker cp "$task_file" "$cid:/tmp/oc_task.txt"

    # Activate the testbed conda env so the agent's python/pytest resolve to the
    # repo-specific interpreter. Then exec opencollab with --prompt-file (one-shot)
    # and --yolo (no permission prompts in non-interactive mode).
    local inner="source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
    inner+="exec '$oc_bin' --workspace /testbed --no-worktrees --yolo"
    [ -n "$model" ] && inner+=" --model '$model'"
    inner+=" --prompt-file /tmp/oc_task.txt"

    # Use -t (allocate TTY) only when our own stdout is a TTY — otherwise
    # `docker exec -it` aborts with "the input device is not a TTY", which
    # makes the team run uninvokable from background/CI contexts.
    local docker_exec_flags="-i"
    if [ -t 0 ] && [ -t 1 ]; then
        docker_exec_flags="-it"
    fi

    set +e
    if [ -n "$timeout_bin" ]; then
        "$timeout_bin" --foreground "$timeout" \
            docker exec $docker_exec_flags -w /testbed "$cid" bash -lc "$inner"
    else
        docker exec $docker_exec_flags -w /testbed "$cid" bash -lc "$inner"
    fi
    local rc=$?
    set -e
    if [ "$rc" -eq 124 ]; then
        echo "warn: opencollab hit the ${timeout}s wall-clock timeout — capturing partial diff"
    elif [ "$rc" -ne 0 ]; then
        echo "warn: opencollab exited with code $rc — capturing diff anyway"
    fi

    patch_file="$(mktemp -t oc_patch.XXXXXX)"
    docker exec -w /testbed "$cid" bash -lc 'git add -A && git diff --cached' > "$patch_file"

    mkdir -p "$(dirname "$output")"
    "$py_bin" - "$iid" "$model_name" "$patch_file" >> "$output" <<'PY'
import json
import sys

with open(sys.argv[3], encoding="utf-8", errors="surrogateescape") as f:
    patch = f.read()

print(json.dumps({
    "instance_id": sys.argv[1],
    "model_name_or_path": sys.argv[2],
    "model_patch": patch,
}, ensure_ascii=False))
PY

    local patch_size
    patch_size="$(wc -c < "$patch_file" | tr -d '[:space:]')"
    if [ "$patch_size" -gt 0 ]; then
        echo ""
        echo "Patch (${patch_size} bytes) appended to $output"
    else
        echo ""
        echo "WARNING: empty patch (team made no tracked changes)"
    fi

    cleanup
    trap - EXIT INT TERM
}

main "$@"

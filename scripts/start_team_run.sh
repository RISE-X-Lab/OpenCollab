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
TEAM_RUN_IO="$SCRIPT_DIR/swe_team_run_io.py"
TEAM_OWNER="$SCRIPT_DIR/swe_team_owner.py"
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
prepare_real_directory() {
    local python_bin="$1"
    local candidate="$2"
    local containment_root="${3:-}"
    "$python_bin" "$TEAM_RUN_IO" prepare-directory \
        "$candidate" "$containment_root"
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
    [ -n "$timeout_bin" ] || die "neither timeout nor gtimeout is installed; refusing an unbounded team run"

    local oc_bin="$VENV_DIR/bin/opencollab"
    [ -x "$oc_bin" ] || die "missing $oc_bin — run scripts/start_opencollab.sh once to build the venv."
    local py_bin="$VENV_DIR/bin/python"
    [ -x "$py_bin" ] || die "missing $py_bin — run scripts/start_opencollab.sh once to build the venv."
    "$py_bin" "$TEAM_RUN_IO" validate-timeout "$timeout" >/dev/null

    # uv-managed Python: the venv's bin/python is a symlink into ~/.local/share/uv/python/...
    # The container needs that directory mounted so the symlink resolves inside.
    local py_real
    py_real="$(readlink -f "$py_bin")"
    [ -x "$py_real" ] || die "cannot resolve venv interpreter at $VENV_DIR/bin/python"
    local py_root
    py_root="$(dirname "$(dirname "$py_real")")"

    local task_file
    task_file="$(readlink -f "$(mktemp -t oc_task.XXXXXX)")"
    local iid=""
    if ! iid="$("$py_bin" "$TEAM_RUN_IO" prepare-task \
        "$instance_file" "$task_file" --include-hints "$include_hints")"; then
        rm -f "$task_file"
        die "instance file failed bounded validation"
    fi

    [ -n "$image" ] || image="sweb.eval.${arch}.${iid}:latest"
    if ! image="$("$py_bin" "$TEAM_RUN_IO" validate-image "$image")"; then
        rm -f "$task_file"
        die "invalid Docker image reference"
    fi
    [ -n "$model_name" ] || model_name="opencollab-team"
    local session_containment=""
    if [ -z "$session_root" ]; then
        session_containment="$REPO_ROOT/.opencollab/swebench"
        session_root="$session_containment/$iid"
    fi
    if ! session_root="$(prepare_real_directory \
        "$py_bin" "$session_root" "$session_containment")"; then
        rm -f "$task_file"
        die "session root failed safe directory validation"
    fi
    local state_key state_base state_root
    state_key="$("$py_bin" "$TEAM_RUN_IO" session-key "$session_root" "$iid")"
    state_base="$REPO_ROOT/.opencollab/harness_state/team_runs"
    if ! state_root="$(prepare_real_directory \
        "$py_bin" "$state_base/$state_key" "$state_base")"; then
        rm -f "$task_file"
        die "host-only harness state failed safe directory validation"
    fi
    local legacy_state=""
    for legacy_state in \
        "$session_root/team_container.lock" \
        "$session_root/team_container.owner" \
        "$session_root/pending_prediction.record.json" \
        "$session_root/pending_prediction.patch"; do
        if [ -e "$legacy_state" ] || [ -L "$legacy_state" ]; then
            rm -f "$task_file"
            die "legacy container-writable harness state requires manual recovery: $legacy_state"
        fi
    done
    rm -f "$session_root/events.jsonl" "$session_root/loop_monitor.json"
    rm -rf "$session_root/loop_monitor_artifacts"

    local name safe_iid suffix max_iid_chars owner_nonce iid_digest pid_iid
    safe_iid="${iid//[^A-Za-z0-9_.-]/_}"
    owner_nonce="$("$py_bin" "$TEAM_OWNER" new-nonce)"
    suffix="-${owner_nonce:0:12}"
    max_iid_chars=$((63 - 8 - ${#suffix}))
    [ "$max_iid_chars" -gt 0 ] || max_iid_chars=1
    name="oc-team-${safe_iid:0:$max_iid_chars}${suffix}"
    iid_digest="$("$py_bin" "$TEAM_RUN_IO" instance-digest "$iid")"
    pid_iid="${safe_iid:0:80}-${iid_digest}-${owner_nonce:0:12}"

    local cid=""
    local host_uid host_gid
    host_uid="$(id -u)"
    host_gid="$(id -g)"
    local patch_file=""
    local pending_patch_file="$state_root/pending_prediction.patch"
    local pending_record_file="$state_root/pending_prediction.record.json"
    local patch_persisted=0
    local retirement_log_host=""
    local retirement_log_identity=""
    local retirement_log_container="/run/opencollab-retirements-${owner_nonce}.jsonl"
    local retirement_signing_key_host="" retirement_signing_key_identity=""
    local retirement_verification_key_host="" retirement_verification_key_identity=""
    local retirement_key_container="/run/opencollab-retirement-key-${owner_nonce}"
    local trusted_root="" trusted_git_dir="" workspace_snapshot_dir=""
    local base_oid=""
    local container_paused=0
    local run_pidfile="/tmp/opencollab-team-${pid_iid}.pid"
    local run_cancelfile="${run_pidfile}.cancel"
    local process_guard="$REPO_ROOT/scripts/container_process_guard.sh"
    local owner_lock="$state_root/team_container.lock"
    local owner_marker="$state_root/team_container.owner"
    local lock_held=0
    local lock_guard_pid=""
    local lock_guard_status="$state_root/team_container.lock.status"
    local cleaned=0
    local cleaning=0
    local cleanup_signal=0
    local force_destroy=0
    local docker_control_timeout=5

    docker_bounded() {
        "$timeout_bin" --foreground --kill-after=2 "$docker_control_timeout" docker "$@"
    }

    release_owner_lock() {
        if [ "$lock_held" != "1" ]; then
            return 0
        fi
        if [ -n "$lock_guard_pid" ]; then
            kill "$lock_guard_pid" >/dev/null 2>&1 || true
            wait "$lock_guard_pid" 2>/dev/null || true
        fi
        rm -f "$lock_guard_status"
        lock_guard_pid=""
        lock_held=0
    }

    destroy_container() {
        local container_name="$1"
        local container_id="${2:-$container_name}"
        local expected_nonce="${3:?missing container owner nonce}"
        local attempt inspect_output inspect_rc owned_id
        for attempt in 1 2 3 4 5; do
            if inspect_output="$(docker_bounded inspect --type container \
                --format '{{.Id}}{{printf "\t"}}{{.Name}}{{printf "\t"}}{{index .Config.Labels "opencollab.harness.owner-token"}}' \
                "$container_name" 2>&1)"; then
                inspect_rc=0
            else
                inspect_rc=$?
            fi
            if [ "$inspect_rc" -eq 1 ] && { [[ "$inspect_output" == *"No such object"* ]] || [[ "$inspect_output" == *"No such container"* ]]; }; then
                return 0
            fi
            if [ "$inspect_rc" -ne 0 ]; then
                echo "error: container ownership inspect failed for $container_name (exit $inspect_rc)" >&2
                return 125
            fi
            if ! owned_id="$("$py_bin" "$TEAM_OWNER" validate-inspect \
                "$inspect_output" "$container_name" "$container_id" \
                "$expected_nonce")"; then
                echo "error: refusing to remove unowned container $container_name" >&2
                return 125
            fi
            docker_bounded rm -f "$owned_id" >/dev/null 2>&1 || true
            sleep 0.1
        done
        if inspect_output="$(docker_bounded inspect --type container "$container_name" 2>&1)"; then
            inspect_rc=0
        else
            inspect_rc=$?
        fi
        if [ "$inspect_rc" -eq 1 ] && { [[ "$inspect_output" == *"No such object"* ]] || [[ "$inspect_output" == *"No such container"* ]]; }; then
            return 0
        fi
        if [ "$inspect_rc" -eq 0 ]; then
            echo "error: container cleanup left $container_name running" >&2
        else
            echo "error: container cleanup could not prove $container_name absent (inspect exit $inspect_rc)" >&2
        fi
        return 125
    }

    remove_current_owner_marker() {
        "$py_bin" "$TEAM_OWNER" remove-marker "$owner_marker" \
            "$state_key" "$name" "${cid:-}" "$owner_nonce"
    }

    flush_pending_prediction() {
        "$py_bin" "$TEAM_RUN_IO" flush-pending "$pending_record_file" \
            "$pending_patch_file" "$output"
    }

    create_pending_prediction() {
        local source_patch_file="${1:?missing source patch file}"
        "$py_bin" "$TEAM_RUN_IO" create-pending "$pending_record_file" \
            "$pending_patch_file" "$source_patch_file" "$output" "$iid" \
            "$model_name" "$rc"
    }

    retire_internal_retirement_log() {
        if [ -z "${retirement_log_host:-}" ]; then
            return 0
        fi
        if ! "$py_bin" "$TEAM_RUN_IO" remove-retirement-log \
            "$retirement_log_host" "$retirement_log_identity"; then
            echo "error: internal retirement log could not be safely retired" >&2
            return 125
        fi
        retirement_log_host=""
        retirement_log_identity=""
    }

    retire_internal_retirement_key() {
        local path identity
        for path in "${retirement_signing_key_host:-}" "${retirement_verification_key_host:-}"; do
            [ -z "$path" ] && continue
            if [ "$path" = "${retirement_signing_key_host:-}" ]; then
                identity="$retirement_signing_key_identity"
            else
                identity="$retirement_verification_key_identity"
            fi
            "$py_bin" "$TEAM_RUN_IO" remove-retirement-log "$path" "$identity" || return 125
        done
        retirement_signing_key_host=""; retirement_verification_key_host=""
    }

    cleanup() {
        if [ "${cleaned:-0}" = "1" ]; then
            [ -z "${task_file:-}" ] || rm -f "$task_file"
            if [ "${patch_persisted:-0}" = "0" ]; then
                [ -z "${patch_file:-}" ] || rm -f "$patch_file"
            fi
            return 0
        fi
        if [ "${cleaning:-0}" = "1" ]; then
            echo "error: recursive container cleanup for ${name:-unknown}" >&2
            return 125
        fi
        cleaning=1
        cleanup_signal=0
        trap 'cleanup_signal=130; force_destroy=1; keep=0' INT
        trap 'cleanup_signal=143; force_destroy=1; keep=0' TERM
        local cleanup_rc=0
        local kept_container=0
        if [ "${container_paused:-0}" = "1" ] && [ -n "${cid:-}" ]; then
            docker_bounded unpause "$cid" >/dev/null 2>&1 || cleanup_rc=125
            container_paused=0
        fi
        if [ -n "${cid:-}" ]; then
            docker_bounded exec "$cid" bash -lc \
                "chown -R '$host_uid:$host_gid' /testbed/.opencollab" >/dev/null 2>&1 || true
        fi
        if [ "${keep:-0}" = "1" ] && [ "${force_destroy:-0}" = "0" ] && [ -n "${cid:-}" ]; then
            echo "(container kept running: ${name:-unknown} / ${cid:-unknown})"
            kept_container=1
            cleaned=1
        else
            if destroy_container "$name" "${cid:-$name}" "$owner_nonce"; then
                cleaned=1
            else
                cleanup_rc=125
            fi
        fi
        if [ "$cleanup_signal" -ne 0 ] && [ "$kept_container" = "1" ]; then
            cleaned=0
            if destroy_container "$name" "${cid:-$name}" "$owner_nonce"; then
                cleaned=1
            else
                cleanup_rc=125
            fi
        fi
        if [ "$cleaned" = "1" ] && [ "$kept_container" = "0" ]; then
            remove_current_owner_marker
            if ! retire_internal_retirement_log; then
                cleanup_rc=125
            fi
            if ! retire_internal_retirement_key; then
                cleanup_rc=125
            fi
        fi
        [ -z "${trusted_root:-}" ] || rm -rf -- "$trusted_root"
        [ -z "${task_file:-}" ] || rm -f "$task_file"
        if [ "${patch_persisted:-0}" = "0" ]; then
            [ -z "${patch_file:-}" ] || rm -f "$patch_file"
        fi
        if [ "$cleaned" = "1" ] && [ "$lock_held" = "1" ]; then
            release_owner_lock
        fi
        cleaning=0
        trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 130; else trap - EXIT INT TERM ERR; exit 125; fi' INT
        trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 143; else trap - EXIT INT TERM ERR; exit 125; fi' TERM
        if [ "$cleanup_signal" -ne 0 ]; then
            echo "error: container cleanup was interrupted by signal $cleanup_signal" >&2
            return 125
        fi
        return "$cleanup_rc"
    }
    trap cleanup EXIT
    trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 130; else trap - EXIT INT TERM ERR; exit 125; fi' INT
    trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 143; else trap - EXIT INT TERM ERR; exit 125; fi' TERM
    trap 'force_destroy=1; keep=0' ERR

    rm -f "$lock_guard_status"
    "$py_bin" "$TEAM_OWNER" hold-lock "$owner_lock" \
        "$lock_guard_status" "$$" &
    lock_guard_pid=$!
    local lock_status=""
    local lock_wait_attempt
    for lock_wait_attempt in $(seq 1 500); do
        if [ -f "$lock_guard_status" ]; then
            lock_status="$("$py_bin" "$TEAM_OWNER" read-lock-status \
                "$lock_guard_status")"
            break
        fi
        if ! kill -0 "$lock_guard_pid" 2>/dev/null; then
            break
        fi
        sleep 0.01
    done
    if [ "$lock_status" != "locked" ]; then
        kill "$lock_guard_pid" >/dev/null 2>&1 || true
        wait "$lock_guard_pid" 2>/dev/null || true
        rm -f "$lock_guard_status"
        die "another team run already owns $session_root"
    fi
    lock_held=1

    if [ -e "$owner_marker" ] || [ -L "$owner_marker" ]; then
        local previous_identity="" previous_name="" previous_id="" previous_nonce=""
        if ! previous_identity="$("$py_bin" "$TEAM_OWNER" read-marker \
            "$owner_marker" "$state_key")"; then
            die "invalid stale container owner marker: $owner_marker"
        fi
        IFS='|' read -r previous_name previous_id previous_nonce <<< "$previous_identity"
        if [ -z "$previous_name" ] || [ -z "$previous_nonce" ]; then
            die "invalid stale container owner identity: $owner_marker"
        fi
        if ! destroy_container "$previous_name" "${previous_id:-$previous_name}" "$previous_nonce"; then
            die "could not recover stale container $previous_name"
        fi
        if ! "$py_bin" "$TEAM_OWNER" remove-marker "$owner_marker" \
            "$state_key" "$previous_name" "$previous_id" \
            "$previous_nonce" --require-match; then
            die "could not remove recovered owner marker $owner_marker"
        fi
    fi

    if [ -e "$pending_record_file" ]; then
        local recovered_rc=""
        if ! recovered_rc="$(flush_pending_prediction)"; then
            die "could not replay pending prediction $pending_record_file"
        fi
        echo "Recovered pending prediction from $session_root"
        cleaned=1
        [ -z "${task_file:-}" ] || rm -f "$task_file"
        if [ "$lock_held" = "1" ]; then
            release_owner_lock
        fi
        trap - EXIT INT TERM ERR
        if [ "$recovered_rc" = "0" ]; then
            return 0
        fi
        return 1
    elif [ -e "$pending_patch_file" ]; then
        die "unrecorded pending patch requires recovery: $pending_patch_file"
    fi

    trusted_root="$(readlink -f "$(mktemp -d -t opencollab-harness-trusted.XXXXXX)")"
    chmod 700 "$trusted_root"
    retirement_log_host="$state_root/internal-retirements-${owner_nonce}.jsonl"
    if ! retirement_log_identity="$("$py_bin" "$TEAM_RUN_IO" \
        create-retirement-log "$retirement_log_host")"; then
        die "could not create the host-owned internal retirement log"
    fi
    retirement_signing_key_host="$trusted_root/signing.key"
    retirement_verification_key_host="$trusted_root/verification.key"
    local key_identities
    if ! key_identities="$("$py_bin" "$TEAM_RUN_IO" create-retirement-keys \
        "$retirement_signing_key_host" "$retirement_verification_key_host")"; then
        die "could not create host-owned internal retirement keys"
    fi
    IFS='|' read -r retirement_signing_key_identity \
        retirement_verification_key_identity <<< "$key_identities"

    "$py_bin" "$TEAM_OWNER" create-marker "$owner_marker" \
        "$name" "$state_key" "$owner_nonce"

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
        --label "opencollab.harness.owner-token=$owner_nonce"
        --network "$network"
        --entrypoint ""
        -v "$session_root:/testbed/.opencollab:rw"
        -v "$retirement_log_host:$retirement_log_container:rw"
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
        -e OPENCOLLAB_EVENTS_FILE="/testbed/.opencollab/events.jsonl"
        -e OPENCOLLAB_INTERNAL_RETIREMENT_LOG="$retirement_log_container"
        -e OPENCOLLAB_INTERNAL_RETIREMENT_KEY_FILE="$retirement_key_container"
        -e OPENCOLLAB_INTERNAL_RETIREMENT_WORKSPACE="/testbed"
        -e TERM="${TERM:-xterm-256color}"
    )
    for _ocv in \
        OPENCOLLAB_PROVIDER OPENCOLLAB_BASE_URL OPENCOLLAB_MODEL \
        OPENCOLLAB_API_KEY OPENCOLLAB_BUDGET OPENCOLLAB_TEMPERATURE \
        OPENCOLLAB_TOP_P OPENCOLLAB_THINKING OPENCOLLAB_THINKING_PARAMS \
        OPENCOLLAB_LLM_TIMEOUT OPENAI_API_KEY OPENAI_BASE_URL \
        ANTHROPIC_API_KEY ANTHROPIC_BASE_URL DASHSCOPE_API_KEY; do
        if [ -n "${!_ocv:-}" ]; then
            docker_args+=(-e "${_ocv}")
        fi
    done
    for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY; do
        if [ -n "${!_pv:-}" ]; then
            docker_args+=(-e "${_pv}")
        fi
    done
    local mount_spec
    if [ "${#extra_mounts[@]}" -gt 0 ]; then
        for mount_spec in "${extra_mounts[@]}"; do
            docker_args+=(-v "$(normalize_mount "$mount_spec")")
        done
    fi
    docker_args+=(-- "$image" tail -f /dev/null)

    local raw_cid
    local docker_run_rc
    set +e
    raw_cid="$("$timeout_bin" --foreground --kill-after=5 120 docker "${docker_args[@]}")"
    docker_run_rc=$?
    set -e
    if [ "$docker_run_rc" -ne 0 ] || [ -z "${raw_cid//[[:space:]]/}" ]; then
        force_destroy=1
        keep=0
        die "docker run failed or exceeded its 120s setup bound (exit $docker_run_rc)"
    fi
    local validated_cid=""
    if ! validated_cid="$("$py_bin" "$TEAM_OWNER" validate-cid \
        "$raw_cid")"; then
        force_destroy=1
        keep=0
        local invalid_cid_rc=1
        if ! cleanup; then
            invalid_cid_rc=125
        fi
        trap - EXIT INT TERM ERR
        echo "error: docker run returned an invalid container id" >&2
        return "$invalid_cid_rc"
    fi
    cid="$validated_cid"
    "$py_bin" "$TEAM_OWNER" bind-cid "$owner_marker" "$state_key" \
        "$name" "$owner_nonce" "$cid"
    echo "Container: ${cid:0:12} ($name)"

    docker_bounded exec "$cid" bash -lc \
        "git config --global --add safe.directory /testbed" >/dev/null
    docker_bounded exec "$cid" bash -lc \
        "printf '/.opencollab/\n' >> /testbed/.git/info/exclude" >/dev/null

    if ! base_oid="$(docker_bounded exec "$cid" env -i PATH=/usr/bin:/bin \
        HOME=/tmp XDG_CONFIG_HOME=/tmp GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
        GIT_ATTR_NOSYSTEM=1 git -C /testbed rev-parse --verify 'HEAD^{commit}')"; then
        force_destroy=1
        keep=0
        die "could not pin the task base commit before model execution"
    fi
    case "$base_oid" in
        ''|*[!0-9a-fA-F]*) die "task base commit is malformed" ;;
    esac
    if [ "${#base_oid}" -ne 40 ] && [ "${#base_oid}" -ne 64 ]; then
        die "task base commit has an unsupported object id length"
    fi
    trusted_git_dir="$(prepare_real_directory \
        "$py_bin" "$trusted_root/git" "$trusted_root")"
    if ! "$timeout_bin" --foreground --kill-after=5 300 docker cp \
        "$cid:/testbed/.git/." - | \
        "$py_bin" "$REPO_ROOT/scripts/safe_workspace_snapshot.py" \
        "$trusted_git_dir" --reject-symlinks >/dev/null; then
        force_destroy=1
        keep=0
        die "could not capture trusted pre-task Git objects"
    fi
    docker_bounded cp "$task_file" "$cid:/tmp/oc_task.txt"
    docker_bounded exec "$cid" bash "$process_guard" prepare \
        "$run_pidfile" "$run_cancelfile"
    docker_bounded cp "$retirement_signing_key_host" "$cid:$retirement_key_container"
    "$py_bin" "$TEAM_RUN_IO" remove-retirement-log \
        "$retirement_signing_key_host" "$retirement_signing_key_identity"
    retirement_signing_key_host=""

    local inner oc_bin_q model_q
    printf -v oc_bin_q '%q' "$oc_bin"
    inner="source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
    inner+="exec $oc_bin_q --workspace /testbed --no-worktrees --yolo"
    if [ -n "$model" ]; then
        printf -v model_q '%q' "$model"
        inner+=" --model $model_q"
    fi
    inner+=" --prompt-file /tmp/oc_task.txt"

    local -a docker_exec_flags=(-i)
    if [ -t 0 ] && [ -t 1 ]; then
        docker_exec_flags=(-it)
    fi

    set +e
    "$timeout_bin" --foreground --kill-after=5 "$timeout" \
        docker exec "${docker_exec_flags[@]}" -w /testbed "$cid" \
        bash "$process_guard" run "$run_pidfile" "$run_cancelfile" \
        bash -lc "$inner"
    local rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        local stop_ok=1
        if ! docker_bounded exec "$cid" bash "$process_guard" stop "$run_pidfile" "$run_cancelfile"; then
            stop_ok=0
        fi
        if [ "$rc" -eq 125 ] || [ "$stop_ok" = "0" ]; then
            force_destroy=1
            keep=0
            die "container process group did not quiesce; refusing to extract a racing patch"
        fi
    fi
    if [ "$rc" -eq 124 ]; then
        echo "warn: opencollab hit the ${timeout}s wall-clock timeout — capturing partial diff"
    elif [ "$rc" -ne 0 ]; then
        echo "warn: opencollab exited with code $rc — capturing diff anyway"
    fi

    if ! docker_bounded pause "$cid" >/dev/null; then
        force_destroy=1
        keep=0
        die "container could not be frozen before trusted snapshot extraction"
    fi
    container_paused=1
    workspace_snapshot_dir="$(prepare_real_directory \
        "$py_bin" "$trusted_root/workspace" "$trusted_root")"
    if ! "$timeout_bin" --foreground --kill-after=5 300 docker cp \
        "$cid:/testbed/." - | \
        "$py_bin" "$REPO_ROOT/scripts/safe_workspace_snapshot.py" \
        "$workspace_snapshot_dir" --exclude-top .git --exclude-top .opencollab \
        >/dev/null; then
        force_destroy=1
        keep=0
        die "frozen container workspace snapshot failed bounded validation"
    fi
    local bounded_diff
    if ! bounded_diff="$("$py_bin" "$TEAM_RUN_IO" bounded-diff-command \
        "$REPO_ROOT/swebench" --workspace "$workspace_snapshot_dir" \
        --retirement-log "$retirement_log_host" \
        --retirement-key "$retirement_verification_key_host" --portable-snapshot \
        --base-revision "$base_oid" \
        --object-directory "$trusted_git_dir/objects" \
        --helper-path "$REPO_ROOT/swebench/gen_prediction_bounded_capture.py")"; then
        force_destroy=1
        keep=0
        echo "error: internal retirement validation failed; refusing patch extraction" >&2
        return 125
    fi
    "$py_bin" "$TEAM_RUN_IO" remove-retirement-log \
        "$retirement_verification_key_host" "$retirement_verification_key_identity"
    retirement_verification_key_host=""
    patch_file="$(readlink -f "$(mktemp "$state_root/extracted-patch.XXXXXX")")"
    set +e
    (
        cd "$workspace_snapshot_dir" || exit 125
        "$timeout_bin" --foreground --kill-after=5 120 bash -lc "$bounded_diff"
    ) > "$patch_file"
    local diff_rc=$?
    set -e
    if [ "$diff_rc" -ne 0 ]; then
        force_destroy=1
        keep=0
        die "bounded patch extraction failed (exit $diff_rc); prediction was not appended"
    fi

    local extracted_patch_file="$patch_file"
    patch_file=""
    if ! create_pending_prediction "$extracted_patch_file"; then
        die "could not persist pending prediction in $session_root"
    fi
    patch_file="$pending_patch_file"
    patch_persisted=1

    local patch_size
    patch_size="$(wc -c < "$patch_file" | tr -d '[:space:]')"
    if [ "$patch_size" -gt 0 ]; then
        echo ""
        echo "Patch prepared (${patch_size} bytes)"
    else
        echo ""
        echo "WARNING: empty patch (team made no tracked changes)"
    fi

    local monitor_file="$session_root/loop_monitor.json"
    if "$py_bin" "$REPO_ROOT/scripts/swebench_loop_monitor.py" \
        --instance-id "$iid" \
        --session-root "$session_root" \
        --events-file "$session_root/events.jsonl" \
        --diff-file "$patch_file" \
        --output "$monitor_file"; then
        echo "Loop monitor: $monitor_file"
    else
        echo "warn: loop monitor failed for $iid" >&2
    fi

    local final_rc=0
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then
        final_rc="$rc"
    fi
    if [ "$patch_size" -eq 0 ] && [ "$final_rc" -eq 0 ]; then
        final_rc=1
    fi
    if ! cleanup; then
        trap - EXIT INT TERM ERR
        return 125
    fi

    local flushed_rc=""
    if ! flushed_rc="$(flush_pending_prediction)"; then
        die "prediction append failed; patch preserved at $pending_patch_file"
    fi
    patch_persisted=0
    patch_file=""
    echo "Prediction appended to $output"
    trap - EXIT INT TERM ERR
    return "$final_rc"
}

main "$@"

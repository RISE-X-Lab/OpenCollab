"""Shared constants and validation for execution-environment adapters."""

from __future__ import annotations

import re
import subprocess

PROCESS_TERM_GRACE_SECONDS = 1.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 2.0
PROCESS_SPAWN_HANDOFF_TIMEOUT_SECONDS = 2.0
DOCKER_SETUP_TIMEOUT_SECONDS = 120.0
DOCKER_COMPENSATION_TIMEOUT_SECONDS = 10.0
DOCKER_CANCEL_COMMAND_TIMEOUT_SECONDS = 5.0
DOCKER_WRITE_TIMEOUT_SECONDS = 120.0
DOCKER_EXEC_QUIESCENCE_FAILURE_RETURN_CODE = 197
WORKTREE_GIT_TIMEOUT_SECONDS = 30.0
PROCESS_OUTPUT_CAPTURE_BYTES = 1_048_576
PROCESS_IO_JOIN_TIMEOUT_SECONDS = 1.0
LOCAL_FILE_READ_LIMIT_BYTES = 4_194_304
LOCAL_FILE_WRITE_LIMIT_BYTES = 4_194_304
DOCKER_OWNER_LABEL = "opencollab.harness.owner-token"
DOCKER_REFERENCE_MAX_BYTES = 512
DOCKER_CONTAINER_NAME_MAX_BYTES = 255
_DOCKER_MISSING_RE = re.compile(rb"no such (?:container|object)", re.IGNORECASE)
_DOCKER_INSPECT_FORMAT = (
    '{{.Id}}{{printf "\\t"}}{{.Name}}{{printf "\\t"}}{{index .Config.Labels "' + DOCKER_OWNER_LABEL + '"}}'
)
_DOCKER_ATTACH_INSPECT_FORMAT = '{{.Id}}{{printf "\\t"}}{{.Name}}{{printf "\\t"}}{{.State.Running}}'

_PROCESS_POPEN = subprocess.Popen


def _validate_docker_image_reference(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Docker image reference must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Docker image reference must be valid UTF-8") from exc
    if (
        len(encoded) > DOCKER_REFERENCE_MAX_BYTES
        or value.startswith("-")
        or "://" in value
        or any(character.isspace() for character in value)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", value) is None
    ):
        raise ValueError("Docker image reference is unsafe or malformed")
    return value


def _validate_docker_container_reference(value: object) -> str:
    """Accept an unambiguous full id or a bounded Docker container name."""
    if not isinstance(value, str) or not value:
        raise ValueError("Docker container reference must be non-empty text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Docker container reference must be ASCII") from exc
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    if (
        len(encoded) > DOCKER_CONTAINER_NAME_MAX_BYTES
        or value.startswith("-")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None
        or re.fullmatch(r"[0-9a-fA-F]+", value) is not None
    ):
        raise ValueError("Docker container reference is unsafe or ambiguous")
    return value


_DOCKER_EXEC_WRAPPER = r"""
pidfile=$1
cancelfile=$2
shellflag=$3
command=$4
shift 4
cleanup() { rm -f -- "$pidfile" "$cancelfile"; }
terminate_child() {
    kill -TERM -- "-$child" 2>/dev/null || true
    probe=0
    while [ "$probe" -lt 10 ] && kill -0 -- "-$child" 2>/dev/null; do
        sleep 0.05
        probe=$((probe + 1))
    done
    if kill -0 -- "-$child" 2>/dev/null; then
        kill -KILL -- "-$child" 2>/dev/null || true
    fi
    wait "$child" 2>/dev/null || true
    probe=0
    while [ "$probe" -lt 20 ] && kill -0 -- "-$child" 2>/dev/null; do
        sleep 0.05
        probe=$((probe + 1))
    done
    if kill -0 -- "-$child" 2>/dev/null; then
        return 1
    fi
    return 0
}
if [ -e "$cancelfile" ]; then
    cleanup
    exit 143
fi
if command -v setsid >/dev/null 2>&1; then
    setsid bash "$shellflag" "$command" "$@" <&0 &
else
    set -m
    bash "$shellflag" "$command" "$@" <&0 &
fi
child=$!
if ! printf '%s\n' "$child" > "$pidfile"; then
    if terminate_child; then
        cleanup
        exit 125
    fi
    exit 197
fi
if [ -e "$cancelfile" ]; then
    if terminate_child; then
        cleanup
        exit 143
    fi
    exit 197
fi
wait "$child"
status=$?
if kill -0 -- "-$child" 2>/dev/null; then
    if terminate_child; then
        cleanup
        exit 125
    fi
    exit 197
fi
cleanup
exit "$status"
""".strip()

_DOCKER_EXEC_CANCEL = r"""
pidfile=$1
cancelfile=$2
if ! : > "$cancelfile"; then
    exit 125
fi
attempt=0
while [ "$attempt" -lt 20 ]; do
    if read -r child < "$pidfile" 2>/dev/null; then
        case "$child" in
            ''|*[!0-9]*) exit 125 ;;
        esac
        kill -TERM -- "-$child" 2>/dev/null || true
        probe=0
        while [ "$probe" -lt 2 ] && kill -0 -- "-$child" 2>/dev/null; do
            sleep 0.05
            probe=$((probe + 1))
        done
        if kill -0 -- "-$child" 2>/dev/null; then
            kill -KILL -- "-$child" 2>/dev/null || true
        fi
        probe=0
        while [ "$probe" -lt 20 ] && kill -0 -- "-$child" 2>/dev/null; do
            sleep 0.05
            probe=$((probe + 1))
        done
        if kill -0 -- "-$child" 2>/dev/null; then
            exit 124
        fi
        exit 0
    fi
    sleep 0.05
    attempt=$((attempt + 1))
done
exit 124
""".strip()

_DOCKER_WRITE_AND_VERIFY = r"""
target=$1
mkdir -p -- "$(dirname -- "$target")" || exit 73
cat > "$target" || exit 74
bytes=$(wc -c < "$target") || exit 74
bytes=${bytes//[[:space:]]/}
if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum -- "$target" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 -- "$target" | awk '{print $1}')
elif command -v openssl >/dev/null 2>&1; then
    digest=$(openssl dgst -sha256 "$target" | awk '{print $NF}')
else
    exit 69
fi
case "$bytes" in
    ''|*[!0-9]*) exit 65 ;;
esac
case "$digest" in
    ''|*[!0-9a-f]*) exit 65 ;;
esac
printf '%s\t%s\n' "$bytes" "$digest"
""".strip()

_DOCKER_CREATE_WRITE_AND_VERIFY = r"""
target=$1
umask 077
set -o noclobber
if ! exec 3> "$target"; then
    exit 73
fi
set +o noclobber
owned_identity=$(stat -Lc '%d:%i' /proc/self/fd/3 2>/dev/null) || exit 69
cleanup_owned() {
    current=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || return 0
    if [ "$current" = "$owned_identity" ]; then
        rm -f -- "$target"
    fi
}
trap 'cleanup_owned' EXIT HUP INT TERM
cat >&3 || exit 74
bytes=$(wc -c < /proc/self/fd/3) || exit 74
bytes=${bytes//[[:space:]]/}
if command -v sha256sum >/dev/null 2>&1; then
    digest=$(sha256sum -- /proc/self/fd/3 | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    digest=$(shasum -a 256 -- /proc/self/fd/3 | awk '{print $1}')
elif command -v openssl >/dev/null 2>&1; then
    digest=$(openssl dgst -sha256 /proc/self/fd/3 | awk '{print $NF}')
else
    exit 69
fi
case "$bytes" in
    ''|*[!0-9]*) exit 65 ;;
esac
case "$digest" in
    ''|*[!0-9a-f]*) exit 65 ;;
esac
current_identity=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || exit 75
if [ "$current_identity" != "$owned_identity" ]; then
    exit 75
fi
trap - EXIT HUP INT TERM
exec 3>&-
printf '%s\t%s\t%s\n' "$owned_identity" "$bytes" "$digest"
""".strip()

_DOCKER_REMOVE_OWNED_TEMP = r"""
target=$1
expected=$2
current=$(stat -Lc '%d:%i' -- "$target" 2>/dev/null) || exit 0
if [ "$current" != "$expected" ]; then
    exit 76
fi
rm -f -- "$target"
""".strip()

"""Shared scheduler runtime limits."""

WORKTREE_DIFF_MAX_CHARS = 12_000
WORKTREE_DIFF_KEEP_CHARS = 6_000
DEFAULT_SCHEDULER_CLEANUP_TIMEOUT = 10.0

# Teammate messages are persisted both in a session sidecar and in the
# scheduler inbox. Keep each representation and every model-bound delivery
# finite, using encoded bytes because that is stable across Python strings and
# directly bounds the serialized prompt payload.
MAX_TEAMMATE_MESSAGE_BYTES = 8 * 1024
MAX_TEAMMATE_INBOX_MESSAGES = 32
MAX_TEAMMATE_INBOX_BYTES = 64 * 1024
MAX_TEAMMATE_DELIVERY_BYTES = 16 * 1024

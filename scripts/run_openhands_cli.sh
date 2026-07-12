#!/usr/bin/env bash
set -euo pipefail

REMOTE_ROOT="${OPENCOLLAB_REMOTE_ROOT:-}"
REMOTE_REPO="${OPENCOLLAB_REMOTE_REPO:-}"
OPENHANDS_SITE="${OPENCOLLAB_OPENHANDS_SITE:-${REMOTE_ROOT:+$REMOTE_ROOT/tools/openhands-site}}"
PYDEPS="${OPENCOLLAB_PYDEPS:-${REMOTE_ROOT:+$REMOTE_ROOT/pydeps}}"
PYTHON_BIN="${OPENCOLLAB_OPENHANDS_PYTHON:-python3}"

if [[ -z "$REMOTE_REPO" || ! -d "$REMOTE_REPO" ]]; then
  echo "OpenHands requires OPENCOLLAB_REMOTE_REPO to name an existing directory" >&2
  exit 2
fi
if [[ ! -d "$OPENHANDS_SITE/openhands_cli" ]]; then
  echo "OpenHands runtime is missing; set OPENCOLLAB_OPENHANDS_SITE or OPENCOLLAB_REMOTE_ROOT" >&2
  exit 2
fi
if [[ ! -d "$PYDEPS" ]]; then
  echo "OpenHands dependencies are missing; set OPENCOLLAB_PYDEPS or OPENCOLLAB_REMOTE_ROOT" >&2
  exit 2
fi
if [[ -z "${LLM_API_KEY:-}" || -z "${LLM_MODEL:-}" ]]; then
  echo "OpenHands requires LLM_API_KEY and LLM_MODEL" >&2
  exit 2
fi

export PYTHONPATH="$REMOTE_REPO:$OPENHANDS_SITE:$PYDEPS${PYTHONPATH:+:$PYTHONPATH}"
export TTY_INTERACTIVE="${TTY_INTERACTIVE:-1}"
export OPENHANDS_SUPPRESS_BANNER="${OPENHANDS_SUPPRESS_BANNER:-1}"

exec "$PYTHON_BIN" -c '
from openhands.sdk import AgentContext
from openhands_cli.stores.agent_store import AgentStore


def offline_context(self):
    return AgentContext(
        skills=[],
        load_user_skills=False,
        load_public_skills=False,
    )


AgentStore._build_agent_context = offline_context
from swebench.openhands_runtime import install_runtime_overrides

install_runtime_overrides()
from openhands_cli.entrypoint import main
main()
' "$@"

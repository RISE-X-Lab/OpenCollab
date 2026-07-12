#!/usr/bin/env bash
set -euo pipefail

REMOTE_ROOT="${OPENCOLLAB_REMOTE_ROOT:-/nfsEDS/dongyh/data/kaka/docker/opencollab}"
OPENHANDS_SITE="${OPENCOLLAB_OPENHANDS_SITE:-$REMOTE_ROOT/tools/openhands-site}"
PYDEPS="${OPENCOLLAB_PYDEPS:-$REMOTE_ROOT/pydeps}"
PYTHON_BIN="${OPENCOLLAB_OPENHANDS_PYTHON:-python3.12}"

if [[ ! -d "$OPENHANDS_SITE/openhands_cli" ]]; then
  echo "OpenHands runtime is missing: $OPENHANDS_SITE" >&2
  exit 2
fi
if [[ -z "${LLM_API_KEY:-}" || -z "${LLM_MODEL:-}" ]]; then
  echo "OpenHands requires LLM_API_KEY and LLM_MODEL" >&2
  exit 2
fi

export PYTHONPATH="${OPENCOLLAB_REMOTE_REPO:-$REMOTE_ROOT}:$OPENHANDS_SITE:$PYDEPS${PYTHONPATH:+:$PYTHONPATH}"
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

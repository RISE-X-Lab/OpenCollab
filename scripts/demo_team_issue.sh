#!/usr/bin/env bash
set -euo pipefail

allow_child_shell=false
for arg in "$@"; do
  if [[ "$arg" == "--allow-local-child-shell" ]]; then
    allow_child_shell=true
  fi
done
if [[ "$allow_child_shell" != true ]]; then
  printf '%s\n' 'This demo requires explicit permission for child agents to run host shell commands.' >&2
  printf '%s\n' 'Run ./scripts/demo_team_issue.sh --allow-local-child-shell to grant it.' >&2
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
FIXTURE_ROOT="$REPO_ROOT/examples/team-issue/workspace"
DEMO_WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/opencollab-team-issue.XXXXXX")"

cp -R "$FIXTURE_ROOT/." "$DEMO_WORKSPACE/"
printf 'Demo workspace: %s\n' "$DEMO_WORKSPACE"

cd "$REPO_ROOT"
exec uv run opencollab \
  --workspace "$DEMO_WORKSPACE" \
  --team-config "$REPO_ROOT/examples/team-issue/team.yaml" \
  --prompt-file "$REPO_ROOT/examples/team-issue/issue.md" \
  --no-worktrees \
  --hold \
  "$@"

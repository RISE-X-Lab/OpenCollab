"""Container-side script used by the direct Pro-Lite evaluator."""

# ruff: noqa: E501

from __future__ import annotations

DIRECT_EVAL_SCRIPT = r"""#!/usr/bin/env bash
set +e
cd /app 2>/dev/null || cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || cd /
export PATH="/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:/root/go/bin:/usr/local/node/bin:/opt/node/bin:/root/.local/share/pnpm:/root/.npm-global/bin:/app/node_modules/.bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1 && command -v corepack >/dev/null 2>&1; then
  corepack enable >/tmp/prolite_corepack.log 2>&1 || true
fi

expected_base_commit="$(cat /eval_input/base_commit)"
: > /eval_output/base_commit.log
base_commit_status=0
if [ -z "$expected_base_commit" ]; then
  echo "missing expected base commit" >> /eval_output/base_commit.log
  base_commit_status=1
elif ! git cat-file -e "$expected_base_commit^{commit}" >> /eval_output/base_commit.log 2>&1; then
  base_commit_status=1
elif ! git reset --hard "$expected_base_commit" >> /eval_output/base_commit.log 2>&1; then
  base_commit_status=1
elif [ "$(git rev-parse HEAD 2>> /eval_output/base_commit.log)" != "$expected_base_commit" ]; then
  base_commit_status=1
fi
echo "$base_commit_status" > /eval_output/base_commit.exit

before_repo_status=99
if [ "$base_commit_status" -eq 0 ]; then
  bash /eval_input/before_repo.sh > /eval_output/before_repo.log 2>&1
  before_repo_status=$?
fi
echo "$before_repo_status" > /eval_output/before_repo.exit

post_before_base_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ]; then
  actual_after_before="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
  if [ "$actual_after_before" = "$expected_base_commit" ]; then
    post_before_base_status=0
  else
    echo "before-repository command changed HEAD to $actual_after_before" >> /eval_output/base_commit.log
    post_before_base_status=1
  fi
fi
echo "$post_before_base_status" > /eval_output/post_before_base.exit

service_bootstrap_status=99
if [ "$post_before_base_status" -eq 0 ]; then
  bash /eval_input/service_bootstrap.sh > /eval_output/service_bootstrap.log 2>&1
  service_bootstrap_status=$?
fi
echo "$service_bootstrap_status" > /eval_output/service_bootstrap.exit

apply_patch_with_fallback() {
  local patch_file="$1"
  local log_file="$2"
  local apply_mode="${3:-strict}"
  local existing_mode="${4:-reject_already_applied}"
  local git_apply_args=(--whitespace=nowarn)
  if [ ! -s "$patch_file" ]; then return 0; fi
  if [ "$apply_mode" = "ignore-space-change" ]; then
    git_apply_args+=(--ignore-space-change)
  fi
  git apply "${git_apply_args[@]}" "$patch_file" > "$log_file" 2>&1
  local status=$?
  if [ "$status" -eq 0 ]; then return 0; fi
  if [ "$existing_mode" = "verify_already_applied" ] && git apply --reverse --check "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1; then
    echo "verified test patch already applied; workspace left unchanged" >> "$log_file"
    return 0
  fi
  if git apply --check --3way "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1; then
    git apply --3way "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1
    status=$?
    if [ "$status" -eq 0 ]; then return 0; fi
  fi
  if command -v patch >/dev/null 2>&1; then
    local patch_args=(--batch --forward -p1)
    if [ "$apply_mode" = "ignore-space-change" ]; then patch_args+=(-l); fi
    if patch "${patch_args[@]}" --dry-run < "$patch_file" >> "$log_file" 2>&1; then
      patch "${patch_args[@]}" < "$patch_file" >> "$log_file" 2>&1
      status=$?
    else
      status=1
    fi
    if grep -Eiq 'Reversed \(or previously applied\) patch detected|Assuming -R' "$log_file"; then
      echo "patch fallback rejected reversed or previously applied patch" >> "$log_file"
      return 1
    fi
  fi
  return "$status"
}

model_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$service_bootstrap_status" -eq 0 ]; then
  model_status=0
  if [ -s /eval_input/model.patch ]; then
    apply_patch_with_fallback /eval_input/model.patch /eval_output/model_patch.log
    model_status=$?
  fi
fi
echo "$model_status" > /eval_output/model_patch.exit

test_status=99
if [ "$model_status" -eq 0 ]; then
  test_status=0
  if [ -s /eval_input/test.patch ]; then
    apply_patch_with_fallback /eval_input/test.patch /eval_output/test_patch.log ignore-space-change verify_already_applied
    test_status=$?
  fi
fi
echo "$test_status" > /eval_output/test_patch.exit

if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$service_bootstrap_status" -eq 0 ] && [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
  cp /eval_input/f2p.command /eval_output/f2p.command
  bash /eval_input/f2p.sh > /eval_output/f2p.log 2>&1
  echo "$?" > /eval_output/f2p.exit
  cp /eval_input/p2p.command /eval_output/p2p.command
  bash /eval_input/p2p.sh > /eval_output/p2p.log 2>&1
  echo "$?" > /eval_output/p2p.exit
else
  echo 99 > /eval_output/f2p.exit
  echo 99 > /eval_output/p2p.exit
fi
exit 0
"""


def direct_eval_script() -> str:
    """Return the immutable container-side evaluation program."""
    return DIRECT_EVAL_SCRIPT


__all__ = ["DIRECT_EVAL_SCRIPT", "direct_eval_script"]

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_RUNNER_EVENTS_PATH = ".arc/runner-events.jsonl"
DEFAULT_TRACEABILITY_DIR = ".arc/traceability"
DEFAULT_PROJECT_DIR = "."
DEFAULT_GIT_USER_NAME = "ARC Bench Agent"
DEFAULT_GIT_USER_EMAIL = "arcbench@example.com"
ARC_GITIGNORE_START = "# >>> arcbench-agent-runtime >>>"
ARC_GITIGNORE_END = "# <<< arcbench-agent-runtime <<<"


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _resolve_under_project(project_dir: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_dir / path


def _paths(
    project_dir: str | None,
    events_path: str | None,
    traceability_dir: str | None,
) -> tuple[Path, Path, Path]:
    resolved_project_dir = (
        Path(
            project_dir
            or os.environ.get("ARCBENCH_OUTPUT_DIR", "").strip()
            or os.environ.get("ARCBENCH_PROJECT_DIR", "").strip()
            or os.environ.get("ARCBENCH_TEMPLATE_DIR", "").strip()
            or DEFAULT_PROJECT_DIR
        )
        .expanduser()
        .resolve()
    )
    runner_events_value = (
        events_path or os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip() or DEFAULT_RUNNER_EVENTS_PATH
    )
    traceability_value = (
        traceability_dir or os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or DEFAULT_TRACEABILITY_DIR
    )
    runner_events_path = _resolve_under_project(resolved_project_dir, runner_events_value)
    traceability_path = _resolve_under_project(resolved_project_dir, traceability_value)
    resolved_project_dir.mkdir(parents=True, exist_ok=True)
    runner_events_path.parent.mkdir(parents=True, exist_ok=True)
    traceability_path.mkdir(parents=True, exist_ok=True)
    return resolved_project_dir, runner_events_path, traceability_path


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _emit_refresh_signal(
    runner_events_path: Path,
    *,
    reason: str,
    submission: bool = False,
    logs: bool = False,
    commit_history: bool = False,
    traceability_selected: bool = False,
    traceability_all: bool = False,
    preview: bool = False,
) -> None:
    _append_jsonl(
        runner_events_path,
        {
            "type": "signal",
            "reason": str(reason or "").strip() or "arcbench_checkpoint",
            "timestamp": _utc_timestamp(),
            "refresh": {
                "submission": bool(submission),
                "logs": bool(logs),
                "commit_history": bool(commit_history),
                "traceability_selected": bool(traceability_selected),
                "traceability_all": bool(traceability_all),
                "preview": bool(preview),
            },
        },
    )


def _git_env() -> tuple[dict[str, str], str, str]:
    env = os.environ.copy()
    user_name = (
        os.environ.get("ARC_GIT_USER_NAME")
        or os.environ.get("GIT_AUTHOR_NAME")
        or os.environ.get("GIT_COMMITTER_NAME")
        or DEFAULT_GIT_USER_NAME
    ).strip()
    user_email = (
        os.environ.get("ARC_GIT_USER_EMAIL")
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or os.environ.get("GIT_COMMITTER_EMAIL")
        or DEFAULT_GIT_USER_EMAIL
    ).strip()
    env["GIT_AUTHOR_NAME"] = user_name
    env["GIT_AUTHOR_EMAIL"] = user_email
    env["GIT_COMMITTER_NAME"] = user_name
    env["GIT_COMMITTER_EMAIL"] = user_email
    return env, user_name, user_email


def _run_git(project_dir: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    env, _, _ = _git_env()
    completed = subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise RuntimeError(stderr)
    return completed


def _configure_identity(project_dir: Path) -> tuple[str, str]:
    _, user_name, user_email = _git_env()
    _run_git(project_dir, ["config", "user.name", user_name])
    _run_git(project_dir, ["config", "user.email", user_email])
    return user_name, user_email


def _ensure_arc_gitignore(project_dir: Path) -> Path:
    gitignore_path = project_dir / ".gitignore"
    managed_block = "\n".join(
        [
            ARC_GITIGNORE_START,
            "backend/node_modules/",
            "frontend/node_modules/",
            "backend/coverage/",
            "frontend/dist/",
            "frontend/dist-ssr/",
            "*.db",
            ".env",
            ".arc/*",
            "!.arc/traceability/",
            "!.arc/traceability/**",
            ARC_GITIGNORE_END,
        ]
    )
    old_content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    start = old_content.find(ARC_GITIGNORE_START)
    end = old_content.find(ARC_GITIGNORE_END)
    if start != -1 and end != -1 and end > start:
        before = old_content[:start].rstrip()
        after = old_content[end + len(ARC_GITIGNORE_END) :].lstrip()
        merged = ""
        if before:
            merged += before + "\n\n"
        merged += managed_block
        if after:
            merged += "\n\n" + after
        content = merged.strip() + "\n"
    elif old_content.strip():
        content = old_content.rstrip() + "\n\n" + managed_block + "\n"
    else:
        content = managed_block + "\n"
    gitignore_path.write_text(content, encoding="utf-8")
    return gitignore_path


def _current_head(project_dir: Path) -> str | None:
    result = _run_git(project_dir, ["rev-parse", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ensure_repo(project_dir: Path, runner_events_path: Path, *, create_initial_commit: bool = True) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    if not (project_dir / ".git").exists():
        _run_git(project_dir, ["init"])
    user_name, user_email = _configure_identity(project_dir)
    _ensure_arc_gitignore(project_dir)
    _emit_refresh_signal(runner_events_path, reason="git_initialized", commit_history=True)
    if create_initial_commit:
        _run_git(project_dir, ["add", "."])
        result = _run_git(project_dir, ["commit", "-m", "init"], check=False)
        if result.returncode == 0:
            _emit_refresh_signal(runner_events_path, reason="git_init_commit", commit_history=True, preview=True)
        elif "nothing to commit" not in (result.stdout + result.stderr).lower():
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git init commit failed")
    _append_jsonl(
        runner_events_path,
        {
            "type": "signal",
            "reason": "git_identity_configured",
            "timestamp": _utc_timestamp(),
            "refresh": {
                "submission": False,
                "logs": False,
                "commit_history": True,
                "traceability_selected": False,
                "traceability_all": False,
                "preview": False,
            },
            "message": f"{user_name} <{user_email}>",
        },
    )


def _commit(project_dir: Path, runner_events_path: Path, message: str) -> bool:
    _run_git(project_dir, ["add", "."])
    result = _run_git(project_dir, ["commit", "-m", message], check=False)
    if result.returncode == 0:
        _emit_refresh_signal(runner_events_path, reason="git_commit", commit_history=True, preview=True)
        return True
    output = (result.stdout + result.stderr).lower()
    if "nothing to commit" in output:
        return False
    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git commit failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage ARC Bench git checkpoints.")
    parser.add_argument("action")
    parser.add_argument("--project-dir")
    parser.add_argument("--events-path")
    parser.add_argument("--traceability-dir")
    parser.add_argument("--message")
    parser.add_argument("--commit")
    parser.add_argument("--no-initial-commit", action="store_true")
    args = parser.parse_args()

    project_dir, runner_events_path, _ = _paths(args.project_dir, args.events_path, args.traceability_dir)
    action = args.action.strip().lower()

    if action == "ensure-repo":
        _ensure_repo(project_dir, runner_events_path, create_initial_commit=not args.no_initial_commit)
        result = {"ok": True, "head": _current_head(project_dir)}
    elif action == "commit":
        if not args.message:
            raise SystemExit("--message is required for commit")
        result = {
            "ok": True,
            "committed": _commit(project_dir, runner_events_path, args.message),
            "head": _current_head(project_dir),
        }
    elif action == "status":
        status = _run_git(project_dir, ["status", "--short"], check=False).stdout
        result = {"ok": True, "status": status}
    elif action == "head":
        result = {"ok": True, "head": _current_head(project_dir)}
    elif action == "reset":
        if not args.commit:
            raise SystemExit("--commit is required for reset")
        _run_git(project_dir, ["reset", "--hard", args.commit])
        _emit_refresh_signal(runner_events_path, reason="git_reset_to_commit", commit_history=True, preview=True)
        result = {"ok": True, "head": _current_head(project_dir)}
    elif action == "restore-worktree":
        _run_git(project_dir, ["reset", "--hard"])
        _emit_refresh_signal(runner_events_path, reason="git_restore_worktree", commit_history=True, preview=True)
        result = {"ok": True, "head": _current_head(project_dir)}
    elif action == "clean-untracked":
        _run_git(project_dir, ["clean", "-fd"])
        _emit_refresh_signal(runner_events_path, reason="git_clean_untracked", commit_history=True, preview=True)
        result = {"ok": True}
    else:
        raise SystemExit(f"Unsupported action: {args.action}")

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

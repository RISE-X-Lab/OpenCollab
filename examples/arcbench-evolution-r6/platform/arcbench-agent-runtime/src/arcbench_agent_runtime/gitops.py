from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .context import RuntimePaths
from .events import EventClient

DEFAULT_GIT_USER_NAME = "ARC Bench Agent"
DEFAULT_GIT_USER_EMAIL = "arcbench@example.com"
ARC_GITIGNORE_START = "# >>> arcbench-agent-runtime >>>"
ARC_GITIGNORE_END = "# <<< arcbench-agent-runtime <<<"


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitClient:
    def __init__(self, paths: RuntimePaths, events: EventClient) -> None:
        self.paths = paths
        self.events = events

    def _get_identity(self) -> tuple[str, str]:
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
        return user_name, user_email

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        user_name, user_email = self._get_identity()
        env["GIT_AUTHOR_NAME"] = user_name
        env["GIT_AUTHOR_EMAIL"] = user_email
        env["GIT_COMMITTER_NAME"] = user_name
        env["GIT_COMMITTER_EMAIL"] = user_email
        return env

    def run(self, args: list[str], *, check: bool = True) -> GitResult:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(self.paths.project_dir),
            env=self._build_env(),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        result = GitResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise RuntimeError(stderr)
        return result

    def configure_identity(self) -> tuple[str, str]:
        user_name, user_email = self._get_identity()
        self.run(["config", "user.name", user_name])
        self.run(["config", "user.email", user_email])
        return user_name, user_email

    def ensure_arc_gitignore(self) -> Path:
        gitignore_path = self.paths.project_dir / ".gitignore"
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

    def ensure_repo(self, *, create_initial_commit: bool = True) -> None:
        self.paths.project_dir.mkdir(parents=True, exist_ok=True)
        git_dir = self.paths.project_dir / ".git"
        if not git_dir.exists():
            self.run(["init"])
        user_name, user_email = self.configure_identity()
        self.ensure_arc_gitignore()
        self.events.notify_commit_history_changed("git_initialized")
        if create_initial_commit:
            self.run(["add", "."])
            result = self.run(["commit", "-m", "init"], check=False)
            if result.returncode == 0:
                self.events.notify_commit_history_changed("git_init_commit", preview=True)
            elif "nothing to commit" not in (result.stdout + result.stderr):
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git init commit failed")
        self.events._emit_traceability_event(
            {
                "type": "signal",
                "reason": "git_identity_configured",
                "refresh": {
                    "submission": False,
                    "logs": False,
                    "commit_history": True,
                    "traceability_selected": False,
                    "traceability_all": False,
                    "preview": False,
                },
                "message": f"{user_name} <{user_email}>",
            }
        )

    def status_porcelain(self) -> str:
        return self.run(["status", "--short"], check=False).stdout

    def add_all(self) -> None:
        self.run(["add", "."])

    def commit(self, message: str) -> bool:
        self.add_all()
        result = self.run(["commit", "-m", message], check=False)
        if result.returncode == 0:
            self.events.notify_commit_history_changed("git_commit", preview=True)
            return True
        output = (result.stdout + result.stderr).lower()
        if "nothing to commit" in output:
            return False
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git commit failed")

    def rollback_last_commit(self, *, hard: bool = False) -> None:
        args = ["reset", "--hard", "HEAD~1"] if hard else ["reset", "--soft", "HEAD~1"]
        self.run(args)
        self.events.notify_commit_history_changed("git_rollback_last_commit", preview=True)

    def reset_to_commit(self, commit_oid: str, *, hard: bool = True) -> None:
        normalized = str(commit_oid or "").strip()
        if not normalized:
            raise ValueError("commit_oid is required")
        args = ["reset", "--hard", normalized] if hard else ["reset", "--soft", normalized]
        self.run(args)
        self.events.notify_commit_history_changed("git_reset_to_commit", preview=True)

    def restore_worktree(self) -> None:
        self.run(["reset", "--hard"])
        self.events.notify_commit_history_changed("git_restore_worktree", preview=True)

    def clean_untracked(self) -> None:
        self.run(["clean", "-fd"])
        self.events.notify_commit_history_changed("git_clean_untracked", preview=True)

    def current_head(self) -> str | None:
        result = self.run(["rev-parse", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

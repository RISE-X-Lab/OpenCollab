from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUNNER_EVENTS_PATH = ".arc/runner-events.jsonl"
DEFAULT_TRACEABILITY_DIR = ".arc/traceability"
DEFAULT_PROJECT_DIR = "."


def _resolve_under_project(project_dir: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_dir / path


@dataclass(frozen=True)
class RuntimePaths:
    project_dir: Path
    runner_events_path: Path
    traceability_dir: Path

    @classmethod
    def from_env(
        cls,
        *,
        project_dir: str | os.PathLike[str] | None = None,
        runner_events_path: str | os.PathLike[str] | None = None,
        traceability_dir: str | os.PathLike[str] | None = None,
    ) -> "RuntimePaths":
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
            runner_events_path
            or os.environ.get("ARCBENCH_RUNNER_EVENTS_PATH", "").strip()
            or DEFAULT_RUNNER_EVENTS_PATH
        )
        traceability_value = (
            traceability_dir or os.environ.get("ARCBENCH_TRACEABILITY_DIR", "").strip() or DEFAULT_TRACEABILITY_DIR
        )
        return cls(
            project_dir=resolved_project_dir,
            runner_events_path=_resolve_under_project(resolved_project_dir, runner_events_value),
            traceability_dir=_resolve_under_project(resolved_project_dir, traceability_value),
        )

    def ensure_parent_dirs(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.runner_events_path.parent.mkdir(parents=True, exist_ok=True)
        self.traceability_dir.mkdir(parents=True, exist_ok=True)

from __future__ import annotations

from dataclasses import dataclass

from .context import RuntimePaths
from .events import EventClient
from .gitops import GitClient
from .traceability import TraceabilityStore


@dataclass
class AgentRuntime:
    paths: RuntimePaths
    events: EventClient
    traceability: TraceabilityStore
    git: GitClient

    @classmethod
    def from_env(
        cls,
        *,
        project_dir: str | None = None,
        runner_events_path: str | None = None,
        traceability_dir: str | None = None,
    ) -> "AgentRuntime":
        paths = RuntimePaths.from_env(
            project_dir=project_dir,
            runner_events_path=runner_events_path,
            traceability_dir=traceability_dir,
        )
        paths.ensure_parent_dirs()
        events = EventClient(paths)
        traceability = TraceabilityStore(paths, events)
        events.set_requirement_state_writer(traceability.upsert_node_state)
        git = GitClient(paths, events)
        return cls(
            paths=paths,
            events=events,
            traceability=traceability,
            git=git,
        )

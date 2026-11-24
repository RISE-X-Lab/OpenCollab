"""Stand in for a dedicated candidate environment in scripted tool tests."""

from opencollab.adapters.candidate_workspace import _CandidateLease
from opencollab.adapters.env import LocalEnvironment


def simulate_candidate_sandbox(monkeypatch) -> None:
    # Keep real Git and fixture test commands while simulating the sandbox metadata.
    monkeypatch.setattr(_CandidateLease, "shares_source_execution", False)
    monkeypatch.setattr(LocalEnvironment, "process_isolated", True)

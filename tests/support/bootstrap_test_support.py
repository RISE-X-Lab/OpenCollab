"""Configuration construction shared by bootstrap integration tests."""

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import SpawnConfig


def spawn_config(*, api_key: str) -> SpawnConfig:
    return SpawnConfig(
        model="gpt-4o",
        provider="openai",
        api_key=api_key,
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(),
        permission_policy=None,
    )

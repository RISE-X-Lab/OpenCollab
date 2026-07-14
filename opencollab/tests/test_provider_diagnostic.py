from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import check_dashscope


async def test_provider_diagnostic_uses_framework_configuration(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            captured["request"] = kwargs
            return SimpleNamespace(content="connected")

    monkeypatch.setattr(
        check_dashscope,
        "build_config",
        lambda workspace: SimpleNamespace(
            model="configured-model",
            provider="configured-provider",
            api_key="configured-key",
            base_url="https://provider.invalid/v1",
            llm_timeout=12.0,
            max_output_tokens=4096,
        ),
    )

    result = await check_dashscope.request_completion(
        "probe",
        workspace=Path("/workspace"),
        client_type=FakeClient,
    )

    assert result == "connected"
    assert captured["client"] == {
        "model": "configured-model",
        "provider": "configured-provider",
        "api_key": "configured-key",
        "base_url": "https://provider.invalid/v1",
        "request_timeout": 12.0,
    }
    assert captured["messages"][-1] == {"role": "user", "content": "probe"}
    assert captured["request"] == {"temperature": 0.0, "max_output_tokens": 256}

"""Unit tests for env-backed runtime configuration."""

from __future__ import annotations

import pytest

from opencollab.bootstrap.config import build_config

_FILTER_ENV = "OPENCOLLAB_FILTER_MESSAGES"


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_FILTER_ENV, raising=False)
    monkeypatch.delenv("OPENCOLLAB_CONFIG_FILE", raising=False)
    monkeypatch.delenv("OPENCOLLAB_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCOLLAB_LLM_TIMEOUT", raising=False)


def test_filter_messages_defaults_off(monkeypatch):
    assert build_config().filter_messages is False


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on"])
def test_filter_messages_truthy_env_values(monkeypatch, raw):
    monkeypatch.setenv(_FILTER_ENV, raw)
    assert build_config().filter_messages is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off"])
def test_filter_messages_falsy_env_values(monkeypatch, raw):
    monkeypatch.setenv(_FILTER_ENV, raw)
    assert build_config().filter_messages is False


def test_filter_messages_surfaces_in_get_config_dict(monkeypatch):
    from opencollab.bootstrap.config import get_config

    monkeypatch.setenv(_FILTER_ENV, "true")
    assert get_config()["filter_messages"] is True


def test_llm_timeout_defaults_to_long_running_provider_window(monkeypatch):
    assert build_config().llm_timeout == 600.0


def test_llm_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "120.5")
    assert build_config().llm_timeout == 120.5


def test_dashscope_api_key_is_supported_as_fallback(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
    assert build_config().api_key == "dashscope-key"


def test_dashscope_base_url_prefers_dashscope_key(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "generic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")

    assert build_config().api_key == "dashscope-key"


def test_dashscope_file_key_beats_generic_export(monkeypatch, tmp_path):
    cfg_file = tmp_path / "dashscope.env"
    cfg_file.write_text(
        "\n".join(
            [
                "OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
                "DASHSCOPE_API_KEY=dashscope-key",
            ]
        )
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "generic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert build_config().api_key == "dashscope-key"


def test_dashscope_file_key_beats_same_name_stale_export(monkeypatch, tmp_path):
    cfg_file = tmp_path / "dashscope.env"
    cfg_file.write_text(
        "\n".join(
            [
                "OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
                "DASHSCOPE_API_KEY=real-file-key",
            ]
        )
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "stale-shell-key")

    assert build_config().api_key == "real-file-key"


def test_anthropic_file_key_beats_generic_export(monkeypatch, tmp_path):
    cfg_file = tmp_path / "anthropic.env"
    cfg_file.write_text(
        "\n".join(
            [
                "OPENCOLLAB_PROVIDER=anthropic",
                "ANTHROPIC_API_KEY=anthropic-file-key",
            ]
        )
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "generic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert build_config().api_key == "anthropic-file-key"


def test_config_repr_does_not_include_api_key(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_API_KEY", "secret-key")
    assert "secret-key" not in repr(build_config())

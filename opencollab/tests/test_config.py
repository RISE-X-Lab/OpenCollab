"""Unit tests for env-backed runtime configuration."""

from __future__ import annotations

import pytest
from opencollab.bootstrap.config import (
    accepted_api_key_envs,
    api_key_env_precedence,
    build_config,
    missing_api_key,
)

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
    monkeypatch.delenv("OPENCOLLAB_TEMPERATURE", raising=False)
    monkeypatch.delenv("OPENCOLLAB_TOP_P", raising=False)


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


def test_temperature_defaults_to_two_tenths(monkeypatch):
    assert build_config().temperature == 0.2


def test_temperature_reads_env(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.7")
    assert build_config().temperature == 0.7


def test_temperature_allows_zero(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0")
    assert build_config().temperature == 0.0


def test_temperature_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "3.0")
    with pytest.raises(Exception):
        build_config()


def test_temperature_surfaces_in_get_config_dict(monkeypatch):
    from opencollab.bootstrap.config import get_config

    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.9")
    assert get_config()["temperature"] == 0.9


def test_top_p_defaults_to_none(monkeypatch):
    # Unset → None so the provider default is used and the request is unchanged.
    assert build_config().top_p is None


def test_top_p_reads_env(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TOP_P", "0.9")
    assert build_config().top_p == 0.9


def test_top_p_empty_env_is_none(monkeypatch):
    # An empty/whitespace value must collapse to None (not crash, not 0.0).
    monkeypatch.setenv("OPENCOLLAB_TOP_P", "  ")
    assert build_config().top_p is None


def test_top_p_allows_zero(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TOP_P", "0")
    assert build_config().top_p == 0.0


def test_top_p_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_TOP_P", "1.5")
    with pytest.raises(Exception):
        build_config()


def test_top_p_surfaces_in_get_config_dict(monkeypatch):
    from opencollab.bootstrap.config import get_config

    # Unset surfaces as None in the dict so cfg.get("top_p") is a clean None.
    assert get_config()["top_p"] is None
    monkeypatch.setenv("OPENCOLLAB_TOP_P", "0.85")
    assert get_config()["top_p"] == 0.85


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


def test_api_key_env_precedence_is_provider_and_endpoint_specific():
    assert api_key_env_precedence("openai")[0] == "OPENCOLLAB_API_KEY"
    assert api_key_env_precedence("anthropic")[0] == "ANTHROPIC_API_KEY"
    assert api_key_env_precedence(
        "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )[0] == "DASHSCOPE_API_KEY"


def test_accepted_api_key_envs_reproduces_hint_order():
    assert accepted_api_key_envs("openai") == ["OPENCOLLAB_API_KEY", "OPENAI_API_KEY"]
    assert accepted_api_key_envs("anthropic") == ["OPENCOLLAB_API_KEY", "ANTHROPIC_API_KEY"]
    assert accepted_api_key_envs(
        "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ) == ["OPENCOLLAB_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY"]


def test_missing_api_key_true_when_no_key_anywhere(monkeypatch):
    assert missing_api_key("openai", None) is True


def test_missing_api_key_false_for_resolved_key(monkeypatch):
    assert missing_api_key("openai", "resolved-key") is False


def test_missing_api_key_treats_whitespace_only_key_as_missing(monkeypatch):
    assert missing_api_key("openai", "   ") is True
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert missing_api_key("openai", None) is True


def test_missing_api_key_honors_dashscope_endpoint(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert missing_api_key("openai", None, base_url) is False

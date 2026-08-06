"""Unit tests for env-backed runtime configuration."""

from __future__ import annotations

import os

import pytest

from opencollab.bootstrap import config as config_mod
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
    for name in (
        _FILTER_ENV, "OPENCOLLAB_CONFIG_FILE",
        "OPENCOLLAB_API_KEY", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY",
        "OPENCOLLAB_LLM_TIMEOUT", "OPENCOLLAB_WIRE_PROTOCOL",
        "OPENCOLLAB_REASONING_EFFORT", "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT", "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
        "OPENCOLLAB_TEMPERATURE", "OPENCOLLAB_TOP_P", "OPENCOLLAB_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


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


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf"])
def test_llm_timeout_rejects_nonpositive_or_nonfinite_env(monkeypatch, raw):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", raw)

    with pytest.raises(Exception):
        build_config()


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


def test_max_output_tokens_defaults_and_reads_env(monkeypatch):
    assert build_config().max_output_tokens == 8192
    monkeypatch.setenv("OPENCOLLAB_MAX_OUTPUT_TOKENS", "32768")
    assert build_config().max_output_tokens == 32768


def test_provider_override_reselects_file_first_api_key(monkeypatch, tmp_path):
    cfg_file = tmp_path / "provider.env"
    cfg_file.write_text(
        "OPENCOLLAB_PROVIDER=openai\n"
        "OPENAI_API_KEY=file-openai\n"
        "ANTHROPIC_API_KEY=file-anthropic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(cfg_file))

    cfg = build_config(overrides={"provider": "anthropic"})

    assert cfg.provider == "anthropic"
    assert cfg.api_key == "file-anthropic"


def test_cli_resolved_config_keeps_max_output_tokens(monkeypatch, tmp_path):
    from opencollab.adapters.cli.config_resolve import resolve_config

    cfg_file = tmp_path / "runtime.env"
    cfg_file.write_text(
        "OPENCOLLAB_API_KEY=k\nOPENCOLLAB_MAX_OUTPUT_TOKENS=32768\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(cfg_file))

    cfg = resolve_config(str(tmp_path), None, None, None, None, None)

    assert cfg["max_output_tokens"] == 32_768


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


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_config_file_rejects_unsafe_or_oversized_input(tmp_path, monkeypatch, kind):
    config = tmp_path / "config.env"
    if kind == "fifo":
        os.mkfifo(config)
    elif kind == "symlink":
        real = tmp_path / "real.env"
        real.write_text("OPENCOLLAB_MODEL=secret\n", encoding="utf-8")
        config.symlink_to(real)
    else:
        config.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(config_mod, "MAX_DOTENV_BYTES", 64)
    monkeypatch.setenv("OPENCOLLAB_CONFIG_FILE", str(config))

    with pytest.raises(ValueError, match="config env|read target exceeds"):
        build_config()


def test_missing_api_key_honors_dashscope_endpoint(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert missing_api_key("openai", None, base_url) is False


def test_responses_configuration_reads_explicit_protocol_reasoning_and_timeouts(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "responses")
    monkeypatch.setenv("OPENCOLLAB_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("OPENCOLLAB_LLM_CONNECT_TIMEOUT", "12")
    monkeypatch.setenv("OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT", "240")
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT", "90")

    config = build_config()

    assert config.wire_protocol == "responses"
    assert config.reasoning_effort == "xhigh"
    assert config.llm_connect_timeout == 12
    assert config.llm_first_event_timeout == 240
    assert config.llm_stream_idle_timeout == 90


def test_responses_configuration_accepts_max_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "responses")
    monkeypatch.setenv("OPENCOLLAB_REASONING_EFFORT", "max")

    assert build_config().reasoning_effort == "max"


@pytest.mark.parametrize("value", ["responses-api", "auto", "unknown"])
def test_unknown_wire_protocol_is_rejected(monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", value)
    with pytest.raises(ValueError, match="wire protocol"):
        build_config()


@pytest.mark.parametrize("value", ["minimal", "ultra", "1"])
def test_unknown_reasoning_effort_is_rejected(monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_REASONING_EFFORT", value)
    with pytest.raises(ValueError, match="reasoning_effort"):
        build_config()

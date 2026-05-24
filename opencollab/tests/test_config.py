"""Unit tests for env-backed runtime configuration."""

from __future__ import annotations

import pytest

from opencollab.bootstrap.config import build_config

_FILTER_ENV = "OPENCOLLAB_FILTER_MESSAGES"


@pytest.fixture(autouse=True)
def _clear_filter_env(monkeypatch):
    monkeypatch.delenv(_FILTER_ENV, raising=False)


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

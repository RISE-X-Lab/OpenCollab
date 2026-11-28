"""Env-file comments preserve quoted values and public startup settings."""

import os

import pytest

from opencollab import OpenCollab
from opencollab.bootstrap.config import load_dotenv


@pytest.mark.parametrize("quote", ["", '"', "'"])
@pytest.mark.parametrize("comment", ["", " # run setting", "\t# run setting"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_public_sdk_reads_commented_settings(tmp_path, monkeypatch, quote, comment, encoding):
    for name in tuple(os.environ):
        if name.startswith("OPENCOLLAB_") or name in {
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY",
            "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
        }:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / ".env").write_text(
        f"OPENCOLLAB_TEMPERATURE={quote}0.7{quote}{comment}\n"
        f"OPENCOLLAB_THINKING={quote}true{quote}{comment}\n"
        f"OPENCOLLAB_MODEL={quote}gpt-4o{quote}{comment}\n",
        encoding=encoding,
    )

    config = OpenCollab(tmp_path).configuration

    assert config["temperature"] == 0.7
    assert config["thinking"] is True
    assert config["model"] == "gpt-4o"

@pytest.mark.parametrize(("raw", "expected"), [
    ("literal#hash", "literal#hash"),
    ("https://endpoint.invalid/v1#route # endpoint", "https://endpoint.invalid/v1#route"),
    ('"literal # hash" # note', "literal # hash"),
    ("'literal # hash' # note", "literal # hash"),
    ('"literal \\\" # hash" # note', 'literal \\\" # hash'),
    ('{"text": "literal # hash"} # note', '{"text": "literal # hash"}'),
    ("# unset", ""),
    ('"" # unset', ""),
])
def test_comment_parser_preserves_literal_hashes(tmp_path, raw, expected):
    path = tmp_path / ".env"
    path.write_text(f"SETTING={raw}\n", encoding="utf-8")

    assert load_dotenv(str(path)) == {"SETTING": expected}

from __future__ import annotations

import json

import pytest

from scripts import _swe_report_io as report_io


def test_report_reader_rejects_symlink_input(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"status": "done"}), encoding="utf-8")
    link = tmp_path / "linked.json"
    link.symlink_to(real)

    value, error = report_io.load_json_with_error(link)

    assert value == {}
    assert error == "unsafe_or_unstable_report_file"


def test_report_reader_maps_a_detected_read_race_to_a_technical_input_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "report.json"
    path.write_text("{}", encoding="utf-8")

    def changed_while_reading(*args, **kwargs):
        raise OSError("input changed while reading")

    monkeypatch.setattr(report_io.safe_files, "read_regular_text", changed_while_reading)

    value, error = report_io.load_json_with_error(path)

    assert value == {}
    assert error == "unsafe_or_unstable_report_file"


def test_report_reader_rejects_documents_above_the_bounded_read_limit(tmp_path, monkeypatch):
    path = tmp_path / "oversized.json"
    path.write_text(json.dumps({"payload": "x" * 100}), encoding="utf-8")
    monkeypatch.setattr(report_io, "MAX_REPORT_BYTES", 16)

    value, error = report_io.load_json_with_error(path)

    assert value == {}
    assert error == "unsafe_or_unstable_report_file"


def test_report_reader_rejects_a_truncated_json_document(tmp_path):
    path = tmp_path / "truncated.json"
    path.write_text('{"status":', encoding="utf-8")

    value, error = report_io.load_json_with_error(path)

    assert value == {}
    assert error == "invalid_report_json"


def test_report_writer_refuses_to_replace_a_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked.json"
    link.symlink_to(real)

    with pytest.raises(OSError, match="not a regular file"):
        report_io.write_json(link, {"status": "done"})

    assert real.read_text(encoding="utf-8") == "{}"

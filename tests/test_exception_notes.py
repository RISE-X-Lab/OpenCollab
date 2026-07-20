from __future__ import annotations

from opencollab.application.exception_notes import add_exception_note


class LegacyException(RuntimeError):
    add_note = None


def test_add_exception_note_populates_legacy_notes_list():
    error = LegacyException("primary")

    assert add_exception_note(error, "cleanup failed") is True
    assert error.__notes__ == ["cleanup failed"]


def test_add_exception_note_appends_multiple_legacy_notes():
    error = LegacyException("primary")

    add_exception_note(error, "first")
    add_exception_note(error, "second")

    assert error.__notes__ == ["first", "second"]


def test_add_exception_note_rejects_non_text_note():
    error = LegacyException("primary")

    try:
        add_exception_note(error, 1)  # type: ignore[arg-type]
    except TypeError as exc:
        assert str(exc) == "exception note must be text"
    else:
        raise AssertionError("non-text note was accepted")

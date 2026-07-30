from labeler import normalize_label


def test_normalizes_a_basic_label():
    assert normalize_label("Release Notes") == "release-notes"


def test_collapses_mixed_unicode_whitespace():
    value = "  Release\t \nNotes\u00a0Draft  "
    assert normalize_label(value) == "release-notes-draft"


def test_whitespace_only_is_empty():
    assert normalize_label(" \t\n ") == ""

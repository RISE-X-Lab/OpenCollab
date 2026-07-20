"""Cross-version exception-note support for Python 3.10 and newer."""

from __future__ import annotations


def add_exception_note(error: BaseException, note: str) -> bool:
    """Attach one diagnostic note without requiring ``BaseException.add_note``.

    Python 3.11 added PEP 678's ``add_note`` method. OpenCollab still supports
    Python 3.10, where ordinary exception instances can retain the compatible
    ``__notes__`` list even though the convenience method is absent.
    """
    if not isinstance(note, str):
        raise TypeError("exception note must be text")
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)
        return True

    notes = getattr(error, "__notes__", None)
    if notes is None:
        notes = []
        try:
            setattr(error, "__notes__", notes)
        except (AttributeError, TypeError):
            return False
    append = getattr(notes, "append", None)
    if not callable(append):
        return False
    append(note)
    return True


__all__ = ["add_exception_note"]

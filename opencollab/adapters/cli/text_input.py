"""Resolve CLI text supplied directly or through a bounded regular file."""

from __future__ import annotations

import typer

from opencollab.adapters.safe_files import read_regular_text


def resolve_text_input(
    text: str | None,
    path: str | None,
    *,
    text_option: str,
    file_option: str,
    max_bytes: int,
) -> str | None:
    """Preserve a nonblank value and report invalid input as a CLI error."""
    if text is not None and path is not None:
        raise typer.BadParameter(f"{text_option} and {file_option} are mutually exclusive.")
    if path is not None:
        if not path.strip():
            raise typer.BadParameter(f"{file_option} path must not be empty.")
        try:
            value = read_regular_text(path, max_bytes=max_bytes)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise typer.BadParameter(f"Cannot read {file_option}. {exc}") from exc
        if not value.strip():
            raise typer.BadParameter(f"{file_option} is empty. {path}")
        return value
    if text is not None:
        if not text.strip():
            raise typer.BadParameter(f"{text_option} must not be empty.")
        return text
    return None


__all__ = ["resolve_text_input"]

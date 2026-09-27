"""Rich text assertions shared by TUI rendering tests."""

from rich.console import Console
from rich.text import Text


def assert_visible_text_has_non_white_style(text: Text) -> None:
    console = Console(color_system="truecolor")
    for offset, char in enumerate(text.plain):
        if char.isspace():
            continue
        style = text.get_style_at_offset(console, offset)
        assert style.color is not None
        assert style.color.name != "white"

"""Safe failure summaries for the local competition entry."""

import os
import re


def failure_message(error):
    text = str(error)
    for name in ("OPENAI_API_KEY", "OPENCOLLAB_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[redacted]", text)[:1500]

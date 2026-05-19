from __future__ import annotations

import json
import os
from typing import Any


class SessionStore:
    allowed_roles = {"system", "user", "assistant", "tool"}

    def save(self, path: str, messages: list[dict[str, Any]]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def load_messages(self, path: str, system_prompt: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError(f"Invalid message at line {lineno}: expected object")
                    role = msg.get("role")
                    if role not in self.allowed_roles:
                        raise ValueError(f"Invalid message role at line {lineno}: {role}")
                    messages.append(msg)

        if not messages:
            messages = [{"role": "system", "content": system_prompt}]
        return messages


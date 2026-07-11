from __future__ import annotations

import json
import os
from typing import Any


class SessionStore:
    allowed_roles = {"system", "user", "assistant", "tool"}

    def save(
        self,
        path: str,
        messages: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_parent(path)
        obj = {**(meta or {}), "messages": messages}
        with open(path, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def save_manifest(self, path: str, manifest: dict[str, Any]) -> None:
        self._ensure_parent(path)
        with open(path, "w") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def load_messages(self, path: str, system_prompt: str) -> list[dict[str, Any]]:
        with open(path) as f:
            text = f.read()

        messages = self._parse(text)
        for lineno, msg in enumerate(messages, 1):
            if not isinstance(msg, dict):
                raise ValueError(f"Invalid message at position {lineno}: expected object")
            role = msg.get("role")
            if role not in self.allowed_roles:
                raise ValueError(f"Invalid message role at position {lineno}: {role}")

        if not messages:
            messages = [{"role": "system", "content": system_prompt}]
        return messages

    def _parse(self, text: str) -> list[dict[str, Any]]:
        """Read the structured-JSON format, falling back to legacy JSONL."""
        if not text.strip():
            return []
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return self._parse_jsonl(text)
        if isinstance(obj, dict):
            return list(obj.get("messages", []))
        if isinstance(obj, list):
            return obj
        raise ValueError("Invalid session file: expected object or array")

    @staticmethod
    def _parse_jsonl(text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                messages.append(json.loads(line))
        return messages

    @staticmethod
    def _ensure_parent(path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


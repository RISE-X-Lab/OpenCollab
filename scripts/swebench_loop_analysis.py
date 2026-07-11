"""Pure message and repetition analysis for the SWE-bench loop monitor."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Collection


def plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if value is None else str(value)


def normalize_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]+`", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def sentences(text: str) -> list[str]:
    chunks = re.split(r"[\n。！？!?]+|(?<=[a-z0-9\)])\.\s+", text)
    return [normalized for chunk in chunks if len(normalized := normalize_text(chunk)) >= 40]


def update_sentence_counter(
    counter: Counter[str],
    text: str,
    *,
    max_sentence_chars: int,
    max_sentence_keys: int,
) -> None:
    for sentence in sentences(text):
        sentence = sentence[:max_sentence_chars]
        if sentence in counter or len(counter) < max_sentence_keys:
            counter[sentence] += 1


def text_report_from_counter(
    sentence_counter: Counter[str],
    recent_texts: list[dict[str, Any]],
) -> dict[str, Any]:
    repeated = [
        {
            "count": count,
            "sha1": hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:12],
            "text": sentence[:500],
        }
        for sentence, count in sentence_counter.most_common()
        if count > 1
    ]
    return {
        "max_repeated_sentence_count": repeated[0]["count"] if repeated else 0,
        "repeated_sentences": repeated[:10],
        "recent_assistant_texts": recent_texts[-3:],
    }


def assistant_text_report(
    messages: list[dict[str, Any]],
    *,
    max_sentence_chars: int,
    max_sentence_keys: int,
) -> dict[str, Any]:
    assistant_texts: list[dict[str, Any]] = []
    sentence_counter: Counter[str] = Counter()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = plain_text(message.get("content"))
        if not text.strip():
            continue
        assistant_texts.append(
            {
                "aid": message.get("_aid"),
                "role": message.get("_agent_role"),
                "source_file": message.get("_source_file"),
                "message_index": message.get("_message_index"),
                "text": text[-2000:],
            }
        )
    for item in assistant_texts:
        update_sentence_counter(
            sentence_counter,
            str(item.get("text") or ""),
            max_sentence_chars=max_sentence_chars,
            max_sentence_keys=max_sentence_keys,
        )
    return text_report_from_counter(sentence_counter, assistant_texts[-3:])


def tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def tool_call_args(call: dict[str, Any]) -> Any:
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[:1000]
    return raw


def truncate_obj(value: Any, limit: int = 1600) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return json.loads(text)
    return text[:limit] + "...[truncated]"


def looks_successful_tool_result(content: str) -> bool:
    head = content.strip().lower()[:300]
    if not head or head.startswith("error:") or "traceback" in head:
        return False
    return re.search(r"\b(exit code|return code):\s*[1-9]", head) is None


def write_and_error_report(
    messages: list[dict[str, Any]],
    *,
    write_tools: Collection[str],
) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    last_write: dict[str, Any] | None = None
    recent_errors: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                calls[call_id] = {
                    "tool": tool_call_name(call),
                    "args": truncate_obj(tool_call_args(call)),
                    "aid": message.get("_aid"),
                    "role": message.get("_agent_role"),
                    "source_file": message.get("_source_file"),
                    "message_index": message.get("_message_index"),
                }
        elif message.get("role") == "tool":
            content = plain_text(message.get("content"))
            call = calls.get(str(message.get("tool_call_id") or ""))
            if content.strip().startswith("Error:") or "Traceback" in content[:1000]:
                recent_errors.append(
                    {
                        "source": "tool_message",
                        "tool": call.get("tool") if call else None,
                        "content": content[:1200],
                    }
                )
            if call and call.get("tool") in write_tools and looks_successful_tool_result(content):
                last_write = {**call, "tool_result": content[:1200]}

    return {
        "last_successful_write": last_write,
        "recent_tool_errors": recent_errors[-3:],
    }

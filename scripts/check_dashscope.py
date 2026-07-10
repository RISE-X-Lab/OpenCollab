from http import HTTPStatus
from pathlib import Path

import requests
from opencollab.bootstrap.config import build_config

WORKSPACE = Path(__file__).resolve().parents[1]
TEST_MODEL_NAME = "kimi-k2.6"


def request_qwen36_plus(prompt):
    config = build_config(str(WORKSPACE))
    api_key = config.api_key
    if not api_key:
        raise ValueError("Missing OPENCOLLAB_API_KEY or DASHSCOPE_API_KEY")

    base_url = config.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    url = f"{base_url.rstrip('/')}/chat/completions"

    if isinstance(prompt, list):
        messages = [{"role": "system", "content": "You are a helpful assistant."}]
        messages += [
            {"role": turn.get("role", "user"), "content": turn.get("content", "")}
            for turn in prompt
        ]
    else:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": str(prompt)},
        ]

    payload = {
        "model": TEST_MODEL_NAME,
        "messages": messages,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code != HTTPStatus.OK:
        raise RuntimeError(f"http status @ {resp.status_code}, body={resp.text}")

    data = resp.json() if resp.content else {}
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


if __name__ == "__main__":
    text = request_qwen36_plus("请用一句话介绍你自己")
    print(text)



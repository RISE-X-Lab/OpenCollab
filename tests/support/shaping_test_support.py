"""Message pairing observations shared by output shaper tests."""


def _orphaned_tool_ids(messages):
    call_ids = {
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls", [])
    }
    result_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    return result_ids - call_ids

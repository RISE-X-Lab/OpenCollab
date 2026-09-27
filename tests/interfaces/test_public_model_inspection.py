"""Public offline model inspection and profile tool attribution."""

from opencollab.models import inspect_model_runtime
from opencollab.tools import profile_tool_names


def test_model_inspection_reports_main_luna_settings_without_a_client():
    snapshot = inspect_model_runtime("gateway/gpt-5.6-luna")
    assert snapshot["capabilities"]["context_window"] == 1_048_576
    assert snapshot["capabilities"]["honors_workflow_thinking_override"] is False
    assert snapshot["history"] == {"trigger": 1_015_576, "target": 761_682, "from": "context_window"}
    assert snapshot["reasoning_request_fields"] is True
    assert snapshot["max_output_token_field"] == "max_completion_tokens"
    assert snapshot["sends_temperature"] is False
    assert snapshot["sends_top_p"] is False
    assert snapshot["pricing_mode"] == "unset"


def test_model_inspection_classifies_local_sample_exceptions():
    snapshot = inspect_model_runtime(
        "unknown-model",
        overflow_samples=[
            {"label": "context", "message": "context length exceeded", "status": 400},
            {"label": "metadata", "message": "String too long in metadata", "status": 400},
        ],
        retry_samples=[
            {"label": "saturation", "message": "Concurrency limit exceeded", "class_name": "APIError"},
            {"label": "application", "message": "Concurrency limit exceeded", "class_name": "ValueError"},
            {"label": "auth", "message": "Concurrency limit exceeded", "class_name": "APIError", "status": 401},
        ],
    )
    assert snapshot["history"]["from"] == "fixed_default"
    assert snapshot["overflow"] == {"context": True, "metadata": False}
    assert snapshot["retry"] == {"saturation": True, "application": False, "auth": False}


def test_profile_default_tools_follow_base_single2_composition():
    assert profile_tool_names("Base") == profile_tool_names("single2")
    assert profile_tool_names("Base") == ("bash", "file_read", "file_write", "apply_patch", "git_diff", "grep")

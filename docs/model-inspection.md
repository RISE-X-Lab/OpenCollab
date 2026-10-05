# Offline model and profile inspection

`opencollab.models.inspect_model_runtime` exposes the installed runtime's model
capability defaults, history shaping thresholds, pricing mode, and error
classification. The function performs local queries and returns plain Python
values. Model requests retain their normal configuration and transport path.

```python
import os

from opencollab.models import inspect_model_runtime
from opencollab.tools import profile_tool_names

settings = inspect_model_runtime(
    os.environ.get("OPENCOLLAB_MODEL", "gpt-4o"),
    overflow_samples=[
        {"label": "context", "message": "context length exceeded", "status": 400}
    ],
    retry_samples=[
        {
            "label": "concurrency",
            "class_name": "APIError",
            "message": "Concurrency limit exceeded for user",
        }
    ],
)
print(settings["capabilities"])
print(settings["history"])
print(settings["overflow"])
print(settings["retry"])
print(profile_tool_names("base"))
```

`capabilities` contains the installed model table's context size and supported
request features. `history` derives its compaction trigger and target from that
table, using fixed defaults when the model has no known context window.
An SDK client's explicit `context_window` override belongs to its effective
configuration and can differ from these model defaults. Read
`OpenCollab.configuration` for the selected client's protocol, context override,
timeouts, and reasoning settings.

`reasoning_request_fields`, `max_output_token_field`, `sends_temperature`, and
`sends_top_p` describe the Chat Completions model defaults. Call-specific
reasoning settings and protocol selection are applied by the runtime request
builder. `pricing_mode` identifies the installed pricing branch, with
`glm-5.2-default` for that model family and `unset` for other models. Custom
price environment variables are applied by the usage ledger in either branch.
Error samples provide a
label, message, optional numeric HTTP status, and optional class name. The class
name labels a locally constructed exception for the existing classifier.
`overflow` and `retry` map each supplied label to its classification.

`profile_tool_names` returns a profile's default coding tools. The run's
`agent_tool_names` metric records the tools actually selected for that run,
including explicit caller choices. Team inspection uses the public
`opencollab.teams` helpers to read declared roles, card text identities,
profiles, and tool bundles from a selected team configuration.

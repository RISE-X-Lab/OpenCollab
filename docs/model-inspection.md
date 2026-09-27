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
    os.environ["OPENCOLLAB_MODEL"],
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
print(settings["retry"])
print(profile_tool_names("base"))
```

The request-field flags describe the Chat Completions request builder.
Call-specific protocol choices and overrides are recorded with run metadata.
Pricing mode follows the current pricing environment. Error samples provide a
label, message, optional numeric HTTP status, and optional class name. The class
name labels a locally constructed exception for the existing classifier.

`profile_tool_names` returns a profile's default coding tools. The run's
`agent_tool_names` metric records the tools actually selected for that run,
including explicit caller choices. Team inspection uses the public
`opencollab.teams` helpers to read declared roles, card text identities,
profiles, and tool bundles from a selected team configuration.

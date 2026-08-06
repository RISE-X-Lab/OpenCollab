# Configs

Runtime configuration lives in this directory.

Create `configs/.env` from the example:

```bash
cp configs/.env.example configs/.env
```

OpenCollab loads config in this order:

1. Process environment variables
2. `configs/.env`
3. Legacy `.env`
4. Built-in defaults

Use `OPENCOLLAB_CONFIG_FILE=/path/to/file.env` to point OpenCollab at a specific
env file.

## Model Settings

OpenCollab supports both Chat Completions and Responses on the OpenAI client
path. Select the wire protocol explicitly. Existing compatible services use
`chat_completions` by default.

Environment variable example:

```bash
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_WIRE_PROTOCOL=chat_completions
export OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENCOLLAB_MODEL=glm-5.1
export OPENCOLLAB_API_KEY=<your-api-key>
export OPENCOLLAB_LLM_TIMEOUT=600
```

Equivalent `configs/.env` values:

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_WIRE_PROTOCOL=chat_completions
OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENCOLLAB_MODEL=glm-5.1
OPENCOLLAB_API_KEY=<your-api-key>
OPENCOLLAB_LLM_TIMEOUT=600
```

For DashScope-compatible mode, `DASHSCOPE_API_KEY` is also accepted and is
preferred over generic API-key variables for DashScope base URLs.

## Responses API

Set `OPENCOLLAB_WIRE_PROTOCOL=responses` for a Responses-compatible service.
The adapter sends system guidance as `instructions`, replays typed output
items locally, returns tool output with the original `call_id`, and requests
`store=false`. Stateless requests include encrypted reasoning content so later
turns can replay the exact reasoning and tool-call items. The adapter does not
infer the protocol from a model name and does not fall back to Chat Completions
after a protocol error.

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_WIRE_PROTOCOL=responses
OPENCOLLAB_REASONING_EFFORT=medium
OPENCOLLAB_LLM_CONNECT_TIMEOUT=30
OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=180
OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180
```

Supported reasoning efforts are `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, and `max`. A service may support only a subset. An unsupported value
is reported as a provider error and never changed automatically.

## Model capability metadata

Compatibility differences are recorded in
`opencollab.adapters.llm.types.model_capabilities` and consumed by the provider
and workflow adapters. Generic runtime code does not branch on product names.

| Exact model id | Context window | Forced tool choice | Per-role thinking override |
| --- | ---: | --- | --- |
| `kimi-for-coding` | 262,144 | Falls back to `auto` | Keeps global thinking enabled |

Models without an exact entry use provider-neutral defaults and the
best-effort context-window families in the same module.

## Sampling

`OPENCOLLAB_TEMPERATURE` sets the LLM sampling temperature for every agent.
It defaults to `0.2`; `0.0` is fully deterministic. The value must be in the
range `0.0`–`2.0`.

```dotenv
OPENCOLLAB_TEMPERATURE=0.2
```

A team file may override the temperature per role via a `temperature:` field on
the role (see [Team](#team) below). A role that leaves it unset inherits this
global value; a role override of `0.0` is honored (not treated as "unset").

## Display

The TUI retains a separate stream for every agent, starts on the Lead (agent 0),
and switches focus with Tab/Shift+Tab both during a turn and at the main prompt.
The prompt redraws the selected agent's complete history collected during the
current TUI session without changing the current input buffer. The switch order
includes configured roles marked `available`; their view stays empty until the
role spawns, then follows the new live agent automatically.

`OPENCOLLAB_FILTER_MESSAGES` remains accepted for compatibility, but no longer
controls event retention or the selected-agent view. Both values are lossless.

```dotenv
OPENCOLLAB_FILTER_MESSAGES=true
```

## Team

Define a multi-agent team — per-role prompts, model overrides, per-role
`temperature:` overrides, tool allowlists, and a directed spawn/message
topology — in a YAML file:

```bash
cp configs/team.example.yaml configs/team.yaml
uv run opencollab --team-config configs/team.yaml --workspace .
```

OpenCollab never discovers a team file by conventional filename. It selects a
team only through one of these explicit inputs, in priority order:

1. CLI `--team-config /path/to/team.yaml` or SDK `team(config=...)`
2. Process environment variable `OPENCOLLAB_TEAM_FILE=/path/to/team.yaml`
3. The built-in single `lead` configuration

Merely creating `configs/team.yaml` does not activate it. With no explicit team
file, the built-in `lead` may still spawn any ad-hoc role. See
`team.example.yaml` for the schema (lead/analyst/coder/reviewer plus a
`topology` graph). An explicitly selected file that is missing or unsafe fails
fast instead of falling back to the built-in team.

## Validation

The final resolved configuration is validated by a Pydantic model. `budget`
must be a positive integer; `llm_timeout` must be a positive number of seconds;
`temperature` must be within `0.0`–`2.0`; blank `api_key` and `base_url` values
are treated as unset.

Do not commit `configs/.env` or any file containing real API keys.

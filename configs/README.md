# Configs

Runtime configuration lives in this directory.

Create `configs/.env` from the example.

```bash
cp configs/.env.example configs/.env
```

OpenCollab loads config in the following order.

1. Process environment variables
2. `configs/.env`
3. Legacy `.env`
4. Built-in defaults

Use `OPENCOLLAB_CONFIG_FILE=/path/to/file.env` to point OpenCollab at a specific
env file.

## Model Settings

OpenCollab supports OpenAI-compatible APIs through the OpenAI client path. Set
`provider=openai` and a compatible `base_url` for those providers.

Set the shell environment variables directly.

```bash
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENCOLLAB_MODEL=glm-5.1
export OPENCOLLAB_API_KEY=<your-api-key>
export OPENCOLLAB_LLM_TIMEOUT=600
```

The same settings can be written to `configs/.env`.

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENCOLLAB_MODEL=glm-5.1
OPENCOLLAB_API_KEY=<your-api-key>
OPENCOLLAB_LLM_TIMEOUT=600
```

API-key fallback is provider and endpoint specific:

| Route | Resolution order |
| --- | --- |
| OpenAI-compatible, non-DashScope | `OPENCOLLAB_API_KEY`, then `OPENAI_API_KEY` |
| Native Anthropic | `ANTHROPIC_API_KEY`, then `OPENCOLLAB_API_KEY` |
| DashScope-compatible base URL | `DASHSCOPE_API_KEY`, then `OPENCOLLAB_API_KEY` |

Keys from another provider are not used as fallbacks. Process-environment
values beat the same variable in an env file, and blank values are ignored.

## Model capability metadata

Compatibility differences are recorded in
`opencollab.adapters.llm.types.model_capabilities` and consumed by the provider
and workflow adapters. Those adapters handle product-specific behavior.

| Exact model id | Context window | Forced tool choice | Per-role thinking override |
| --- | ---: | --- | --- |
| `kimi-for-coding` | 262,144 | Falls back to `auto` | Keeps global thinking enabled |

Unlisted models use provider-neutral defaults and the
best-effort context-window families in the same module.

## Sampling

`OPENCOLLAB_TEMPERATURE` sets the LLM sampling temperature for every agent.
It defaults to `0.2`. A value of `0.0` is fully deterministic. The value must be
in the range `0.0`–`2.0`.

```dotenv
OPENCOLLAB_TEMPERATURE=0.2
```

A team file may override the temperature per role via a `temperature:` field on
the role (see [Team](#team) below). A role that leaves it unset inherits this
global value. A role value of `0.0` overrides the global setting.

## Thinking

OpenCollab adds no thinking configuration by default, leaving that behavior to
the provider. Enable an explicit configuration globally with
`OPENCOLLAB_THINKING=true` or set `thinking: true` on one role. Parameters use
the selected provider's native request shape.

OpenAI-compatible endpoints receive `OPENCOLLAB_THINKING_PARAMS` through
`extra_body`.

```dotenv
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"enable_thinking":true}
```

The native Anthropic provider accepts manual or adaptive thinking. Manual
thinking requires a budget of at least 1,024 tokens and below
`OPENCOLLAB_MAX_OUTPUT_TOKENS`.

```dotenv
OPENCOLLAB_PROVIDER=anthropic
OPENCOLLAB_MAX_OUTPUT_TOKENS=32768
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","budget_tokens":16000}}
```

Adaptive thinking can include an effort setting supported by the selected
Anthropic model.

```dotenv
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}
```

OpenCollab omits `temperature` from native Anthropic thinking requests and
requires the provider default for `top_p`. Manual thinking uses automatic tool
selection when a caller requests a forced tool. Signed thinking blocks and
their original ordering survive tool calls. Invalid or incompatible thinking
parameters fail before the provider request is sent.

## Display

The TUI retains a separate stream for every agent, starts on the Lead (agent 0),
and switches focus with Tab/Shift+Tab both during a turn and at the main prompt.
The prompt redraws the selected agent's complete history collected during the
current TUI session without changing the current input buffer. The switch order
includes configured roles marked `available`. Their view stays empty until the
role spawns and then follows the new live agent automatically.

OpenCollab still accepts `OPENCOLLAB_FILTER_MESSAGES` for compatibility. Event
retention and the selected-agent view are lossless for either value.

```dotenv
OPENCOLLAB_FILTER_MESSAGES=true
```

## Team

Define a multi-agent team in a YAML file. The file can set role prompts, model
and temperature overrides, tool allowlists, and the directed spawn and message
topology.

```bash
cp configs/team.example.yaml configs/team.yaml
uv run opencollab --team-config configs/team.yaml --workspace .
```

OpenCollab selects a team through these inputs, in priority order.

1. CLI `--team-config /path/to/team.yaml` or SDK `team(config=...)`
2. Process environment variable `OPENCOLLAB_TEAM_FILE=/path/to/team.yaml`
3. The built-in single `lead` configuration

Select `configs/team.yaml` through one of these inputs to activate it. With no
selected team file, the built-in `lead` may spawn any ad-hoc role. See
`team.example.yaml` for the schema (lead/analyst/coder/reviewer plus a
`topology` graph). A selected file that is missing or unsafe raises an error.

## Validation

The final resolved configuration is validated by a Pydantic model. `budget`
must be a positive integer. `llm_timeout` must be a positive number of seconds.
`temperature` must be within `0.0`–`2.0`. Blank `api_key` and `base_url` values
are treated as unset. Unknown configuration and team-schema keys are rejected
instead of being silently ignored.

Do not commit `configs/.env` or any file containing real API keys.

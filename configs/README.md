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

## Streaming chat completions

`OPENCOLLAB_LLM_STREAM_CHAT=true` consumes OpenAI-compatible chat completions
as a stream. It is off by default, and off means the request body is exactly
the one the non-streaming path has always sent — neither `stream` nor
`stream_options` is added — so runs recorded before and after this setting
existed remain comparable.

Turn it on to record the model's reasoning: several endpoints, DeepSeek among
them, return `reasoning_content` **only** over the streamed format, so a
non-streamed request pays for the thinking and receives none of the text.
While streaming, recorded reasoning is kept out of the outbound history: it
reaches the trajectory, but is not echoed back to the model on the next turn.

Streaming reuses `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` and
`OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` (both 180s) — the request timeout only
bounds a single socket read once a response is streamed. A stream that ends
without a `finish_reason`, or one whose endpoint reports no token usage, is
an error rather than a silently partial answer.

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
and temperature overrides, tool allowlists, each role's token allowance, the
context policy every session runs, and the directed spawn and message topology.

```bash
cp configs/team.example.yaml configs/team.yaml
uv run opencollab --team-config configs/team.yaml --workspace .
```

CLI `--team-config /path/to/team.yaml` or SDK `team(config=...)` selects an
explicit team first. Otherwise, OpenCollab reads the process environment
variable `OPENCOLLAB_TEAM_FILE=/path/to/team.yaml`. The fallback is the built-in
Self-Collaboration team with an `analyst` entry, a `coder`, and a `tester`.

The built-in topology allows the Analyst to spawn the Coder and Tester. The
Coder and Tester each work within their own tool bundle. To add a role such as
`reviewer`, declare it and its topology edges in a team file, then select that
file through one of the explicit inputs. See `team.example.yaml` for the schema.
A selected file that is missing or unsafe raises an error.

Two optional entries hold a team's resources fixed in the file rather than at
the call site:

- `budget: { tokens: N }`, at the top level or on a role, gives each role its
  own token allowance. Allowances are independent: each agent is held to its
  own, and the team's total is their sum. Once one role has an allowance every
  role must, the roster must be prebuilt, and a run that is also handed a
  `budget` refuses to start. Without the entry, the team shares one pool.
- `context:` names the context policy. `default`, the value when omitted, is
  the full compaction pipeline; `no_history_compaction` keeps only the cap on
  each tool result, which `tool_result_budget` can set.

A Team run's trajectory opens with the declared organization: every role with
its model, tools and allowance, and every edge, so a role that never acts still
appears. A run started with `team()` gets its own `run_id`, which its
trajectory, `team.json` and the result's metrics share. `opencollab.teams` reads the same facts from a file
before a run (`declared_role_budgets`, `declared_context_policy`).

`team.collab.yaml` is a ready-made three-role team (Analyst, Coder, Tester) that
hands work over rather than doing it in one seat, with every role prompt inline.
It requires a prebuilt roster, which the CLI cannot ask for: started through
`uv run opencollab --team-config`, it seats the Analyst alone and produces a
solo run that reads like a team's. Run it with `scripts/run_collab_team.py`, the
SDK (`prebuild_team=True`), or the evaluation harness. See
[the team handoff](../docs/2026-08-31-collab-team.md).

## Validation

The final resolved configuration is validated by a Pydantic model. `budget`
must be a positive integer. `llm_timeout` must be a positive number of seconds.
`temperature` must be within `0.0`–`2.0`. Blank `api_key` and `base_url` values
are treated as unset. Unknown configuration and team-schema keys are rejected
instead of being silently ignored.

Do not commit `configs/.env` or any file containing real API keys.

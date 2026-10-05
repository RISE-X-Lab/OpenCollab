# Configs

Runtime configuration lives in this directory.

Create `configs/.env` from the example.

```bash
cp configs/.env.example configs/.env
```

Explicit CLI options and SDK constructor settings take precedence over loaded
settings. For other values, process environment variables precede env-file
values and built-in defaults. Env files are searched in workspace order, first
`configs/.env` and then legacy `.env`, followed by the same paths in the current
working directory when it differs. Earlier files provide values first, and later
files fill missing variables.

Use `OPENCOLLAB_CONFIG_FILE=/path/to/file.env` to select one env file in place
of that search. The selected file must exist and be a regular UTF-8 file.
`OpenCollab(...)` resolves these settings once when the client is constructed.

## Model Settings

OpenCollab supports OpenAI-compatible APIs through the OpenAI client path. Set
`provider=openai` and a compatible `base_url` for those providers.

Set the shell environment variables directly.

```bash
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENCOLLAB_MODEL=glm-5.1
export OPENCOLLAB_API_KEY='your-api-key' # pragma: allowlist secret
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

API-key fallback is provider and endpoint specific.

| Route | Resolution order |
| --- | --- |
| OpenAI-compatible, non-DashScope | `OPENCOLLAB_API_KEY`, then `OPENAI_API_KEY` |
| Native Anthropic | `ANTHROPIC_API_KEY`, then `OPENCOLLAB_API_KEY` |
| DashScope-compatible base URL | `DASHSCOPE_API_KEY`, then `OPENCOLLAB_API_KEY` |

Keys from another provider are not used as fallbacks. Process-environment
values beat the same variable in an env file, and blank values are ignored.
Provider-specific key priority applies across both sources. For example, a
`DASHSCOPE_API_KEY` in the selected env file precedes an exported generic
`OPENCOLLAB_API_KEY`. An explicit CLI or SDK `api_key` takes precedence over
both. Base URLs also accept `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` for the
selected provider after `OPENCOLLAB_BASE_URL`.

## Wire protocol

`OPENCOLLAB_WIRE_PROTOCOL=chat_completions` is the default for OpenAI-compatible
providers. Select `responses` for a compatible endpoint exposing the Responses
API. The native `anthropic` provider uses Messages and requires the default
`chat_completions` setting in this shared configuration field.

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_WIRE_PROTOCOL=responses
OPENCOLLAB_MODEL=gpt-4o
```

Select a model and base URL supported by the endpoint. `OPENCOLLAB_CONTEXT_WINDOW`
overrides the runtime's best-effort model context window with a positive token
count. `OPENCOLLAB_MAX_OUTPUT_TOKENS` sets the output allowance for each model
call and defaults to 8,192.

`OPENCOLLAB_REASONING_EFFORT` accepts `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, or `max`. The request builder applies the setting through the selected
protocol and model capabilities. Use [offline model inspection](../docs/model-inspection.md)
to inspect the installed model defaults, and `OpenCollab.configuration` to read
the effective settings of a client.

## Timeouts and retries

Transport settings use seconds and apply to model calls.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCOLLAB_LLM_TIMEOUT` | `600` | Provider request timeout, including individual socket reads during streaming |
| `OPENCOLLAB_LLM_CONNECT_TIMEOUT` | `30` | Connection timeout |
| `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` | `180` | Wait for the first streamed event |
| `OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` | `180` | Wait between streamed events |
| `OPENCOLLAB_LLM_MAX_RETRIES` | `3` | Retries after a retryable provider failure |
| `OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET` | `0` | Optional shared allowance for failed attempts and retry delays |

`OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=0` leaves retry attempts governed by
`OPENCOLLAB_LLM_MAX_RETRIES`. A positive value adds the shared time allowance.
Retries use backoff and provider `Retry-After` where available. A whole-run
deadline is selected separately through SDK `timeout=` or a workflow role's
`timeout=`. SDK `cleanup_timeout=` bounds shutdown after execution ends.

## Streaming chat completions

`OPENCOLLAB_LLM_STREAM_CHAT=true` consumes OpenAI-compatible chat completions
as a stream. It is off by default. Enabling it adds `stream` and `stream_options`
to the Chat Completions request. Responses streaming follows the selected
model's capability settings.

Turn it on when the endpoint returns reasoning text through streamed deltas.
Returned reasoning is recorded in the trajectory. Chat history preserves
`reasoning_content` for thinking continuations or models whose capabilities
require it, including the configured DeepSeek reasoning models. Other Chat
requests use the ordinary assistant content and tool calls.

Streaming uses the first-event and stream-idle timeouts above alongside the
provider transport timeout. A Chat stream requires a terminal `finish_reason`
and reported token usage to complete successfully.

## Model capability metadata

Compatibility differences are recorded in
`opencollab.adapters.llm.types.model_capabilities` and consumed by the provider
and workflow adapters. Those adapters handle product-specific behavior.

| Exact model id | Context window | Forced tool choice | Per-role thinking override |
| --- | ---: | --- | --- |
| `kimi-for-coding` | 262,144 | Falls back to `auto` | Keeps global thinking enabled |
| `qwen3.8-flash` | 983,616 | Falls back to `auto` | Supports role override |
| `deepseek-v4-flash` | 1,048,576 | Falls back to `auto` | Keeps global thinking enabled |

Unlisted models use provider-neutral defaults and the
best-effort context-window families in the same module.

## Sampling

`OPENCOLLAB_TEMPERATURE` sets the LLM sampling temperature for every agent.
It defaults to `0.2`. A value of `0.0` selects the lowest configured temperature.
The value must be in the range `0.0`–`2.0`.

```dotenv
OPENCOLLAB_TEMPERATURE=0.2
```

A team file may override the temperature per role via a `temperature:` field on
the role (see [Team](#team) below). A role that leaves it unset inherits this
global value. A role value of `0.0` overrides the global setting.

`OPENCOLLAB_TOP_P` sets nucleus sampling in the range `0.0`–`1.0`. Leaving it
unset uses the provider default. Request builders omit sampling fields where
the selected model or native thinking mode requires that behavior.

## Thinking

OpenCollab adds no thinking configuration by default, leaving that behavior to
the provider. Enable an explicit configuration globally with
`OPENCOLLAB_THINKING=true` or set `thinking: true` on one role. Parameters use
the selected provider's native request shape.

OpenAI-compatible Chat Completions requests receive
`OPENCOLLAB_THINKING_PARAMS` through `extra_body`. Responses requests use
`OPENCOLLAB_REASONING_EFFORT` for their supported reasoning controls.

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

A finite session reserves input tokens before selecting the call's output
allowance. Manual thinking requires room for its thinking budget plus answer
tokens. When the remaining allowance falls below that minimum, the session
stops with a budget reason and retains its completed tool results and usage.

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
uv run opencollab team init team.yaml
uv run opencollab team show --team-config team.yaml
uv run opencollab --team-config team.yaml --workspace .
```

`team init` writes an editable copy of the current built-in team and preserves
an existing destination file. `team show` loads the selected configuration and
prints its entry role, tools and topology without starting agent sessions.
Both setup commands work before API credentials are configured.

CLI `--team-config /path/to/team.yaml` or SDK `team(config=...)` selects an
explicit team first. Otherwise, OpenCollab reads the process environment
variable `OPENCOLLAB_TEAM_FILE=/path/to/team.yaml`. The fallback is the built-in
Self-Collaboration team with an `analyst` entry, a `coder`, and a `tester`.

The built-in topology allows the Analyst to spawn the Coder and Tester. The
Coder and Tester each work within their own tool bundle. To add a role such as
`reviewer`, declare it and its topology edges in a team file, then select that
file through one of the explicit inputs. See `team.example.yaml` for the schema.
A selected file that is missing or unsafe raises an error.

`budget: { tokens: N }`, at the top level or on a role, gives each role its
own token allowance. Each agent is held to its allowance, and the team's total
is their sum. Once one role has an allowance every role must have one. Select
`prebuild_team=True` and let the team file supply the budget. Teams with this
setting reject a separate SDK `budget=`. A team with omitted allowances shares
the run's token pool.

`context:` names the context policy. The default policy applies a per-tool-result
allowance and pressure-triggered history compaction. `no_history_compaction`
retains the cap on each tool result, which `tool_result_budget` can set.

A Team run's trajectory opens with the declared organization, listing every
role with its model, tools and allowance, and every edge. A role that never acts
still appears. A run started with `team()` gets its own `run_id`, which its
trajectory, `team.json` and the result's metrics share. `opencollab.teams` reads
the same facts from a file before a run (`declared_role_budgets`,
`declared_context_policy`).

`team.collab.yaml` is a ready-made three-role team (Analyst, Coder, Tester) with
every role prompt inline. It requires a prebuilt roster. The root CLI starts the
Analyst alone, and that role's prompt asks it to report the missing peers and
stop. Run this file with `scripts/run_collab_team.py`, the SDK
(`prebuild_team=True`), or the evaluation harness. See
[the team handoff](../docs/2026-08-31-collab-team.md).

## Usage records

`OPENCOLLAB_API_USAGE_LOG=/path/to/usage.jsonl` enables the append-only provider
usage ledger. Records retain input, output, cache, and reasoning counters along
with an `estimated` flag and the reported raw usage. Total tokens are input plus
output. Cached input is already included in input tokens.

Custom cost reporting uses `OPENCOLLAB_INPUT_USD_PER_MTOK`,
`OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK`,
`OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK`, and
`OPENCOLLAB_OUTPUT_USD_PER_MTOK`. The `glm-5.2` pricing branch uses the
corresponding `GLM_*` variables. These settings affect ledger cost estimates.
Session budgets use token counters.

## Validation

The final resolved configuration is validated by a Pydantic model. `budget`
must be a positive integer. `llm_timeout` must be a positive number of seconds.
`temperature` must be within `0.0`–`2.0`. Blank `api_key` and `base_url` values
are treated as unset. Unknown configuration and team-schema keys are rejected
when supplied as SDK configuration keys or team YAML fields. Env files may
contain other applications' variables, which the runtime leaves unused.

Do not commit `configs/.env` or any file containing real API keys.

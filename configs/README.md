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

OpenCollab supports OpenAI-compatible APIs through the OpenAI client path. Set
`provider=openai` and a compatible `base_url` for those providers.

Environment variable example:

```bash
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENCOLLAB_MODEL=glm-5.1
export OPENCOLLAB_API_KEY=<your-api-key>
```

Equivalent `configs/.env` values:

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENCOLLAB_MODEL=glm-5.1
OPENCOLLAB_API_KEY=<your-api-key>
```

## Team

Define a multi-agent team — per-role prompts, model overrides, tool allowlists,
and a directed spawn/message topology — in `configs/team.yaml`:

```bash
cp configs/team.example.yaml configs/team.yaml
```

OpenCollab resolves the team file in this order:

1. `OPENCOLLAB_TEAM_FILE=/path/to/team.yaml`
2. `<workspace>/configs/team.yaml`
3. `<cwd>/configs/team.yaml`

With no team file, the default is a single `lead` agent that may spawn any
ad-hoc role. See `team.example.yaml` for the schema (lead/analyst/coder/reviewer
plus a `topology` graph).

## Validation

The final resolved configuration is validated by a Pydantic model. `budget`
must be a positive integer; blank `api_key` and `base_url` values are treated as
unset.

Do not commit `configs/.env` or any file containing real API keys.

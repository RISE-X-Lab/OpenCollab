# ARC-Bench r6 Weave workflow

[Chinese guide](README.zh-CN.md)

This example adapts the r6 competition harness to OpenCollab's built-in Weave (`weave`) workflow. The core workflow schedules independent Single2 instances over one application workspace. Each requirement group and repair round has a fresh conversation. The instances hand off application files, requirement cards, progress and executable check reports.

The competition adapter reads the public YAML and builds requirement cards and resource hints. It supplies the original prompts, tools, browser checks and repair evidence to `run_weave`. The core workflow orders groups, allocates execution opportunities, expands soft allowances within the active session and controls bounded repair. GitHub collaboration and spreadsheet adapters execute real browser and SQLite checks. The platform runtime, model compatibility wrapper and `.arc` reports remain in this example. [SOURCES.md](SOURCES.md) records the supplied package and its attributed compatibility observations.

## Build a competition submission ZIP

Treat this example directory as the submission project. Its existing `main.py`
becomes the ZIP's root entry. The exporter includes an OpenCollab wheel built
from the same Git revision, so the submission installs the matching Weave core.
The wheel contains the OC runtime; the example contains the competition adapter,
checks, prompts and resources.

Install Python 3.10+, Git and `uv` on the packaging machine. From the OC repository
root, run the following command. Choose an output path that does not exist.

```bash
python examples/arcbench-evolution-r6/build_submission.py \
  --output /tmp/weave-submission.zip
```

The exporter reads committed `HEAD`. To export another committed version, add
`--ref <commit-or-tag>`. It exports tracked example files, builds the matching OC
wheel and writes `requirements.txt` and `SUBMISSION.json`. Local credentials,
virtual environments and untracked run artifacts stay outside the archive.
Building the wheel may download build dependencies.

Unzip the archive into a new directory. `main.py`, `requirements.txt`, `wheels/`,
`platform/`, `arcbench_r6/`, `template/` and `skills/` are directly at its root.
The competition platform installs and starts it from that directory.

```bash
python -m pip install -r requirements.txt
python main.py "$TASK_DIR" --output-dir "$APP_WORKSPACE" --type web
```

The platform supplies the task and application directories plus `MODEL`,
`OPENAI_BASE_URL` and `OPENAI_API_KEY`. Installation can download Python runtime
dependencies. The Linux/WSL2, Node, application dependencies and browser setup
below still apply. Upload this ZIP using the original competition submission
procedure. The packaging script runs from an OC Git checkout; the exported
`main.py` runs directly from the extracted submission project.

## Install and run

Use Linux or WSL2 with Python 3.10 or newer, Node 22.12 or newer and Git. Supply the matching public requirement YAML, inherited application and database. The application needs `frontend/package.json` with a build script and `backend/package.json` with a start script serving `/api/health` on `PORT`. Install the application's dependencies, `@playwright/test` and its Chromium before the competition run.

From the OpenCollab repository root, install the current SDK and the supplied platform runtime.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install ./examples/arcbench-evolution-r6/platform/arcbench-agent-runtime
```

Set `MODEL` or `OPENCOLLAB_MODEL` explicitly. Provide the competition's OpenAI-compatible Chat endpoint through `OPENAI_BASE_URL` and its credential through `OPENAI_API_KEY`. The corresponding `OPENCOLLAB_` names are also accepted. The model wrapper preserves recorded reasoning in Chat continuations and uses automatic tool selection where the original competition alias required it. Explicitly disabled thinking and disabled tools retain their meanings.

```bash
python examples/arcbench-evolution-r6/main.py /absolute/path/to/requirements \
  --output-dir /absolute/path/to/application-copy --type web
```

A requirement directory is searched for `requirements.yaml`, `requirements.yml`, then `task.yaml`. A direct YAML file is also accepted. Platform defaults use `ARCBENCH_TASK_DIR`, `ARCBENCH_OUTPUT_DIR` and `ARCBENCH_TASK_TYPE`. If both application package files are absent, missing files from the supplied starter template are copied into the workspace. The template is a generic skeleton; competition inputs supply the inherited application and data.

## Original run settings

| Setting | Default |
| --- | ---: |
| Total token allowance | 16,000,000 |
| Main soft and hard allowances | 12,000,000 and 14,000,000 |
| Reserved final repair allowance | 2,000,000 |
| Cumulative main steps | 200 |
| Scheduling time and coding-phase setting | 100 and 80 minutes |
| Maximum response and context window | 32,768 and 1,000,000 tokens |
| Final repair | At most three rounds, each at most 60 steps, 600 seconds and 2,000,000 tokens |

The original check and cleanup reserves determine each group's actual window. Two repair rounds without observable progress stop repair. Soft allowance growth retains the same session and stays within its actual hard grant. The example uses explicit workflow limits, independently of another run's unbounded environment setting.

`OPENCOLLAB_BUDGET`, `ARC_MAIN_BUDGET`, `ARC_MAIN_HARD_BUDGET`, `ARC_REPAIR_RESERVE`, `ARC_WALL_LIMIT_MIN`, `ARC_AGENT_LIMIT_MIN`, `OPENCOLLAB_MAX_STEPS` and `OPENCOLLAB_CONTEXT_WINDOW` retain their competition meanings. The optional `ARC_HISTORY_TRIGGER_TOKENS` is disabled by default. The native workflow implements r6's default grouped route; setting `ARC_GROUPED_MAIN=0` returns a configuration error.

## Python Workflow entry

Add this example directory to Python's import path. Install the platform runtime as above. Both this entry and the competition CLI call the same decorated workflow.

```python
import asyncio
from dataclasses import asdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path("examples/arcbench-evolution-r6").resolve()))
from opencollab import OpenCollab
from arcbench_r6.model import CompetitionModel
from arcbench_r6.settings import Settings
from arcbench_r6.workflow import weave

async def run():
    settings = Settings.from_env()
    client = OpenCollab(
        "/absolute/path/to/application-copy",
        model="your-competition-model",
        config={"budget": settings.budget,
                "context_window": settings.context_window,
                "max_output_tokens": settings.max_output_tokens,
                "wire_protocol": "chat_completions"},
    )
    model_client = client.create_model_client()
    try:
        return await client.workflow(
            weave,
            {"requirements": "/absolute/path/to/task.yaml", "settings": asdict(settings)},
            agent_profile="single2", concurrency=1, budget=settings.budget,
            max_steps=settings.max_steps, limit_mode="explicit",
            llm=CompetitionModel(model_client), trace=False,
            cleanup_timeout=settings.cleanup_seconds,
        )
    finally:
        await model_client.close()

result = asyncio.run(run())
print(result.output)
```

## Results and continuation

Preflight prepares dependencies and generated source in the implementation workspace. Verification executes uncached installation, build and checks in disposable copies. Multiple reaction scenarios use independent migrated database copies while reusing one installation and frontend build. Their exact persisted counts and restart checks remain part of the full result.

The current application report is `.arc/checks/outcome.json`. `delivery_ok` describes complete local verification; the platform supplies `official_score`. SDK execution status and application correctness are separate. The CLI returns zero when this invocation completes and local delivery verification passes.

Use `--resume` to continue the original run ID, requirement document, input snapshot, returned usage and phase records. Generated groups pending checks proceed to verification. Finished groups retain their outcome. Interrupted stages retain their candidate and handoff and continue with the remaining allowance. A repair round that already started remains part of the original three-round maximum. For a direct SDK continuation, pass the original `run_id` and the remaining token allowance to `client.workflow`.

Use `--fresh` to declare a new input application explicitly. Prior reports move to `.arc/history/`; the dependency-install cache remains reusable only when its manifest matches. One run owns an application workspace at a time. Cancellation waits for managed generation, verification and preflight cleanup before releasing it.

Reports include requirement coverage, phase results, cumulative session usage, the original input snapshot, repair history and platform events. Failed new inputs replace the current outcome with the new run's failure and preserve the earlier result as history. Credentials are supplied by the caller and excluded from failure summaries.

## Execute the checks

[CHECKS.md](CHECKS.md) documents the independent checker entry and its reports. Regular Python tests cover the native SDK workflow, repair, continuation, failure state, local HTTP request compatibility and cancellation. The browser job runs the actual Node adapters, SQLite and Chromium, including a complete native workflow with local model responses.

```bash
uv run pytest -q examples/arcbench-evolution-r6/tests
npm ci --prefix examples/arcbench-evolution-r6
cd examples/arcbench-evolution-r6
npx playwright install chromium
OPENCOLLAB_TEST_PYTHON=/path/to/OpenCollab/.venv/bin/python npm run test:browser
```

The test application and model responses are local fixtures. Reproducing the competition score uses the original GitHub and Sheet task inputs, inherited applications, model configuration and the platform's external evaluator.

# IO2Code reconstruction

[Chinese guide](README.zh-CN.md)

Reconstruct a hidden program from its input/output behaviour alone. The
benchmark is IO2CodeBench from the FromIO paper ([arXiv 2605.15334](https://arxiv.org/abs/2605.15334),
code at [JiaruQian/IO2Code](https://github.com/JiaruQian/IO2Code)): 125 small
Python functions, each with 8 visible examples and 15 held-out tests. A task
counts as solved only when all 15 held-out tests pass.

Everything specific to the benchmark lives in this directory. The exploration
protocol is OpenCollab's built-in [`reconstruct` workflow](../../docs/reconstruct.md);
OpenCollab also provides the agent sessions, tools, Docker environment, budgets
and traces.

```text
main.py        binds an IO2Code task to the reconstruct workflow, scores held-out tests, writes result.json
tasks.py       loads a task from the IO2Code checkout (oracle + held-out stay in this process)
tools.py       query_oracle, check_candidate, candidate execution inside the container
workflow.py    a free "probe, code, check" workflow and the benchmark's fixed mode (the non-protocol modes)
tests/         offline unit tests (no model, no Docker, no checkout)
```

## Prerequisites

- Linux or macOS with Docker. The agent works inside a `python:3.12-slim`
  container with no network; candidate programs are executed in that same
  container, never on the host.
- An IO2Code checkout at the revision this example was validated against:

  ```bash
  git clone https://github.com/JiaruQian/IO2Code.git
  git -C IO2Code checkout ef15827
  export IO2CODE_ROOT=$PWD/IO2Code
  ```

  The checkout is read only by this process (oracle functions, held-out
  tests, input generators). It is never mounted into the container.
- A model configured for OpenCollab in `configs/.env` (see
  [configs/README.md](../../configs/README.md)): `OPENCOLLAB_MODEL`,
  `OPENCOLLAB_API_KEY`, `OPENCOLLAB_BASE_URL` and, for Responses-only models,
  `OPENCOLLAB_WIRE_PROTOCOL=responses`. Set `OPENCOLLAB_MAX_OUTPUT_TOKENS` high
  enough for reasoning models (65536 was used for the results below). Models
  that re-encrypt reasoning in the terminal Responses event (gpt-6-luna) need
  `OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=1` in the process
  environment.

## Run

From the repository root:

```bash
uv run python examples/io2code_oc/main.py --list
uv run python examples/io2code_oc/main.py Abs_Current Extra_PolishRPN \
  --random-share 0.5 --coverage-gate --docker python:3.12-slim
```

Each task gets a fresh git workspace and container under
`examples/io2code_oc/runs/<run-name>/<task>/`, with `result.json`
(status, held-out score, queries, tokens, the final program), `oracle_queries.json`
(every input/output pair the agent was given, with its source) and OpenCollab's
`artifacts/` trace. A task that crashes or is skipped still gets a
`result.json` (status `crashed`/`skipped`), so `summary.json`, rebuilt from
every `result.json` in the run directory, keeps the full task count.
`--concurrency N` runs tasks side by side; `--resume` skips finished tasks and
retries crashed or skipped ones.

## Modes

| `--mode` | evidence the agent gets | who queries the oracle |
|---|---|---|
| `paper-active` (default) | 2 examples to start, a new batch (+2 each time) only after the candidate passes every known example; 5 consecutive promotions stop early; at most 50 iterations | the built-in `reconstruct` workflow, following the paper's Section 4.6 protocol; the agent proposes inputs |
| `fixed` | the benchmark's 8 visible examples | nobody |
| `active` | nothing up front | the agent, through `query_oracle`, up to `--max-queries` |

`paper-active` is the built-in `reconstruct` workflow, which mirrors
`run_active_dio_agent.py` from the paper's repository (batch sizes, promotion
rule, early stop, retry of rejected proposals, the stage score that picks the
final program). The program-writing step is an OpenCollab agent session instead
of the paper's evolutionary DIO-Agent. `main.py` only supplies the binding: the
oracle, the benchmark's input generator, candidate execution in the container,
the tools, and the held-out scoring afterwards.

## Optional disciplines (the reconstruct workflow's arguments)

| flag | rule |
|---|---|
| `--random-share 0.5` | half of every batch comes from the benchmark's own input generator instead of the agent, so part of the evidence is independent of the agent's hypothesis |
| `--coverage-gate` | a promotion counts towards early stop only once the known inputs cover the workflow's structural case categories (empty, singleton, boundary values, duplicates, ...); missing categories are requested from the agent |
| `--memorisation-gate` | a candidate that embeds known inputs verbatim is not a verified pass |
| `--complexity-gate` | a pass by an over-complex candidate needs more evidence before it counts |
| `--domain-check` | agent-proposed inputs outside the task's numeric range are rejected |
| `--rea` | all of the above with `--random-share 0.5` |

## Results

[RESULTS.md](RESULTS.md) lists the 115 non-multimodal tasks under the
benchmark's fixed protocol (90/115), the paper protocol (104/115), and the paper
protocol with the first two disciplines (113/115), one run each on `gpt-6-luna`.
The 10 multimodal tasks need a vision model at runtime and are not covered.

## Trust model

The oracle and the held-out tests never leave this process. The agent's
container has no network and sees only its workspace. Candidate programs run
inside that container with a 30 s timeout, and their outputs are compared with
the same rules as IO2Code's evaluator. Task names are never shown to the agent
(many of them spell out the rule).

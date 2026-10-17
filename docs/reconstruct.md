# Reconstruct: a hidden program from input/output behaviour

`reconstruct` is a built-in workflow for black-box program reconstruction: a
hidden program can only be queried with inputs, and the agent must write a
program that behaves the same way. The workflow implements the active-exploration
protocol of the FromIO paper ([arXiv 2605.15334](https://arxiv.org/abs/2605.15334),
Section 4.6) and adds optional verification disciplines borrowed from
[rea](https://github.com/morluto/rea).

## Protocol

1. The workflow obtains `initial_batch` observed examples (default 2).
2. Each iteration starts one coding-agent session with every observed example
   and the previous feedback; the agent writes `candidate.py` in the workspace.
3. The workflow runs the candidate on every observed example. A full pass is a
   *promotion*; a failing candidate gets the failures as feedback and no new
   examples.
4. After a promotion the workflow asks the agent to propose the next batch of
   inputs (`batch_step` more than the previous batch), validates the proposal
   (exact count, no repeats, accepted by the oracle; a rejected proposal is
   retried up to `propose_attempts` times) and queries the hidden program.
5. `early_stop` consecutive promotions, `max_iterations` iterations, a failed
   proposal or an exhausted token budget end the loop. The budget check runs
   after the current candidate has been evaluated and archived, so the last
   program the agent wrote is never lost.
6. Every iteration's candidate is archived in `<workspace>/.oc_candidates/`; the
   final `candidate.py` is the program archived by this run with the best stage
   score on the final example set (accuracy minus the paper's complexity and
   hard-coding penalties). Files left in that directory by an earlier run are
   ignored.

The workflow queries the hidden program; the agent never does. One coding
session replaces one DIO-Agent evolution iteration of the paper.

## Disciplines

| argument | default | rule |
|---|---|---|
| `random_share` | `0.0` | this fraction of every batch comes from `ReconstructionTask.random_inputs` (a source independent of the agent, such as the benchmark's input generator) instead of the agent |
| `coverage_gate` | `false` | a promotion counts towards early stop only once the observed inputs cover the structural case categories for the task's input kind (empty, short, long, zero, negative, repeated ...); missing categories are requested from the agent; after three batches on the same missing set they stop blocking |
| `memorisation_gate` | `false` | a candidate that embeds three or more observed inputs verbatim is not a verified pass: it is asked to rewrite on the same evidence, twice, before a batch is granted anyway |
| `complexity_gate` | `false` | a pass by a candidate whose complexity penalty is at least 0.8 is granted its batch but not counted as a promotion, up to three times |
| `domain_check` | `false` | proposed inputs with numbers outside `[lo, hi]` are rejected and the range is stated in the prompt |

On IO2CodeBench with `gpt-6-luna`, the plain protocol solved 104 of 115 tasks and
`random_share=0.5` with `coverage_gate` solved 113; the other three disciplines
fixed two tasks and lost two others at a much higher token cost. See the
[IO2Code example](../examples/io2code_oc/README.md).

## Binding a hidden program

The binding is a `ReconstructionTask`:

```python
from opencollab.builtin_workflows import ReconstructionTask

task = ReconstructionTask(
    input_type="list of integers",
    input_constraints="Valid input is a list of integers.",
    function_name="solve",                 # candidate.py must define solve(input_data)
    workspace=workspace,                    # the agent's workspace (candidate.py lives here)
    oracle=hidden_program,                  # input -> output; raise for an invalid input
    evaluate=run_candidate,                 # async (program_path, inputs) -> [{"ok", "output"|"error"}]
    tools=[*builtin_tools("bash", "file_read", "file_write", "apply_patch"), check_candidate],
    random_inputs=generator,                # (k, exclude) -> fresh inputs; needed for random_share
    on_example=record,                      # (input, output, source) for the caller's own log
    kind="list", lo=-1000, hi=1000,         # input kind and numeric range for the gates
)
```

`evaluate` should run the candidate where the agent's own code runs (for
example inside the agent's Docker container), never in the harness process.
`kind` is one of `list`, `two_lists`, `int`, `pair`, `string`, `points` or
`other`; `other` disables the coverage gate and the domain check.

Workflow arguments are recorded with the run's artifacts, so they must be
JSON-serializable: `args["task"]` is a handle to the binding, not the object.

### SDK

`register_task` keeps the binding in this process and returns a token that the
workflow consumes:

```python
from opencollab import OpenCollab
from opencollab.builtin_workflows import reconstruct, register_task

result = await OpenCollab(workspace, environment=container).workflow(
    reconstruct,
    {"task": register_task(task), "random_share": 0.5, "coverage_gate": True},
    budget=5_000_000, max_steps=100, artifacts=run_dir / "artifacts",
)
```

### CLI

`args["task"]` may name a factory as `module:callable`; it is called with
`args["task_args"]` and must return a `ReconstructionTask`:

```bash
uv run opencollab workflow run reconstruct \
  --args '{"task": "my_bench.binding:make_task", "task_args": {"name": "Abs_Current"}, "random_share": 0.5}'
```

## Result

| field | meaning |
|---|---|
| `stop_reason` | `early_stop`, `max_iterations`, `proposer_failed` or `budget_exhausted` |
| `iterations`, `promotions` | coding sessions run, full passes among them |
| `known_examples`, `random_inputs` | observed examples, how many came from `random_inputs` |
| `examples` | every observed `[input, output, source]` |
| `events` | per-iteration record: known, passed, score, promoted, uncovered categories, memorised inputs, complexity |
| `final_program`, `final_score` | the archived candidate copied to `candidate.py` and its stage score |
| `coverage_uncovered_at_end` | categories still missing when the loop ended |

Scoring against held-out tests is the caller's job; the workflow never sees them.

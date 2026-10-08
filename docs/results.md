# Benchmark results

Six harnesses run GPT-5.6-Luna at `max` reasoning effort on SWE-bench Pro,
Terminal-Bench 2.1, DeepSWE, and SWE-bench Pro v2 HARD-51. This snapshot was
updated on October 8, 2026. The Duo results use the OC 0.9 series.
[OC (Base)](single2.md) is a single agent. [OC (Duo)](duo.md) produces two
isolated solutions, compares their evidence, and adopts one.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/benchmark-results-dark.svg">
    <img src="../assets/benchmark-results-light.svg" alt="Final results for six harnesses on four benchmarks. Duo scores 68.91% on SWE-bench Pro, 83.15% on Terminal-Bench 2.1, 65.49% on DeepSWE, and 90.20% on HARD-51. Columns show Pass@1, average tokens, estimated average cost, and cache hit. HARD-51 is tied with Base, Codex CLI, and Mini-SWE-Agent." width="980">
  </picture>
</p>

Duo has the highest Pass@1 on SWE-bench Pro, Terminal-Bench 2.1, and DeepSWE.
On HARD-51, Duo, Base, Codex CLI, and Mini-SWE-Agent each pass 46 of 51 tasks.
Base has the lowest average token use and estimated cost on all four
benchmarks. Duo uses fewer tokens than Claude Code on each benchmark.
Its estimated DeepSWE cost is $8.42 per task, compared with Claude Code's
$7.66, reflecting their different cache hit rates.

## Metrics and coverage

The [summary data](benchmark-results.json) contains every row's pass and fail
counts, token use, estimated cost, and cache hit rate. The benchmark sizes are
193 tasks for SWE-bench Pro, 89 for Terminal-Bench 2.1, 113 for DeepSWE, and 51
for HARD-51.

Pass@1 divides the accepted pass count by the full task count. Average tokens
include input and output, with cached tokens already included in input and
reasoning tokens included in output. Usage follows the accepted generation
history, including every Duo role. Restored histories are counted once.
Cache hit is the ratio of total cached input to total input.

Average cost is the estimate `T × (0.2975 − 0.2185 × h)`, where `T` is average
input plus output in millions of tokens and `h` is the aggregate cache hit
fraction. Calls whose usage was unavailable retain unknown provider charges.
The recorded usage for all six HARD-51 groups covers all 51 tasks in each
group. Earlier comparison rows retain their reported usage aggregates.

## Duo against Base, task by task

The following comparisons match the final accepted results by original task
ID. Counts cover every task in the corresponding dataset. The last column
is the exact two-sided sign test on the tasks that only one configuration
passed.

| Benchmark | Both pass | Only Duo | Only Base | Both fail | p |
| --- | ---: | ---: | ---: | ---: | ---: |
| Terminal-Bench 2.1 | 65 | 9 | 6 | 9 | 0.6072 |
| DeepSWE | 53 | 21 | 10 | 29 | 0.0708 |
| HARD-51 | 45 | 1 | 1 | 4 | 1.0000 |

On DeepSWE, Duo gains 9.73 percentage points over Base, with 21 Duo-only
passes and 10 Base-only passes. On Terminal-Bench 2.1 it gains 3.37 points.
On HARD-51 both configurations pass 46 tasks, with one task unique to each.

## Network access on Terminal-Bench 2.1

A Terminal-Bench 2.1 task gets internet access when its own `allow_internet`
flag allows it. Some generation attempts on that benchmark received material
that gives away the answer. Every pass obtained with such material was withdrawn or
replaced by a fresh run before scoring, and the table above reports the results
after this handling.

## Run OpenCollab on a benchmark

[OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval) runs agents on
software-engineering benchmarks through OpenCollab's public Python API. It
creates an isolated workspace for each task, records the patch, runs the
official tests, and keeps the commands and reports needed to inspect the
result. It currently supports SWE-bench Pro-Lite and provides a generic task
runner for other evaluation workloads.

The default [OC Base agent](single2.md) maps to Single2 through the public
`agent(...)` entry. For a collaborative evaluation, follow the
[Duo with Single2 quick start](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start).
It covers matching OC/OCE 0.9.x installations, benchmark images, a Responses
model endpoint, and one-task official evaluation with
`oc-eval duo --config /path/to/g22.json --indices 1 --workers 1`.
`oc-eval g22` remains an alias for that command. The same
configuration runs a batch and keeps per-task patches, trajectories, and
official test reports together.

The [evaluation guide](https://github.com/RISE-X-Lab/OpenCollab-Eval#supported-environment)
explains how to run it, and the
[integrity guide](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/docs/evaluation-integrity.md)
explains how results are checked.
[MIGRATION.md](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/MIGRATION.md)
records the boundary between the two repositories.

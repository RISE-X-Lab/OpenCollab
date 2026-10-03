# Benchmark results

Five harnesses run the same model, GPT-5.6-Luna at `max` reasoning effort, on
SWE-bench Pro, Terminal-Bench 2.1, and DeepSWE. [OC (Base)](single2.md) is a single agent. [OC (Duo)](duo.md)
produces two isolated solutions, compares their evidence, and adopts one.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/benchmark-results-dark.svg">
    <img src="../assets/benchmark-results-light.svg" alt="Cross-harness benchmark results. OC Duo leads Pass@1 with 64.25% on SWE-bench Pro, 83.15% on Terminal-Bench 2.1, and 69.91% on DeepSWE. Columns show Pass@1, average tokens, estimated average cost, and cache hit." width="980">
  </picture>
</p>

OC (Duo) has the highest Pass@1 on all three benchmarks. OC (Base) uses the
fewest tokens and costs the least on all three. OC (Duo) spends more than Base,
because it runs two complete solving processes and selects between their
candidates, and it still costs less than Claude Code on each benchmark.

## Duo against Base, task by task

Duo is a Workflow: its code issues every handoff, so both coders run on every
task. The table pairs Duo with Base on each task and gives each outcome as a
share of the benchmark's tasks. The last column is the exact two-sided sign
test on the tasks that only one of them passed.

| Benchmark | Both pass | Only Duo | Only Base | Both fail | p |
| --- | ---: | ---: | ---: | ---: | ---: |
| Terminal-Bench 2.1 | 76.40% | 6.74% | 3.37% | 13.48% | 0.51 |
| DeepSWE | 51.33% | 18.58% | 4.42% | 25.66% | 0.0025 |

On DeepSWE, Duo gains 14.2 points over Base and the difference is significant.
On Terminal-Bench 2.1 it gains 3.4 points, too few discordant tasks to separate
the two.

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
It covers matching OC/OCE 0.8 installations, benchmark images, a Responses
model endpoint, and one-task official evaluation with
`oc-eval g22 --config /path/to/g22.json --indices 1 --workers 1`. The same
configuration runs a batch and keeps per-task patches, trajectories, and
official test reports together.

The [evaluation guide](https://github.com/RISE-X-Lab/OpenCollab-Eval#supported-environment)
explains how to run it, and the
[integrity guide](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/docs/evaluation-integrity.md)
explains how results are checked.
[MIGRATION.md](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/MIGRATION.md)
records the boundary between the two repositories.

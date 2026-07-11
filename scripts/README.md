# Scripts

Helper scripts for running OpenCollab from the repository root.

## `start_opencollab.sh`

Bootstraps the local Python environment and starts OpenCollab.

```bash
scripts/start_opencollab.sh
```

By default, this starts chat mode:

```bash
opencollab/.venv/bin/opencollab chat --workspace <repo-root>
```

Start team mode instead:

```bash
scripts/start_opencollab.sh team
```

Pass extra OpenCollab CLI arguments after the mode:

```bash
scripts/start_opencollab.sh team --trace
scripts/start_opencollab.sh chat --yolo
```

Show script help:

```bash
scripts/start_opencollab.sh --help
```

## Configuration

The script expects runtime config in:

```text
configs/.env
```

If that file is missing, the script creates it from:

```text
configs/.env.example
```

Then it exits so you can add a real API key:

```dotenv
OPENCOLLAB_API_KEY=<your-api-key>
```

For DashScope-compatible mode, `DASHSCOPE_API_KEY` is also accepted and is
preferred over generic API-key variables for DashScope base URLs.

The API key can also come from the process environment:

```bash
export OPENCOLLAB_API_KEY=<your-api-key>
scripts/start_opencollab.sh
```

Do not commit `configs/.env`.

## Environment Bootstrap

If `opencollab/.venv/bin/opencollab` already exists, the script uses it.

Otherwise it tries, in order:

1. `uv venv opencollab/.venv` and `uv pip install --python opencollab/.venv/bin/python -e opencollab`
2. `python3 -m venv opencollab/.venv` and `opencollab/.venv/bin/pip install -e opencollab`

If neither `uv` nor `python3` is available, the script exits with an error.

## Generated Files

OpenCollab may create local runtime state under:

```text
.opencollab/
```

That directory is ignored by git.

## SWE-bench team eval (`run_team_batch.sh` → `start_team_run.sh`)

Generate predictions by running the OpenCollab team inside each official
`sweb.eval` container. `run_team_batch.sh` is the batch driver;
`start_team_run.sh` runs one instance. Grading is done separately on the
**scoring side** (`/path/to/swebench-eval/`, see its `README.md`).

### Full run

```bash
scripts/run_team_batch.sh \
  --dataset SWE-bench/SWE-bench_Lite --split test \
  --output /path/to/swebench-eval/predictions-team.jsonl \
  --timeout 1500
```

Re-running with the same `--output` skips instances that already have a
non-empty patch (add `--retry-empty` to also redo empty-patch ones).

### Subset eval

There is **no separate subset script** — a subset is just `--instance-ids`
(a CSV of instance ids). Subsets are kept as files under `evals/`, one id per
line. To run one:

```bash
scripts/run_team_batch.sh \
  --dataset SWE-bench/SWE-bench_Lite --split test \
  --output /path/to/swebench-eval/predictions-team-nohints.jsonl \
  --instance-ids "$(awk 'NF && $1!~/^#/{print $1}' evals/subset_hints_ablation.txt | paste -sd,)" \
  --timeout 1500 \
  -- --no-hints
```

- `awk ... | paste -sd,` turns the subset file into the CSV `--instance-ids`
  wants (it skips `#` comments and takes column 1, so marked/2-column files work).
- `-- --no-hints` passes everything after `--` through to `start_team_run.sh`;
  `--no-hints` omits the issue's `hints_text` from the task prompt. **Drop the
  whole `-- --no-hints` to keep hints.** Each instance log prints `Hints: on/off`.
- Use a **distinct `--output`** per experiment so results never collide, and a
  matching `run_id` when grading.

### Grading the subset

```bash
cd /path/to/swebench-eval
.venv/bin/python grade_team.py predictions-team-nohints.jsonl oc-team-nohints
```

Only instances present in the jsonl are graded, so grading is a subset too.

### The hints-ablation mixed subset (`evals/subset_hints_ablation.txt`)

This is the **mixed subset** used for the hints on/off experiment. It is one
file of **30 instances** in a 2-column `id<TAB>group` format (`#` comments
allowed), deliberately mixing two groups so the result is interpretable:

| group | count | baseline (`oc-team`, **with** hints) | why it's in the set |
|---|---|---|---|
| `unresolved` | 20 | all **unsolved** (0/20) | the subject group — does removing hints keep them unsolved, or were hints irrelevant? |
| `control` | 10 | all **solved** (10/10) | the control — if these regress without hints, the no-hints pipeline itself is suspect; if they hold, the unresolved result is trustworthy |

Both groups are run with `--no-hints` (the no-hints arm). The **with-hints arm
is not re-run** — the `oc-team` baseline already supplies those numbers for all
30 ids (`swebench-eval/opencollab-team.oc-team.json`).

How to read the no-hints results against the baseline:

- **control** mostly still solved → pipeline is healthy; trust the unresolved row.
- **control** collapses → no-hints prompt/pipeline is broken; the unresolved row
  means nothing (fix and re-run).
- **unresolved** gains some solves without hints → those weren't blocked by
  missing hints. Stays 0 → hints (or harder reasoning) were the bottleneck.

The 30 ids were sampled evenly across repos from the baseline's `unresolved_ids`
(20) and `resolved_ids` (10), with no overlap between the two groups.

To build a new subset from a report's id lists:

```bash
cd /path/to/swebench-eval
python3 - <<'PY' > /path/to/OpenCollab/evals/subset_new.txt
import json, collections
r = json.load(open("opencollab-team.oc-team.json"))
ids = sorted(r["unresolved_ids"])           # or resolved_ids / empty_patch_ids
g = collections.defaultdict(list)
for i in ids: g[i.split("__")[0]].append(i)
pick = []; keys = sorted(g, key=lambda k: -len(g[k])); idx = {k: 0 for k in keys}
while len(pick) < 20:
    for k in keys:
        if idx[k] < len(g[k]) and len(pick) < 20:
            pick.append(g[k][idx[k]]); idx[k] += 1
for p in sorted(pick): print(p)
PY
```

### Subset quick reference

| Goal | What to change |
|---|---|
| Different subset | swap the `--instance-ids` file/CSV |
| Keep hints (baseline arm) | drop `-- --no-hints` |
| Avoid collisions | new `--output` file + new grading `run_id` |
| Only first N | add `--limit N` |
| Resume from an id | add `--start-from <id>` |

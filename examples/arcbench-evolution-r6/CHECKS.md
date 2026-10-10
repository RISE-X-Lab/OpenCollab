# ARC-Bench r6 Weave checks

This example checks a prepared Web application against public ARC-Bench requirements. Its checker layer comes from the r6 submission. It includes GitHub and spreadsheet scenario adapters, SQLite preservation checks, isolated builds and restarts, failure reports, and fresh-copy stability replays. Source attribution is recorded in [SOURCES.md](SOURCES.md).

Supply an application workspace containing `frontend/package.json`, `backend/package.json`, the inherited database, installed application dependencies, and the matching public requirements YAML. The backend must support `npm run start`, a `PORT` environment variable and `/api/health`. The frontend must support `npm run build`. Browser checks resolve `@playwright/test` from the application's backend and use an installed Chromium. `ARC_BROWSER_EXECUTABLE` can select an existing compatible browser executable explicitly. Python requires PyYAML. Node 22.12 or newer runs the fixture application.

The first invocation captures the inherited SQLite data and test content before editing. The capture uses SQLite's backup API, including committed WAL data. Each verification copies the application to a temporary directory and restores the original database snapshot into that disposable copy. A matching existing dependency cache shares installed modules. Other installs run inside each copy, using npm's download cache and retaining generated source for that verification. Browser contexts are independent while ordinary scenarios execute in order. When multiple reaction scenarios are requested, each uses its own copy of the migrated application and database, reusing the completed installation and build. Each reaction copy verifies its writes through a backend restart. Application mutations remain in disposable copies, which are removed after verification.

```bash
python examples/arcbench-evolution-r6/check.py /path/to/application-copy \
  --requirements /path/to/public-requirements.yaml --initialize
```

Use `--requirement-id` repeatedly to specify the original requirements being delivered. Omitting it selects all atomic requirements in the supplied task. The report records both requested and mapped scenarios. Each scenario is associated with its original requirement ID and ordinal, preserving the feature name for readable reports. Unknown adapters, absent scenario inputs, repeated results and unexecuted scenarios remain unverified. Full delivery success requires the requested scenario set to pass. Requested persistence needs actual restart evidence.

Subsequent checks read the existing snapshot. A second initialization fails with an input error so resumed work continues to compare against the original data. Missing or damaged snapshots produce an unverified result and preserve the candidate application.

```bash
python examples/arcbench-evolution-r6/check.py /path/to/application-copy --confirm-stability
```

`verification.json`, `scenario-ledger.json`, browser reports, backend logs and stability records are written under the application workspace's `.arc/checks/`. The CLI exits successfully only when its current verification passes. These reports contain local executable evidence. Platform evaluation supplies the official score.

The Python integration API consists of `write_public_checks(document, workspace, requirement_ids=...)`, first-use `capture_baseline(workspace, run_id=..., input_source=...)`, resume `load_baseline(workspace, run_id=...)`, and `verify(workspace, scope=..., requirement_ids=..., output_dir=..., cancel_event=...)`. Focused scopes require a separate output directory and retain the original task denominator for their selected IDs. `arc_light.evidence` exposes check selection, source comparison, summaries and repair targets independently of model tools. `arc_light.reports.write_json` supplies the existing atomic report write used by stability and the coordinator. `preflight(workspace, cancel_event=...)` propagates cancellation through dependency installation, browser launch and backend health polling. An asynchronous caller sets the event and awaits the worker cleanup before passing workspace ownership to another stage.

Preflight prepares the application's actual dependency directories and generated source for subsequent implementation. Its backend health probe uses a disposable copy. Verification runs any uncached installation in its disposable copy and checks migrated inherited data before browser execution.

The Release description observation in `compatibility.py` retains its user-supplied report source and exact scenario match. An explicit public GIVEN description takes precedence. The direct filter entry and persisted reaction total assertions remain in the r6 browser adapters. Their inherited provenance is described in SOURCES.md.

The fixture tests execute the browser adapters against a small hand-written Node application with SQLite. They cover separate same-name requirement accounts, actual page input and reload, startup idempotence, restart persistence, HTTP 500 with an independent business failure, unmapped requirements and empty scenario sets. Python tests exercise committed WAL backup, original-row loss on resume, inherited tests, cancellation cleanup and stability streaks.

```bash
uv run pytest -q examples/arcbench-evolution-r6/tests
npm ci --prefix examples/arcbench-evolution-r6
cd examples/arcbench-evolution-r6
npx playwright install chromium
OPENCOLLAB_TEST_PYTHON=/path/to/OpenCollab/.venv/bin/python npm run test:browser
```

The browser fixtures have a dedicated CI job and matching Playwright lockfile. The regular Python suite includes the example's pure Python tests. Application data, browser binaries and runtime credentials are supplied by the caller. The fixture inputs are synthetic.

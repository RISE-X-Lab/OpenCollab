---
name: arcbench-traceability
description: Record and query ARC Bench traceability assets in .arc/traceability using the runtime SDK. Use when an agent needs to persist requirements, scenarios, interfaces, tests, call edges, requirement node states, node contracts, or test/interface pass status so the ARC Bench frontend can display traceability changes and refresh from runner-events.jsonl.
---

# ARC Bench Traceability

Use this skill when generated assets need to become visible in ARC Bench traceability views. Prefer batch payloads for groups of related records to reduce agent overhead.

## Quick Start

Use the script path relative to this skill directory. Run from the target project workspace and keep `--project-dir .`, or prefix `scripts/arc_traceability.py` with the relative path from the workspace to this skill directory.

```bash
python scripts/arc_traceability.py init --project-dir .
python scripts/arc_traceability.py upsert-interface --project-dir . --payload-json '{"interface_id":"REQ-1.UI.Home","req_ids":["REQ-1"],"type":"ui","content":"Home page component","file_path":"frontend/src/Home.jsx"}'
python scripts/arc_traceability.py upsert-node-state --project-dir . --req-id REQ-1 --state DESIGNED --phase design
python scripts/arc_traceability.py list-tests --project-dir . --req-id REQ-1
```

## Storage Model

Traceability is current-state JSON under `.arc/traceability`, not SQLite. The directory should be committed with the project so rewind/reset restores traceability naturally.

## Action Groups

- Store requirements: `init`, `store-requirement-tree`, `upsert-requirement`, `upsert-scenario`.
- Store generated assets: `upsert-interface`, `set-interface-implemented`, `upsert-test`, `set-test-status`, `set-test-statuses`, `insert-call-edge`.
- Store node progress: `upsert-node-state`, `upsert-node-contract`, `clear-node-design-artifacts`.
- Query current state: `get-requirement`, `list-requirements`, `list-interfaces`, `list-tests`, `list-call-edges`, `list-node-states`.

## Payload Rules

- Use `--payload-json` or `--payload-file` for complex records.
- Use stable IDs. Recommended forms: `REQ-1.UI.Home`, `REQ-1.API.Search`, `REQ-1.TEST.home-renders`.
- Always include `req_ids` for interfaces and `req_id` for tests.
- After direct SDK writes, the SDK emits frontend refresh signals automatically.

## Reference

Read `references/schema.md` for expected payload keys and examples.

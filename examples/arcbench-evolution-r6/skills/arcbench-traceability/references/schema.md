# ARC Bench Traceability Payloads

## Requirement

Required: `req_id`. Optional: `name`, `description`, `visual_reference`, `scenarios`, `parent_id`, `children_ids`, `dependencies`.

## Scenario

Required: `scenario_id`, `req_id`, `name`. Optional: `steps`.

## Interface

Required: `interface_id`, `req_ids`, `type`, `content`. Optional: `file_path`, `first_line`, `implemented`, `callers`, `callees`.

Example:

```json
{"interface_id":"REQ-2.API.Search","req_ids":["REQ-2"],"type":"api","content":"GET /api/search accepts origin, destination, date","file_path":"backend/routes/search.js","implemented":false}
```

## Test

Required: `test_id`, `req_id`, `type`. Optional: `file_path`, `first_line`, `interface_ids`, `passed`, `scenario_id`.

## Call Edge

Required: `source_req_id`, `target_req_id`, `from_interface_id`, `to_interface_id`. Optional: `edge_type`.

## Node State

Required: `req_id`, `state`. Optional: `phase`.

Common states: `UNSEEN`, `DESIGNING`, `DESIGNED`, `IMPLEMENTING`, `IMPLEMENTED`, `PASSED`, `FAILED`, `CONVERGED`, `CONVERGED_WITH_FAILED_CHILDREN`.

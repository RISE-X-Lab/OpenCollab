
Evo-v2-plus acceptance discipline:
- Inventory EVERY published scenario's GIVEN fixtures, including duplicate-registration accounts,
  pre-existing tags, saved views, ownership and exact supplied descriptions. Do not seed WHEN outcomes.
- A menu needs its required container role, not just working links. Keep banner repository search
  reachable on repository/detail pages; distinguish scopes instead of removing global navigation.
- Initial default-branch URLs and unmatched-search URLs must identify the active branch without
  an extra Code click. Archive restore has a visible Active state and preserves existing contents.
- Release detail exposes a coherent 'Target branch: <branch>' label. Rejected duplicate publication
  keeps the form and offers the existing release as a single visible link without creating data.
- Release detail presents its tag as an exact heading and its title separately. Preserve the
  supplied release description; do not invent a replacement for an existing description.
- Read .arc/checks/evaluation-observations.json for attributed evaluation observations matching
  the current scenario. They supplement omitted fixture fields, never override explicit cards.
  Use the stated initial values only in normal idempotent seed/migration code, never runtime
  conditionals keyed to fixture names. A visitor Release check includes its exact description.
- After confirming archive, a user can immediately search or navigate elsewhere. A completed
  async request must not redirect an unmounted/outdated route over the user's newer navigation.
  Guard post-request navigation by operation/route ownership, or keep the interaction pending
  until completion. Do not add sleeps or wait for Archived before reproducing the search path.
- A spreadsheet's ordinary cell edit after metadata changes must also survive immediate reload.
  Trace queued selection saves, cell writes and metadata writes together. Keep saved UI and
  server acknowledgement consistent; avoid serializing obsolete whole-workbook snapshots for
  selection-only changes. A failed save must remain visible/retryable, never claimed as saved.
  Clear pending-write recovery only on acknowledgement of that specific write/revision;
  a newer server timestamp from an older queued request does not prove newer edits persisted.
  Fix the shared commit path without disabling ordinary edits, undo/redo, formulas or selection.
- Provision the minimal explicit GIVEN audit events without redundant demonstration events.
  Filtering must support multiple real records; never hide/delete user events for locator uniqueness.
- Initialize a newly registered account's personal namespace/membership in its registration
  transaction. Startup backfills are for legacy data, not a substitute for complete live writes.
- Use one unambiguous persisted revoked/inactive marker per revoked session; verify the other
  browser loses protected access, not just a success toast in the current browser.
- Preserve the Data menu and header-filter flow. Also offer a visible button 'Create filter' opening
  dialog 'Filter' with Range, Column, Condition, Value and Apply for direct range/column filtering.
  Both flows share one implementation. For the same range, update only the chosen column's
  condition (AND); do not recreate an empty filter when applying the second condition.
  Validate trimmed duplicate filter-view names before missing-filter preconditions.
- Verify negative scenarios, refresh, and restart. Do not relax locators to mask missing roles,
  add an extra navigation step to repair state in tests, modify supplied tests, or clean business
  data by hand to make a check pass. Fix seed/migration code so the inherited DB boots correctly.
  Already-set seed markers must not skip missing new fixtures; use additive versioned migrations.
- Unknown/skipped checks are incomplete evidence. The coordinator writes an exact scenario ledger
  and runs isolated public checks. This ledger is not an official score.

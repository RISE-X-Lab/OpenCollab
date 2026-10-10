Delivery and interaction contract:
- Backend startup must support both inherited and newly provisioned DBs. Await compatible schema + idempotent seed
  before listening. Seed explicitly existing GIVEN/description records and relationships only,
  never successful WHEN/THEN outcomes; restarting must not reset or duplicate user data.
- Match exact visible/accessibility names, capitalization and punctuation in source, not CSS.
  Respect specified role, container scope and number of controls: links are anchors, buttons
  buttons, labelled textboxes/searchboxes/selects have the stated type. Keep controls reachable
  on every specified entry page. Disabled means disabled; permission-hidden means absent.
- Check each GIVEN starting page before implementing WHEN: a route existing does not make
  its entry reachable. Organization and issue/PR titles must be real links in the specified
  starting context, backed by visible data and its permissions. Avoid duplicate control names.
- If a choice is opened then clicked as an option, render visible clickable options only for
  the open listbox; hidden native options are insufficient for that interaction. Put header
  filter controls inside the worksheet grid, and let rename forms submit on Enter.
- Implement the user's whole path: home entry -> specified session/context -> operation ->
  visible result -> reload/reopen persistence. Stable URLs identify the same object. Encode
  path values containing slashes so they round-trip. Keep selected context visible after navigation.
- Implement every required validation/permission/empty state. Show simultaneous field errors
  together when required, keep allowed non-sensitive inputs, and use the exact application messages;
  native browser validation is not a substitute. Keep submit actionable unless the spec disables it.
- Do not make unspecified inputs mandatory or add steps to the requested path. A blank workbook
  must support home -> New blank workbook -> Create without filling a name: default its name to
  "Untitled workbook". Keep empty-name validation for an explicit rename operation separate.
- Check permissions server-side for each operation; roles are not a universal cumulative ladder.
  Failed operations leave persistent state unchanged; related writes commit atomically.
- For row/column deletion, move surviving cells and their references atomically; clearing
  deleted old coordinates afterwards must not erase cells moved into those coordinates.
- Reuse shared session/navigation/data state across dependent features. For a spreadsheet,
  implement its actual model/selection/calculation behaviour and the required ARIA state; visible
  labels alone do not implement a grid. Concrete descriptions govern placeholder scenario prose.
  Match shared parent-card grid/menu/editor names exactly; decorative arrows/icons must not
  become part of a cell's data text. Range copy/cut, formula offsets and undo/redo require actual state changes.
  Update selection and active-cell state before acting on shortcuts; asynchronous saves must not
  make the next edit or paste operate on the previous cell/range.
- Preserve unrelated work and protected validation. No withheld answers, weakening checks,
  test-only application branches or private reset interfaces. Change application files as needed.

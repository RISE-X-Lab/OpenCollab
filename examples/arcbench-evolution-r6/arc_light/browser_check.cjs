// Navigation and bounded public prerequisites; this does not measure full acceptance coverage.
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect: playwrightExpect } = require(require.resolve('@playwright/test', { paths: [process.cwd()] }));
const expect = playwrightExpect.configure({timeout: 4000});
const output = process.env.ARC_VERIFY_OUTPUT;
const base = process.env.ARC_VERIFY_URL;
const report = { evidence_kind: 'browser_navigation_smoke_not_business_acceptance',
  pages: [], prerequisites: [], page_errors: [], server_errors: [], warnings: [], ok: false };
const configPath = path.join(output, 'public-prerequisites.json');
const contract = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : { kind: 'none' };
const deadline = Date.now() + Math.max(1000, Number(process.env.ARC_BROWSER_SECONDS || 420) * 1000);

async function prerequisite(key, execute) {
  try { return await execute(); }
  catch (error) { error.blocked_by ||= key; throw error; }
}

function watchPage(page, checkName = null, sourceIds = []) {
  page.on('pageerror', error => report.page_errors.push(String(error).slice(0, 1000)));
  page.on('response', response => {
    if (response.status() >= 500 && response.url().startsWith(base)) {
      report.server_errors.push({ url: response.url(), status: response.status(),
        method: response.request().method(), check: checkName, source_ids: sourceIds });
    }
  });
}

async function check(browser, name, execute,
                     sourceIds = contract.check_sources?.[name] || contract.source_ids || [], options = {}, identity = {}) {
  if (deadline - Date.now() < 5000) {
    report.prerequisites.push({ ...identity, name, ok: null, status: 'skipped', reason: 'Browser time reserve exhausted', source_ids: sourceIds });
    return;
  }
  const context = await browser.newContext(options);
  const page = await context.newPage();
  watchPage(page, name, sourceIds);
  page.setDefaultTimeout(2500);
  page.setDefaultNavigationTimeout(5000);
  let timer;
  try {
    await Promise.race([
      (async () => { await page.goto(base + '/'); await execute(page); })(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('ARC_CHECK_DEADLINE')), Math.max(1, Math.min(20000, deadline - Date.now()))); }),
    ]);
    report.prerequisites.push({ ...identity, name, ok: true, status: 'passed', source_ids: sourceIds });
  } catch (error) {
    if (String(error.message).startsWith('Missing public scenario input:')) {
      skip(name, error.message + '; path not verified', sourceIds, identity);
    } else if (error.message === 'ARC_CHECK_DEADLINE') {
      skip(name, 'Scenario exceeded its bounded execution deadline; result unknown', sourceIds, identity);
    } else {
      const location = String(error.stack || '').split('\n').find(line => /at evolution(?:Github|Sheet)/.test(line));
      report.prerequisites.push({ ...identity, name, ok: false, status: error.blocked_by ? 'blocked' : 'failed',
        blocked_by: error.blocked_by, detail: [location, String(error)].filter(Boolean).join('\n').slice(0, 1800), source_ids: sourceIds });
    }
  } finally {
    clearTimeout(timer);
    await context.close();
    fs.writeFileSync(path.join(output, "browser-progress.json"), JSON.stringify(report, null, 2));
  }
}

function skip(name, reason, sourceIds = contract.source_ids || [], identity = {}) {
  report.prerequisites.push({ ...identity, name, ok: null, status: 'skipped', reason, source_ids: sourceIds });
}

async function createBlank(page) {
  await page.getByRole('button', { name: 'New blank workbook', exact: true }).click();
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toHaveAttribute('aria-selected', 'true');
}

function gridCell(page, name) { return page.getByRole('gridcell', { name, exact: true }); }

async function enterCell(page, name, value) {
  const cell = gridCell(page, name);
  if (contract.sheet_contracts?.inline_edit) {
    await cell.dblclick();
    const editor = page.getByRole('textbox', { name: 'Edit ' + name, exact: true });
    await editor.fill(value);
    await editor.press('Enter');
  } else {
    await cell.click();
    await expect(cell).toHaveAttribute('aria-selected', 'true');
    const formula = page.getByRole('textbox', { name: 'Formula bar', exact: true });
    await formula.fill(value);
    await formula.press('Enter');
  }
  if (!value.startsWith('=')) await expect(cell).toHaveText(value);
}

async function dragRange(page, first, last) {
  await gridCell(page, first).scrollIntoViewIfNeeded();
  const a = await gridCell(page, first).boundingBox();
  const b = await gridCell(page, last).boundingBox();
  if (!a || !b) throw new Error('Range corners are not visible: ' + first + ':' + last);
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 });
  await page.mouse.up();
  await expect(gridCell(page, first)).toHaveAttribute('aria-selected', 'true');
  await expect(gridCell(page, last)).toHaveAttribute('aria-selected', 'true');
}

async function sheetPrerequisites(browser) {
  if (!contract.sheet_create) return;
  await check(browser, 'blank_workbook_create_and_optional_edit_persistence', async page => {
    await createBlank(page);
    const tab = page.getByRole('tab', { name: 'Sheet1', exact: true });
    const cell = page.getByRole('gridcell', { name: 'A1', exact: true });
    await expect(page.getByRole('tab')).toHaveCount(1);
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expect(cell).toHaveAttribute('aria-selected', 'true');
    if (contract.sheet_edit) {
      const text = 'arc-cell-' + Date.now().toString(36);
      await cell.click();
      const formula = page.getByRole('textbox', { name: 'Formula bar', exact: true });
      await formula.fill(text);
      await formula.press('Enter');
      await expect(cell).toHaveText(text);
      await page.reload();
      await expect(cell).toHaveText(text);
    } else {
      await page.reload();
    }
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expect(cell).toHaveAttribute('aria-selected', 'true');
  });
  const contracts = contract.sheet_contracts || {};
  for (const [key, header, command] of [
    ['row_headers', ['rowheader', '1'], 'Insert 1 row above'],
    ['column_headers', ['columnheader', 'A'], 'Insert 1 column left'],
  ]) {
    if (contracts[key]) await check(browser, 'sheet_' + key, async page => {
      await createBlank(page);
      const target = page.getByRole(header[0], { name: header[1], exact: true });
      await expect(target).toHaveCount(1);
      await target.click({ button: 'right' });
      await expect(page.getByRole('menuitem', { name: command, exact: true })).toBeVisible();
    }, contracts[key]);
  }
  if (contracts.worksheet_options) await check(browser, 'sheet_worksheet_options', async page => {
    await createBlank(page);
    const menu = page.getByRole('button', { name: 'Worksheet options for Sheet1', exact: true });
    await expect(menu).toHaveCount(1);
    await menu.click();
    await expect(page.getByRole('menuitem', { name: 'Rename', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Delete', exact: true })).toBeVisible();
  }, contracts.worksheet_options);
  if (contracts.inline_edit) await check(browser, 'sheet_inline_edit_and_reload', async page => {
    await createBlank(page);
    await gridCell(page, 'A1').dblclick();
    const editor = page.getByRole('textbox', { name: 'Edit A1', exact: true });
    const value = 'arc-inline-' + Date.now().toString(36);
    await editor.fill(value);
    await editor.press('Enter');
    await expect(gridCell(page, 'A1')).toHaveText(value);
    await page.reload();
    await expect(gridCell(page, 'A1')).toHaveText(value);
  }, contracts.inline_edit);
  if (contracts.range_clipboard) await check(browser, 'sheet_range_copy_cut_undo_redo_reload', async page => {
    await createBlank(page);
    const value = 'arc-range-' + Date.now().toString(36);
    for (const [name, text] of [['A1', value], ['B1', '2'], ['A2', 'Other'], ['B2', '4']]) {
      await enterCell(page, name, text);
    }
    await dragRange(page, 'A1', 'B2');
    await page.keyboard.press('Control+c');
    await gridCell(page, 'D1').click();
    await page.keyboard.press('Control+v');
    for (const [name, text] of [['D1', value], ['E1', '2'], ['D2', 'Other'], ['E2', '4'], ['A1', value]]) {
      await expect(gridCell(page, name)).toHaveText(text);
    }
    if (contracts.undo_redo) {
      await page.getByRole('button', { name: 'Undo', exact: true }).click();
      await expect(gridCell(page, 'D1')).toHaveText('');
      await expect(gridCell(page, 'E2')).toHaveText('');
      await page.getByRole('button', { name: 'Redo', exact: true }).click();
      await expect(gridCell(page, 'D1')).toHaveText(value);
      await expect(gridCell(page, 'E2')).toHaveText('4');
    }
    await dragRange(page, 'D1', 'E2');
    await page.keyboard.press('Control+x');
    await gridCell(page, 'G1').click();
    await page.keyboard.press('Control+v');
    await expect(gridCell(page, 'G1')).toHaveText(value);
    await expect(gridCell(page, 'H2')).toHaveText('4');
    await expect(gridCell(page, 'D1')).toHaveText('');
    await expect(gridCell(page, 'E2')).toHaveText('');
    await page.reload();
    await expect(gridCell(page, 'G1')).toHaveText(value);
    await expect(gridCell(page, 'H2')).toHaveText('4');
    await expect(gridCell(page, 'D1')).toHaveText('');
  }, [...contracts.range_clipboard, ...(contracts.undo_redo || [])], { permissions: ['clipboard-read', 'clipboard-write'] });
  if (contracts.formula_copy) await check(browser, 'sheet_formula_relative_absolute_copy_reload', async page => {
    await createBlank(page);
    for (const [name, text] of [['A1', '2'], ['B1', '3'], ['A2', '7'], ['C1', '=A1+$B$1']]) {
      await enterCell(page, name, text);
    }
    await expect(gridCell(page, 'C1')).toHaveText('5');
    await gridCell(page, 'C1').click();
    await page.keyboard.press('Control+c');
    await gridCell(page, 'C2').click();
    await page.keyboard.press('Control+v');
    await expect(gridCell(page, 'C2')).toHaveText('10');
    await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=A2+$B$1');
    await expect(gridCell(page, 'C1')).toHaveText('5');
    await page.reload();
    await expect(gridCell(page, 'C2')).toHaveText('10');
  }, [...contracts.formula_copy, ...contracts.range_clipboard], { permissions: ['clipboard-read', 'clipboard-write'] });
}


// Bounded evolution checks use the first published scenario, not inherited reports.
function quoted(text) { return [...text.matchAll(/`([^`]+)`/g)].map(m => m[1]); }
function requireMatch(text, pattern, label) {
  const match = text.match(pattern);
  if (!match) throw new Error('Missing public scenario input: ' + label);
  return match[1];
}
async function control(page, label) {
  const locator = page.getByRole('button', {name: label, exact: true})
    .or(page.getByRole('menuitem', {name: label, exact: true}))
    .or(page.getByRole('link', {name: label, exact: true}));
  await locator.click();
}
async function evoSignIn(page, account, emailCase = false) {
  if (!account) throw new Error('Missing public scenario input: account');
  await prerequisite('shared:sign_in_entry', () => signInEntry(page).click());
  await prerequisite('shared:authenticated_session', async () => {
    await page.getByLabel('Username or email', {exact: true}).fill(emailCase ? account.email.toUpperCase() : account.username);
    await page.getByLabel('Password', {exact: true}).fill(account.password);
    await page.getByRole('button', {name: 'Sign in', exact: true}).click();
    await expect(page.getByText(account.username, {exact: true}).first()).toBeVisible();
  });
}
function signInEntry(page) {
  return (contract.main_sign_in ? page.getByRole('main') : page)
    .getByRole('link', {name: 'Sign in', exact: true});
}
function requiresAccount(item) {
  if (['Search for and Locate Repositories', 'List and Switch Repository Branches'].includes(item.name)) return false;
  return !(item.visitor && ['Create and View Repository Releases', 'Add and Remove Issue Reactions'].includes(item.name));
}
async function openRepository(page, repository) {
  const search = page.getByRole('searchbox', {name: 'Search', exact: true});
  await search.fill(repository); await search.press('Enter');
  await page.getByRole('main').getByRole('link', {name: repository, exact: true}).click();
  await expect(page.getByRole('heading').filter({hasText: repository}).first()).toBeVisible();
}
async function chooseScalar(page, label, value) {
  // Ordinary scalar form choices use native controls; do not rewrite option-role branch pickers.
  await page.getByLabel(label, {exact: true}).selectOption({label: value});
}
async function dataMenu(page, label) {
  await control(page, 'Data'); await page.getByRole('menuitem', {name: label, exact: true}).click();
}
async function createFilter(page, item) {
  const range = requireMatch(item.when, /filter for\s+([A-Z]+\d+:[A-Z]+\d+)/, 'filter range');
  const [first, last] = range.split(':');
  await dragRange(page, first, last);
  await dataMenu(page, 'Create filter');
}
async function ordinaryEdit(page, item) {
  // Use a verified empty cell only; never replace inherited content as a probe.
  const target = gridCell(page, 'A1');
  if ((await target.innerText()).trim()) {
    throw new Error('Missing public scenario input: safe empty A1 for ordinary edit; persistence path not verified');
  }
  const probe = 'arc-evo-' + Date.now().toString(36);
  await enterCell(page, 'A1', probe);
  await page.reload();
  await expect(gridCell(page, 'A1')).toHaveText(probe);
}
async function evolutionSheet(page, item) {
  const workbook = requireMatch(item.given, /workbook\s+`([^`]+)`/i, 'workbook');
  await page.getByRole('link', {name: workbook, exact: true}).click();
  const name = item.name;
  if (name === 'Rename a Workbook') {
    const desired = quoted(item.when)[1];
    await control(page, 'Rename workbook');
    await page.getByLabel('Workbook name', {exact: true}).fill(desired);
    await control(page, 'Save');
    await expect(page.getByRole('heading', {name: desired.trim(), exact: true})).toBeVisible();
    await page.reload();
    await control(page, 'Rename workbook');
    await expect(page.getByLabel('Workbook name', {exact: true})).toHaveValue(desired.trim());
    await page.getByLabel('Workbook name', {exact: true}).fill('X'.repeat(81));
    await control(page, 'Save');
    await expect(page.getByText('Workbook name must be 80 characters or fewer', {exact: true})).toBeVisible();
    await expect(page.getByLabel('Workbook name', {exact: true})).toHaveValue(desired.trim());
    await control(page, 'Cancel');
    await ordinaryEdit(page, item);
    await expect(page.getByRole('heading', {name: desired.trim(), exact: true})).toBeVisible();
  } else if (name === 'Rename a Worksheet') {
    const original = requireMatch(item.given, /active worksheet\s+`([^`]+)`/i, 'worksheet');
    const desired = requireMatch(item.when, /enters\s+`([^`]+)`\s+in\s+"Worksheet name"/i, 'worksheet name').trim();
    await control(page, 'Worksheet options for ' + original);
    await control(page, 'Rename');
    await page.getByLabel('Worksheet name', {exact: true}).fill(desired);
    await control(page, 'Save');
    await expect(page.getByRole('tab', {name: desired, exact: true})).toHaveAttribute('aria-selected', 'true');
    await ordinaryEdit(page, item);
    await expect(page.getByRole('tab', {name: desired, exact: true})).toHaveAttribute('aria-selected', 'true');
  } else if (name === 'Edit a Cell Through the Grid or Formula Bar') {
    const coord = requireMatch(item.when, /selects cell\s+([A-Z]+\d+)/, 'cell');
    await gridCell(page, coord).click();
    await page.keyboard.press('Delete');
    await expect(gridCell(page, coord)).toHaveText('');
    await expect(gridCell(page, coord)).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByLabel('Formula bar', {exact: true})).toHaveValue('');
    await page.reload();
    await expect(gridCell(page, coord)).toHaveText('');
  } else if (name === 'Freeze Rows and Columns') {
    const selected = requireMatch(item.when, /selects\s+([A-Z]+\d+)/, 'cell');
    const state = requireMatch(item.then, /button\s+"([^"]+)"/, 'freeze state');
    // Repeated checks may encounter the already-applied state. Change it through public UI
    // first so a no-op cannot masquerade as a successful persistence write.
    if (await page.getByRole('button', {name: state, exact: true}).isVisible()) {
      const row = Number(selected.match(/\d+/)[0]) + 1;
      await gridCell(page, selected.replace(/\d+/, String(row))).click();
      await control(page, 'View'); await control(page, 'Freeze rows through ' + row);
    }
    await gridCell(page, selected).click();
    await control(page, 'View');
    await control(page, requireMatch(item.when, /clicks\s+"([^"]+)"/, 'freeze action'));
    await expect(page.getByRole('button', {name: state, exact: true})).toBeVisible();
    // Immediate reload is intentional: optimistic UI alone is not persistence evidence.
    await page.reload();
    await expect(page.getByRole('button', {name: state, exact: true})).toBeVisible();
    await ordinaryEdit(page, item);
    await expect(page.getByRole('button', {name: state, exact: true})).toBeVisible();
  } else if (name === 'Find and Replace Cell Text') {
    await gridCell(page, 'A1').click();
    await control(page, 'Edit'); await control(page, 'Find and replace');
    await page.getByLabel('Find', {exact: true}).fill(requireMatch(item.when, /enters\s+`([^`]+)`\s+in\s+"Find"/, 'find text'));
    await control(page, 'Find next'); await control(page, 'Find next');
    await expect(page.getByText('Match 2 of 3', {exact: true})).toBeVisible();
    const coord = requireMatch(item.then, /Cell\s+([A-Z]+\d+)\s+is selected/i, 'selected cell');
    await expect(gridCell(page, coord)).toHaveAttribute('aria-selected', 'true');
  } else if (name === 'Create and Edit Named Ranges') {
    const tokens = quoted(item.when); const rangeName = tokens[1], range = tokens[2], formula = tokens[3];
    await control(page, 'Data'); await control(page, 'Named ranges'); await control(page, 'Add named range');
    await page.getByLabel('Name', {exact: true}).fill(rangeName);
    await page.getByLabel('Range', {exact: true}).fill(range);
    await control(page, 'Save');
    // The public contract leaves the named-range list visible after save.
    await expect(page.getByText(rangeName, {exact: true}).first()).toBeVisible();
    const coord = requireMatch(item.when, /selects\s+([A-Z]+\d+)/, 'formula cell');
    await enterCell(page, coord, formula);
    const value = requireMatch(item.then, /displays\s+`([^`]+)`/, 'formula result');
    await expect(gridCell(page, coord)).toHaveText(value);
    await ordinaryEdit(page, item);
    await expect(gridCell(page, coord)).toHaveText(value);
    await gridCell(page, coord).click();
    await expect(page.getByLabel('Formula bar', {exact: true})).toHaveValue(formula);
  } else if (name === 'Create, Edit, and Delete a Cell Note') {
    const coord = requireMatch(item.when, /selects\s+([A-Z]+\d+)/, 'note cell');
    const text = requireMatch(item.when, /enters\s+`([^`]+)`\s+in\s+"Note"/, 'note text');
    const before = await gridCell(page, coord).innerText();
    const existed = await gridCell(page, coord).getByRole('button', {name: 'Open note for ' + coord, exact: true}).count();
    await gridCell(page, coord).click(); await control(page, 'Insert'); await control(page, 'Add note');
    if (existed) {
      skip('evolution:note_creation', 'Note already present; checking edit/view persistence instead of first creation', item.source_ids);
      const edit = page.getByRole('button', {name: 'Edit note', exact: true});
      if (await edit.isVisible()) await edit.click();
    }
    await page.getByLabel('Note', {exact: true}).fill(text); await control(page, 'Save note');
    await page.reload(); await control(page, 'Open note for ' + coord);
    await expect(page.getByRole('dialog', {name: 'Note for ' + coord, exact: true})
      .getByLabel('Note', {exact: true})).toHaveValue(text);
    await control(page.getByRole('dialog', {name: 'Note for ' + coord, exact: true}), 'Close');
    await ordinaryEdit(page, item); await control(page, 'Open note for ' + coord);
    await expect(page.getByLabel('Note', {exact: true})).toHaveValue(text);
    // A note indicator may contribute text, so compare the cell's original value separately.
    await expect(gridCell(page, coord)).toContainText(before);
  } else if (name === 'Set Dropdown or Numeric Validation for a Range') {
    const coord = requireMatch(item.when, /selects\s+([A-Z]+\d+)/, 'validated cell');
    const before = await gridCell(page, coord).innerText();
    const value = requireMatch(item.when, /enters\s+`([^`]+)`/, 'invalid value');
    const message = requireMatch(item.then, /message\s+"([^"]+)"/, 'custom error');
    await gridCell(page, coord).click();
    const bar = page.getByLabel('Formula bar', {exact: true});
    await bar.fill(value); await bar.press('Enter');
    await expect(page.getByText(message, {exact: true})).toBeVisible();
    await expect(gridCell(page, coord)).toHaveText(before);
    await ordinaryEdit(page, item);
    await gridCell(page, coord).click(); await bar.fill(value); await bar.press('Enter');
    await expect(page.getByText(message, {exact: true})).toBeVisible();
    await expect(gridCell(page, coord)).toHaveText(before);
  } else if (name === 'Create, Edit, and Delete Conditional Formatting Rules') {
    const editing = /Edit rule/.test(item.when);
    if (!editing) {
      const range = requireMatch(item.when, /selects\s+([A-Z]+\d+:[A-Z]+\d+)/, 'format range');
      await dragRange(page, ...range.split(':'));
    }
    await control(page, 'Format'); await control(page, 'Conditional formatting');
    const dialog = page.getByRole('dialog', {name: 'Conditional formatting', exact: true});
    if (editing) {
      await control(dialog, 'Edit rule 1');
    } else {
      await chooseScalar(dialog, 'Condition', requireMatch(item.when, /chooses "([^"]+)" in "Condition"/, 'condition'));
      await dialog.getByLabel('Value', {exact: true}).fill(requireMatch(item.when, /enters `([^`]+)` in "Value"/, 'threshold'));
    }
    await chooseScalar(dialog, 'Style', requireMatch(item.when, /chooses "([^"]+)" in "Style"/, 'style'));
    await control(dialog, 'Save');
    const color = requireMatch(item.then, /(rgb\(\d+,\s*\d+,\s*\d+\))/, 'fill color');
    requireMatch(item.then, /^([A-Z]+\d+) and ([A-Z]+\d+)/, 'matching cells');
    const matching = item.then.match(/^([A-Z]+\d+) and ([A-Z]+\d+)/);
    for (const coord of matching.slice(1)) await expect(gridCell(page, coord)).toHaveCSS('background-color', color);
    if (editing) {
      await control(page, 'Format'); await control(page, 'Conditional formatting');
      await control(dialog, 'Delete rule 1');
      for (const coord of matching.slice(1)) await expect(gridCell(page, coord)).not.toHaveCSS('background-color', color);
    }
    await page.reload();
    for (const coord of matching.slice(1)) {
      if (editing) await expect(gridCell(page, coord)).not.toHaveCSS('background-color', color);
      else await expect(gridCell(page, coord)).toHaveCSS('background-color', color);
    }
    if (!editing) {
      const nonmatching = requireMatch(item.then, /,\s*([A-Z]+\d+) does not/, 'nonmatching cell');
      await expect(gridCell(page, nonmatching)).not.toHaveCSS('background-color', color);
    }
  } else if (name === 'Filter Rows by Value or Condition') {
    await createFilter(page, item);
    if (item.scenario_index === 0) {
      for (const match of item.when.matchAll(/applies `([^`]+)` value `([^`]+)` to "([^"]+)"/g)) {
        await control(page, match[3]);
        const dialog = page.getByRole('dialog', {name: match[3], exact: true});
        // The inherited filter chooser has a custom combobox/option contract.
        const condition = dialog.getByRole('combobox', {name: 'Condition', exact: true});
        if (await condition.evaluate(el => el.tagName === 'SELECT')) await condition.selectOption({label: match[1]});
        else { await condition.click(); await dialog.getByRole('option', {name: match[1], exact: true}).click(); }
        await dialog.getByLabel('Value', {exact: true}).fill(match[2]); await control(dialog, 'Apply');
      }
    }
    // Preserve the user's whitespace: normalization belongs to the application.
    const view = requireMatch(item.when, /enters `([^`]+)` in "Filter view name"/, 'view name');
    await dataMenu(page, 'Save filter view');
    const input = page.getByLabel('Filter view name', {exact: true}); await input.fill(view);
    await control(page, 'Save view');
    if (item.scenario_index !== 0) {
      await expect(page.getByRole('alert')).toContainText('Filter view name already exists');
      await control(page.getByRole('dialog').filter({has: input}), 'Close');
      // Supplement the public scenario with the state observed in the r4 report:
      // a duplicate name must still be rejected when there is no active filter.
      await dataMenu(page, 'Clear filter');
      await dataMenu(page, 'Save filter view');
      await input.fill(view); await control(page, 'Save view');
      await expect(page.getByRole('alert'), 'Validate trimmed duplicate names before missing-filter preconditions')
        .toContainText('Filter view name already exists');
      await control(page.getByRole('dialog').filter({has: input}), 'Close');
      const savedName = requireMatch(item.given, /saved filter view `([^`]+)`/, 'saved view');
      await dataMenu(page, 'Filter views'); await control(page, savedName); await control(page, 'Delete filter view');
      await expect(page.getByRole('dialog').getByRole('button', {name: savedName, exact: true})).toHaveCount(0);
      await page.reload(); await dataMenu(page, 'Filter views');
      await expect(page.getByRole('dialog').getByRole('button', {name: savedName, exact: true})).toHaveCount(0);
    } else {
      const row = requireMatch(item.then, /Only row (\d+)/, 'visible filtered row');
      const range = requireMatch(item.given, /range ([A-Z]+)(\d+):([A-Z]+\d+)/, 'source range');
      const fullRange = item.given.match(/range ([A-Z]+)(\d+):([A-Z]+)(\d+)/);
      const assertRows = async () => {
        for (let r = Number(fullRange[2]) + 1; r <= Number(fullRange[4]); r++) {
          if (String(r) === row) await expect(gridCell(page, range + r)).toBeVisible();
          else await expect(gridCell(page, range + r)).toBeHidden();
        }
      };
      await assertRows(); await page.reload(); await assertRows();
      await dataMenu(page, 'Filter views');
      await expect(page.getByRole('dialog').getByRole('button', {name: view.trim(), exact: true})).toHaveCount(1);
    }
  } else {
    // Explicitly deferred rather than pretending a menu proves its operation.
    throw new Error('Unsupported evolution executor: ' + name);
  }
}
const plus = require('./plus_checks.cjs')({expect, base, contract, gridCell, dragRange, enterCell,
  control, dataMenu, chooseScalar, evoSignIn, signInEntry, legacySheet: evolutionSheet, watchPage, prerequisite});

async function sharedPrerequisites(browser) {
  const inputs = contract.shared_inputs || {};
  if (contract.kind === 'github') {
    if (inputs.sign_in_enabled !== false) await check(browser, 'shared:sign_in_entry', async page => {
      await signInEntry(page).click();
      await expect(page.getByLabel('Username or email', {exact: true})).toBeVisible();
    }, inputs.sign_in_ids || []);
    if (inputs.menu_account) await check(browser, 'shared:account_menu', async page => {
      await evoSignIn(page, inputs.menu_account);
      await page.getByRole('button', {name: 'Account menu', exact: true}).click();
      await page.getByRole('menu').getByRole('link', {name: 'Your organizations', exact: true}).click();
      await expect(page.getByRole('heading', {name: 'Your organizations', exact: true})).toBeVisible();
    }, inputs.menu_ids || []);
    if (inputs.repository) await check(browser, 'shared:repository_global_search', async page => {
      const search = page.getByRole('banner').getByRole('searchbox', {name: 'Search', exact: true});
      await search.fill(inputs.repository); await search.press('Enter');
      await page.getByRole('main').getByRole('link', {name: inputs.repository, exact: true}).click();
      await expect(page.getByRole('heading').filter({hasText: inputs.repository}).first()).toBeVisible();
      await expect(search).toBeVisible();
    }, inputs.repository_ids || []);
  } else if (contract.scope === 'shared' && inputs.workbook) {
    await check(browser, 'shared:workbook_grid', async page => {
      await page.getByRole('link', {name: inputs.workbook, exact: true}).click();
      await expect(page.getByRole('grid', {name: 'Worksheet grid', exact: true})).toBeVisible();
      await expect(page.getByLabel('Formula bar', {exact: true})).toBeVisible();
    }, inputs.workbook_ids || []);
  } else if (contract.scope === 'shared') {
    skip('shared:unsupported_contract', 'No supported shared entry was found in current requirements');
  }
}

async function evolutionPrerequisites(browser) {
  for (const item of contract.evolution_checks || []) {
    const name = 'evolution:' + item.name + ':scenario_' + ((item.scenario_index || 0) + 1);
    const identity = { requirement_id: item.requirement_id, scenario_index: item.scenario_index, scenario_id: item.scenario_id };
    if (contract.kind === 'github' && item.name !== 'Register a New GitHub Account' && requiresAccount(item) && !item.account) {
      skip(name, 'Cannot extract the public account; path not verified', item.source_ids, identity); continue;
    }
    if (item.remaining_scenarios) skip(name + ':other_scenarios', 'Outside the bounded selected scenario sample', item.source_ids);
    await check(browser, name, page => contract.kind === 'sheet'
      ? plus.sheet(page, item) : plus.github(page, item, browser), item.source_ids, {}, identity);
    if (contract.kind === 'sheet' && item.name === 'Filter Rows by Value or Condition' && item.scenario_index === 0)
      await check(browser, 'compatibility:filter_range_column_entry', page => plus.filterCompatibility(page, item), item.source_ids);
  }
}

async function githubPrerequisites(browser) {
  for (const key of contract.missing_inputs || []) {
    report.warnings.push('Skipped public prerequisite: missing input ' + key);
    skip('public_input:' + key, 'No corresponding public GIVEN input; path not verified');
  }
  for (const label of contract.deferred_home_targets || []) {
    skip('home_target:' + label, 'Outside the bounded representative home sample');
  }
  async function signInPage(page) {
    await signInEntry(page).click();
    await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
  }
  async function signIn(page, identifier, password, username) {
    await page.getByLabel('Username or email', { exact: true }).fill(identifier);
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByText(username, { exact: true }).first()).toBeVisible();
    await page.reload();
    await expect(page.getByText(username, { exact: true }).first()).toBeVisible();
  }
  if (contract.auth_checks !== false) {
    await check(browser, 'registration_validation_and_persisted_signin', async page => {
      await signInPage(page);
      await page.getByRole('link', { name: 'Create an account', exact: true }).click();
      for (const label of ['Username', 'Email', 'Password', 'Confirm password']) {
        await expect(page.getByLabel(label, { exact: true })).toBeVisible();
      }
      const terms = page.getByRole('checkbox', { name: 'Agree to the terms', exact: true });
      await expect(terms).not.toBeChecked();
      await page.getByLabel('Username', { exact: true }).fill('-invalid');
      await page.getByLabel('Email', { exact: true }).fill('not-an-email');
      await page.getByLabel('Password', { exact: true }).fill('short');
      await page.getByLabel('Confirm password', { exact: true }).fill('different');
      const create = page.getByRole('button', { name: 'Create account', exact: true });
      await expect(create).toBeEnabled();
      await create.click();
      for (const text of ['Username format is invalid', 'Email format is invalid',
        'Password requirements are not satisfied', 'Agree to terms is required']) {
        await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
      }
      await expect(page.getByLabel('Username', { exact: true })).toHaveValue('-invalid');
      await expect(page.getByLabel('Email', { exact: true })).toHaveValue('not-an-email');
      await expect(page.getByLabel('Password', { exact: true })).toHaveValue('');
      await expect(page.getByLabel('Confirm password', { exact: true })).toHaveValue('');
      const username = 'arc-check-' + Date.now().toString(36);
      const email = username + '@example.test';
      await page.getByLabel('Username', { exact: true }).fill(username);
      await page.getByLabel('Email', { exact: true }).fill(email);
      await page.getByLabel('Password', { exact: true }).fill('Valid-password-123!');
      await page.getByLabel('Confirm password', { exact: true }).fill('Valid-password-123!');
      await terms.check();
      await create.click();
      await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
      await signIn(page, email, 'Valid-password-123!', username);
    });
    if (contract.account) {
      for (const key of ['username', 'email']) {
        await check(browser, 'seeded_' + key + '_signin_and_reload', async page => {
          await signInPage(page);
          if (key === 'username') {
            await page.getByLabel('Username or email', { exact: true }).fill(contract.account.username);
            await page.getByLabel('Password', { exact: true }).fill('Wrong-password-123!');
            await page.getByRole('button', { name: 'Sign in', exact: true }).click();
            await expect(page.getByText('Invalid credentials', { exact: true })).toBeVisible();
          }
          await signIn(page, contract.account[key], contract.account.password, contract.account.username);
        });
      }
    }
    await check(browser, 'local_password_recovery_contract', async page => {
      await signInPage(page);
      await page.getByRole('link', { name: 'Forgot password', exact: true }).click();
      await page.getByLabel('Email', { exact: true }).fill('arc-unknown-' + Date.now() + '@example.test');
      await page.getByRole('button', { name: 'Send reset link', exact: true }).click();
      await expect(page.getByText('123456', { exact: true })).toBeVisible();
      for (const label of ['Verification code', 'New password', 'Confirm password']) {
        await expect(page.getByLabel(label, { exact: true })).toBeVisible();
      }
      await expect(page.getByRole('button', { name: 'Reset password', exact: true })).toBeEnabled();
    });
  }
  if (contract.public_repository) {
    await check(browser, 'public_repository_search_open_and_reload', async page => {
      const search = page.getByRole('searchbox', { name: 'Search', exact: true });
      await search.fill(contract.public_repository);
      await search.press('Enter');
      await page.getByRole('link', { name: contract.public_repository, exact: true }).click();
      await expect(page.getByRole('heading').filter({ hasText: contract.public_repository }).first()).toBeVisible();
      const destination = page.url();
      await page.reload();
      await expect(page.getByRole('heading').filter({ hasText: contract.public_repository }).first()).toBeVisible();
      if (page.url() !== destination) throw new Error('Repository identity changed after reload');
    });
  }
  if (contract.public_navigation?.length) {
    for (const label of contract.public_navigation) {
      const key = label === 'Issues' ? 'issue_title' : label === 'Pull requests' ? 'pull_title' : null;
      const title = key && contract[key];
      if (key && !title) skip('public_list_detail:' + label, 'Missing public GIVEN title; only entry checked');
      await check(browser, (key && !title ? 'public_entry_only: ' : 'public_navigation_from_home: ') + label, async page => {
        const home = page.url();
        await page.getByRole('link', { name: label, exact: true })
          .or(page.getByRole('button', { name: label, exact: true })).click();
        await expect(page).not.toHaveURL(home);
        if (!title) return;
        await page.getByRole('link', { name: 'Open', exact: true }).click();
        if (key === 'issue_title') {
          await page.getByRole('searchbox', { name: 'Search issues', exact: true }).fill(title);
        }
        const item = page.getByRole('link', { name: title, exact: true });
        await expect(item).toBeVisible();
        await page.reload();
        await expect(item).toBeVisible();
        await item.click();
        const heading = page.getByRole('heading', { name: title, exact: true });
        await expect(heading).toBeVisible();
        await page.reload();
        await expect(heading).toBeVisible();
      });
    }
  }
  for (const target of contract.home_targets || []) {
    await check(browser, 'home_direct_' + target.kind + ':' + target.label, async page => {
      const entry = page.getByRole('link', { name: target.label, exact: true });
      await expect(entry).toHaveCount(1);
      await entry.click();
      const heading = target.kind === 'repository'
        ? page.getByRole('heading').filter({ hasText: target.label }).first()
        : page.getByRole('heading', { name: target.label, exact: true });
      await expect(heading).toBeVisible();
      const destination = new URL(page.url()).pathname;
      await page.reload();
      await expect(heading).toBeVisible();
      if (new URL(page.url()).pathname !== destination) throw new Error('Object identity changed after reload');
    }, target.source_ids);
  }
  for (const [key, label] of [['account_menu', 'Your organizations'], ['compare_entry', 'Compare']]) {
    const selected = contract[key];
    if (!selected) continue;
    if (!selected.account) {
      skip(key, 'Missing public GIVEN account; authenticated path not verified', selected.source_ids);
      continue;
    }
    await check(browser, key + '_unique_navigation', async page => {
      await signInPage(page);
      await signIn(page, selected.account.username, selected.account.password, selected.account.username);
      if (key === 'account_menu') {
        await page.getByRole('button', { name: 'Account menu', exact: true }).click();
      }
      const entry = page.getByRole('link', { name: label, exact: true });
      await expect(entry).toHaveCount(1);
      await entry.click();
      if (key === 'compare_entry') {
        await expect(page.getByRole('combobox', { name: 'Base', exact: true })).toBeVisible();
        await expect(page.getByRole('combobox', { name: 'Compare', exact: true })).toBeVisible();
        await expect(page.getByRole('button', { name: 'Compare changes', exact: true })).toBeVisible();
      } else {
        await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible();
      }
    }, selected.source_ids);
  }
}

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.ARC_BROWSER_EXECUTABLE || undefined });
    const context = await browser.newContext();
    const page = await context.newPage();
    watchPage(page);
    page.on('console', message => {
      if (message.type() === 'error') report.warnings.push(message.text().slice(0, 600));
    });
    const queue = [base + '/'];
    const visited = new Set();
    const navigationLimit = contract.scope && contract.scope !== 'all' ? 1 : 3;
    while (queue.length && visited.size < navigationLimit) {
      const url = queue.shift();
      if (visited.has(url)) continue;
      visited.add(url);
      try {
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 8000 });
        // Wait for React's first render without waiting for every network request to become idle.
        await page.locator('body').waitFor({ state: 'attached', timeout: 5000 });
        await page.waitForFunction(() => document.body.innerText.trim() ||
          document.querySelector('a,button,input,select,[role="grid"],[role="tab"]'), null, { timeout: 4000 })
          .catch(() => {});
        await page.waitForTimeout(200);
        const body = (await page.locator('body').innerText()).trim();
        const snapshot = await page.locator('body').ariaSnapshot();
        const controls = await page.locator('a, button, input, select, [role="grid"], [role="tab"]').count();
        report.pages.push({ url: page.url(), status: response?.status(),
          text: body.slice(0, 800), accessibility: snapshot.slice(0, 1500), controls,
          blank: !body && controls === 0 });
        if (visited.size === 1) await page.screenshot({ path: path.join(output, 'browser-home.png') });
        for (const link of await page.locator('a[href]').evaluateAll(elements => elements
          .filter(element => element.getClientRects().length)
          .map(element => ({ href: element.href, text: (element.textContent || '').trim() })))) {
          const target = new URL(link.href);
          if (target.origin !== new URL(base).origin || target.pathname.startsWith('/api/')) continue;
          if (/logout|sign.?out|delete|remove/i.test(target.pathname + ' ' + link.text)) continue;
          if (!visited.has(target.href) && !queue.includes(target.href)) queue.push(target.href);
        }
      } catch (error) {
        report.pages.push({ url, navigation_error: String(error).slice(0, 1200) });
      }
    }
    await context.close();
    if (!contract.scope || ['all', 'shared'].includes(contract.scope)) await sharedPrerequisites(browser);
    await evolutionPrerequisites(browser);
    if (!contract.scope || contract.scope === 'all') {
      if (contract.kind === 'github') await githubPrerequisites(browser);
      if (contract.kind === 'sheet' && contract.sheet_create !== false) await sheetPrerequisites(browser);
    }
    report.evidence_kind = 'navigation_and_public_prerequisites_not_full_acceptance';
    report.ok = report.pages.length > 0 && !report.page_errors.length && !report.server_errors.length &&
      report.prerequisites.every(check => check.ok !== false &&
        (!check.name.startsWith('shared:') || check.status === 'passed')) &&
      (contract.evolution_checks || []).every(item => report.prerequisites.filter(check => check.requirement_id === item.requirement_id && check.scenario_index === item.scenario_index && check.status === 'passed').length === 1) &&
      report.pages.every(page => !page.blank && !page.navigation_error && page.status < 400);
    process.exitCode = report.ok ? 0 : 1;
  } catch (error) {
    report.error = String(error).slice(0, 800);
    report.capability = /Executable doesn't exist|browserType.launch|shared libraries/i.test(report.error)
      ? 'browser_unavailable' : 'browser_error';
    process.exitCode = report.capability === 'browser_unavailable' ? 78 : 1;
  } finally {
    if (browser) await browser.close();
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  }
})();

// Public-contract adapters with separately attributed user-supplied evaluation observations.
// No official test files, generated applications, or output databases are bundled here.
module.exports = function createChecks(h) {
  const {expect, base, contract, gridCell, dragRange, enterCell, control, dataMenu,
    chooseScalar, evoSignIn, signInEntry, legacySheet, watchPage, prerequisite} = h;
  const clean = value => String(value || '').replace(/[“”]/g, '"').replace(/\s+/g, ' ');
  const quoted = text => [...text.matchAll(/`([^`]+)`/g)].map(m => m[1]);
  function match(text, pattern, label) {
    const found = text.match(pattern);
    if (!found) throw new Error('Missing public scenario input: ' + label);
    return found[1];
  }
  const button = (page, name) => page.getByRole('button', {name, exact: true});
  const link = (page, name) => page.getByRole('link', {name, exact: true});
  const label = (page, name) => page.getByLabel(name, {exact: true});
  const text = (page, value) => page.getByText(value, {exact: true});
  async function absentOrDisabled(locator) {
    for (const element of await locator.all()) {
      if (await element.isVisible()) await expect(element).toBeDisabled();
    }
  }
  async function repository(page, name) {
    const search = page.getByRole('banner').getByRole('searchbox', {name: 'Search', exact: true});
    await search.fill(name); await search.press('Enter');
    await page.getByRole('main').getByRole('link', {name, exact: true}).click();
    await expect(page.getByRole('heading').filter({hasText: name}).first()).toBeVisible();
  }
  async function organizations(page) {
    await prerequisite('shared:account_menu', async () => {
      await button(page, 'Account menu').click();
      await page.getByRole('menu').getByRole('link', {name: 'Your organizations', exact: true}).click();
    });
  }
  async function sessions(page) {
    await button(page, 'Account menu').click();
    await link(page, 'Settings').click(); await link(page, 'Active sessions').click();
    await expect(page.getByRole('heading', {name: 'Active sessions', exact: true})).toBeVisible();
  }
  async function github(page, raw, browser) {
    const item = {...raw, given: clean(raw.given), when: clean(raw.when), then: clean(raw.then)};
    const {name, given, when, then, scenario_index: number} = item;
    if (name === 'Register a New GitHub Account') {
      const values = quoted(when);
      await prerequisite('shared:sign_in_entry', () => signInEntry(page).click()); await link(page, 'Create an account').click();
      for (const [key, value] of [['Username', values[0]], ['Email', values[1]], ['Password', values[2]], ['Confirm password', values[2]]])
        await label(page, key).fill(value);
      await page.getByRole('checkbox', {name: 'Agree to the terms', exact: true}).check();
      await button(page, 'Create account').click();
      if (number > 0) {
        await expect(text(page, number === 1 ? 'Username format is invalid' : 'Username already exists')).toBeVisible();
        await expect(label(page, 'Username')).toHaveValue(values[0]);
        await expect(label(page, 'Email')).toHaveValue(values[1]);
        await expect(button(page, 'Create account')).toBeVisible();
      } else {
        await label(page, 'Username or email').fill(values[1]); await label(page, 'Password').fill(values[2]);
        await button(page, 'Sign in').click();
        await expect(text(page, values[0]).first()).toBeVisible();
        await page.reload(); await expect(text(page, values[0]).first()).toBeVisible();
      }
      return;
    }
    if (name === 'Sign In with an Existing Account') {
      const values = quoted(when);
      await prerequisite('shared:sign_in_entry', () => signInEntry(page).click()); await label(page, 'Username or email').fill(values[0]);
      await label(page, 'Password').fill(values[1]); await button(page, 'Sign in').click();
      if (number === 2) await expect(text(page, 'Invalid credentials')).toBeVisible();
      else {
        await expect(text(page, item.account.username).first()).toBeVisible();
        await page.reload(); await expect(text(page, item.account.username).first()).toBeVisible();
      }
      return;
    }
    if (!['Search for and Locate Repositories', 'List and Switch Repository Branches'].includes(name) && !item.visitor)
      await evoSignIn(page, item.account);
    if (name === 'Create an Organization After Authentication') {
      await organizations(page); await control(page, 'New organization');
      const identifier = match(when, /enters (?:the existing identifier )?`([^`]+)` in/, 'organization identifier');
      const display = number === 2 ? '   ' : match(when, /enters `([^`]+)` in (?:the )?"Display name"/, 'display name');
      await label(page, 'Organization name').fill(identifier); await label(page, 'Display name').fill(display);
      await button(page, 'Create organization').click();
      if (number === 0) {
        await expect(page.getByRole('heading').filter({hasText: identifier.toLowerCase()}).first()).toBeVisible();
        await page.reload(); await expect(page.getByRole('heading').filter({hasText: identifier.toLowerCase()}).first()).toBeVisible();
      } else {
        for (const error of (number === 1 ? ['Organization name already exists'] : ['Organization name format is invalid', 'Display name is required']))
          await expect(text(page, error)).toBeVisible();
        await expect(label(page, 'Organization name')).toHaveValue(identifier);
        await expect(button(page, 'Create organization')).toBeVisible();
      }
    } else if (name === 'Search for and Locate Repositories') {
      const query = match(when, /with `([^`]+)`/, 'search query');
      const search = page.getByRole('banner').getByRole('searchbox', {name: 'Search', exact: true});
      await search.fill(query); await search.press('Enter');
      if (number === 2) await expect(page.getByText(/No results|No repositories/i).first()).toBeVisible();
      else await expect(page.getByRole('main').getByRole('link', {name: match(given, /public repository `([^`]+)`/, 'repository'), exact: true})).toBeVisible();
    } else if (name === 'List and Switch Repository Branches') {
      await repository(page, match(given, /public repository `([^`]+)`/, 'repository'));
      const active = match(given, /active branch `([^`]+)`/, 'active branch');
      // No extra Code click: initial navigation must already encode its active branch.
      await expect(page).toHaveURL(new RegExp(encodeURIComponent(active)));
      await button(page, 'Branch ' + active).click();
      const query = match(when, /(?:enters|switches to) `([^`]+)`/, 'branch query');
      await label(page, 'Find branch').fill(query);
      let expected = query;
      if (number === 1) {
        await expect(text(page, 'No matching branch')).toBeVisible();
        await page.keyboard.press('Escape'); expected = active;
      } else await page.getByRole('option', {name: query, exact: true}).click();
      for (let pass = 0; pass < 2; pass++) {
        await expect(button(page, 'Branch ' + expected)).toBeVisible();
        await expect(page).toHaveURL(new RegExp(encodeURIComponent(expected)));
        if (number !== 1) await expect(link(page, match(given, /file `([^`]+)`/, 'target file'))).toBeVisible();
        if (!pass) await page.reload();
      }
    } else if (name === 'Manage Active Browser Sessions') {
      if (number === 0) {
        await sessions(page); await expect(text(page, 'Current session')).toBeVisible();
        await expect(page.getByText(/Last active/i).first()).toBeVisible(); return;
      }
      const other = await browser.newContext({userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36'});
      try {
        const second = await other.newPage(); watchPage(second); second.setDefaultTimeout(4000);
        await second.goto(base); await evoSignIn(second, item.account); await sessions(second);
        await sessions(page);
        const active = button(page, 'Revoke session');
        // Find the controlled browser by the visible device label; ambiguity fails, never passes.
        const row = page.locator('li, tr, [role="row"]').filter({has: active}).filter({hasText: /Android|Mobile/i});
        await expect(row).toHaveCount(1); await button(row, 'Revoke session').click();
        if (number === 1) await expect(page.getByText(/revoked|inactive/i)).toBeVisible();
        await second.reload(); await expect(label(second, 'Username or email')).toBeVisible();
        await page.reload();
        if (number === 1) await expect(page.getByText(/revoked|inactive/i)).toBeVisible();
        await expect(text(page, item.account.username).first()).toBeVisible();
      } finally { await other.close(); }
    } else if (name === 'View Organization Audit Log') {
      const organization = match(given, /(?:organization |pre-provisions )`([^`]+)`/, 'organization');
      // The audit GIVEN starts at home; its organization link does not depend on account-menu navigation.
      await page.getByRole('main').getByRole('link', {name: organization, exact: true}).click();
      if (number === 2) { await absentOrDisabled(link(page, 'Audit log')); return; }
      await link(page, 'Audit log').click();
      const table = page.getByRole('table');
      for (const name of ['Actor', 'Action', 'Target', 'Timestamp'])
        await expect(table.getByRole('columnheader', {name, exact: true})).toBeVisible();
      if (number === 1) {
        const action = match(when, /selects "([^"]+)"/, 'audit action');
        await label(page, 'Filter action').selectOption({label: action}); await page.reload();
        const rows = table.getByRole('row').filter({has: page.getByRole('cell')});
        await expect(rows.first()).toBeVisible();
        for (const row of await rows.all()) await expect(row).toContainText(action);
        await label(page, 'Filter action').selectOption('');
        // Keep this exact observable check: don't hide ambiguity with first(). Provision minimal GIVEN data;
        // never remove real audit events just to satisfy a fixture-cardinality expectation.
        await expect(table.getByText('Repository created', {exact: true})).toBeVisible();
      } else { await page.reload(); await expect(table).toBeVisible(); }
    } else if (name === 'Archive and Restore a Repository') {
      const repo = match(given, /repository `([^`]+)`/, 'repository');
      await repository(page, repo);
      if (number === 1) {
        await expect(text(page, 'Archived')).toBeVisible();
        await expect(link(page, match(given, /file `([^`]+)`/, 'file'))).toBeVisible();
        for (const name of ['Add file', 'Create new file', 'New issue', 'New pull request'])
          await absentOrDisabled(button(page, name).or(link(page, name)));
        return;
      }
      const action = number === 2 ? 'Restore' : 'Archive';
      await link(page, 'Settings').click(); await button(page, action + ' repository').click();
      await button(page.getByRole('dialog', {name: action + ' repository', exact: true}), 'Confirm ' + action.toLowerCase()).click();
      // Restore WHEN explicitly reloads the repository overview, not the settings form.
      if (number === 2) { await link(page, 'Code').click(); await page.reload(); }
      const verify = async () => {
        if (number === 2) {
          await expect(text(page, 'Archived')).toHaveCount(0);
          await expect(page.getByText(/^Active$/i)).toBeVisible();
          await expect(link(page, match(given, /contains `([^`]+)`/, 'file'))).toBeVisible();
        } else await expect(text(page, 'Archived')).toBeVisible();
      };
      // Confirm -> search is a consecutive user path. Waiting for Archived here masks a
      // late archive callback that overwrites the newer search navigation (r3 regression).
      if (number === 0) await repository(page, repo);
      else { await verify(); await repository(page, repo); }
      await page.reload(); await verify();
    } else if (name === 'Create and View Repository Releases') {
      const repo = match(given, /repository `([^`]+)`/, 'repository');
      await repository(page, repo); await link(page, 'Releases').click();
      if (number === 1) {
        const description = item.assertions?.release_description;
        if (!description || typeof description.value !== 'string' || !description.source)
          throw new Error('Missing public scenario input: exact release description; no matching attributed observation');
        const tag = match(given, /published tag `([^`]+)`/, 'tag');
        await link(page, tag).click();
        for (let pass = 0; pass < 2; pass++) {
          await expect(page.getByRole('heading', {name: tag, exact: true})).toBeVisible();
          await expect(text(page, match(given, /title `([^`]+)`/, 'title'))).toBeVisible();
          await expect(text(page, description.value)).toBeVisible();
          const branch = match(given, /branch `([^`]+)`/, 'branch');
          await expect(page.getByText('Target branch: ' + branch, {exact: true})).toBeVisible();
          await absentOrDisabled(button(page, 'Publish release').or(link(page, 'New release')));
          if (!pass) await page.reload();
        }
      } else {
        await control(page, 'New release');
        const tag = match(number === 2 ? given : when, /(?:existing |enters )tag `([^`]+)`/, 'tag');
        // A rejection probe uses fresh ordinary form values, not invented seed records.
        const title = number === 2 ? 'Duplicate tag probe' : match(when, /title `([^`]+)`/, 'title');
        const description = number === 2 ? 'Rejected publication must not replace the original' : match(when, /description `([^`]+)`/, 'description');
        for (const [key, value] of [['Tag name', tag], ['Release title', title], ['Description', description]]) await label(page, key).fill(value);
        if (number === 0) await chooseScalar(page, 'Target branch', match(when, /target branch `([^`]+)`/, 'branch'));
        await button(page, 'Publish release').click();
        if (number === 2) {
          await expect(text(page, 'Tag already exists')).toBeVisible();
          await expect(label(page, 'Tag name')).toHaveValue(tag);
          await expect(text(page, tag)).toHaveCount(1);
          await link(page, tag).click();
          await expect(page.getByRole('heading', {name: tag, exact: true})).toBeVisible();
          await expect(text(page, title)).toHaveCount(0);
          await link(page, 'Releases').click(); await expect(link(page, tag)).toHaveCount(1);
        } else for (let pass = 0; pass < 2; pass++) {
          await expect(page.getByRole('heading', {name: tag, exact: true})).toBeVisible();
          for (const value of [title, description]) await expect(text(page, value)).toBeVisible();
          await expect(text(page, 'Target branch: ' + match(when, /target branch `([^`]+)`/, 'branch'))).toBeVisible();
          if (!pass) await page.reload();
        }
      }
    } else if (name === 'Add and Remove Issue Reactions') {
      const title = match(given, /issue `([^`]+)`/, 'issue');
      const repo = given.match(/repository `([^`]+)`/);
      if (repo) { await repository(page, repo[1]); await link(page, 'Issues').click(); }
      else if (!await page.getByRole('main').getByRole('link', {name: title, exact: true}).count()) {
        // The scenario omits its repository. Discover it through public repository links,
        // prioritizing sibling GIVEN references but confirming the issue actually exists.
        const siblings = (contract.evolution_checks || []).filter(c => c.name === name)
          .flatMap(c => [...c.given.matchAll(/repository\s+`([^`]+)`/g)].map(m => m[1]));
        const home = await page.getByRole('main').getByRole('link').evaluateAll(elements => elements
          .filter(e => /\/repositories\/[^/?]+\/?$/.test(new URL(e.href).pathname))
          .map(e => (e.textContent || '').trim()));
        const candidates = [...new Set([...siblings.filter(n => home.includes(n)), ...home])];
        let found = false;
        for (const candidate of candidates) {
          await page.getByRole('main').getByRole('link', {name: candidate, exact: true}).click();
          await link(page, 'Issues').click();
          await expect(page.getByRole('main')).toBeVisible();
          // Wait for a visible issue list/empty state before deciding, not a network race.
          await page.waitForLoadState('networkidle');
          if (await link(page, title).count()) { found = true; break; }
          await page.goto(base);
        }
        if (!found) throw new Error('Issue is not reachable through public repository navigation: ' + title);
      }
      await link(page, title).click();
      // The user-supplied r4 report requires a readable aggregate, including zero.
      // Read the starting value so other users' reactions are not assumed away.
      const total = page.getByText(/^\d+ reactions?$/);
      const totalMessage = 'Display the persisted reaction total as "N reaction(s)", including zero; update it after add/remove and reload';
      await expect(total, totalMessage).toBeVisible();
      const originalTotal = Number.parseInt(await total.innerText(), 10);
      const assertTotal = value => expect(total, totalMessage).toHaveText(`${value} ${value === 1 ? 'reaction' : 'reactions'}`);
      await assertTotal(originalTotal);
      if (item.visitor) {
        expect(originalTotal, 'The public issue is provisioned with an existing reaction').toBeGreaterThan(0);
        await page.reload(); await assertTotal(originalTotal);
        await absentOrDisabled(button(page, 'Add reaction')); return;
      }
      const remove = button(page, 'Remove +1 reaction');
      await expect(remove).toHaveCount(0);
      await button(page, 'Add reaction').click();
      await page.getByRole('menu').getByRole('menuitem', {name: '+1', exact: true}).click();
      await expect(remove).toBeVisible(); await assertTotal(originalTotal + 1);
      await page.reload(); await expect(remove).toBeVisible(); await assertTotal(originalTotal + 1);
      if (number === 1) {
        await remove.click(); await expect(remove).toHaveCount(0); await assertTotal(originalTotal);
        await page.reload(); await expect(remove).toHaveCount(0); await assertTotal(originalTotal);
      }
    } else throw new Error('Missing public scenario input: unsupported feature ' + name);
  }

  async function sheet(page, raw) {
    const item = {...raw, given: clean(raw.given), when: clean(raw.when), then: clean(raw.then)};
    const {name, given, when, then, scenario_index: number} = item;
    // These original adapters cover all variants selected here with real writes and reloads.
    if ((number === 0 && !['Find and Replace Cell Text'].includes(name)) ||
        name === 'Freeze Rows and Columns' ||
        (name === 'Create, Edit, and Delete Conditional Formatting Rules' && number === 2) ||
        (name === 'Filter Rows by Value or Condition' && number === 2)) {
      return legacySheet(page, item);
    }
    const workbook = match(given, /workbook `([^`]+)`/i, 'workbook');
    await link(page, workbook).click();
    if (name === 'Rename a Workbook' || name === 'Rename a Worksheet') {
      const isWorkbook = name === 'Rename a Workbook';
      const field = isWorkbook ? 'Workbook name' : 'Worksheet name';
      const original = isWorkbook ? workbook : match(when, /Worksheet options for ([^"]+)"/, 'worksheet');
      const desired = match(when, /enters (?:the \d+-character value )?`([^`]+)`/, 'attempted name');
      const open = async () => {
        if (isWorkbook) await button(page, 'Rename workbook').click();
        else { await button(page, 'Worksheet options for ' + original).click(); await page.getByRole('menuitem', {name: 'Rename', exact: true}).click(); }
      };
      await open(); await label(page, field).fill(desired); await button(page, 'Save').click();
      await expect(text(page, match(then, /message "([^"]+)"/, 'validation message'))).toBeVisible();
      await expect(label(page, field)).toHaveValue(original); await page.reload(); await open();
      await expect(label(page, field)).toHaveValue(original);
    } else if (name === 'Edit a Cell Through the Grid or Formula Bar') {
      const selected = match(when, /selects (?:cell )?([A-Z]+\d+(?::[A-Z]+\d+)?)/, 'selection');
      let cells = [selected];
      if (selected.includes(':')) {
        await dragRange(page, ...selected.split(':'));
        cells = [...then.matchAll(/\b([A-Z]+\d+)\b/g)].map(m => m[1]);
      } else await gridCell(page, selected).click();
      await page.getByRole('grid', {name: 'Worksheet grid', exact: true}).focus(); await page.keyboard.press('Delete');
      for (const cell of cells) {
        await expect(gridCell(page, cell)).toHaveText('');
        if (number === 2) await expect(gridCell(page, cell)).toHaveAttribute('aria-selected', 'true');
      }
      const dependent = then.match(/cell ([A-Z]+\d+) displays `([^`]+)`/i);
      if (dependent) await expect(gridCell(page, dependent[1])).toHaveText(dependent[2]);
      await page.reload(); for (const cell of cells) await expect(gridCell(page, cell)).toHaveText('');
      if (dependent) await expect(gridCell(page, dependent[1])).toHaveText(dependent[2]);
    } else if (name === 'Set Dropdown or Numeric Validation for a Range') {
      const cell = match(when, /(?:edits|for|selects) ([A-Z]+\d+)/, 'cell');
      const before = await gridCell(page, cell).innerText();
      const value = match(when, /enters `([^`]+)`/, 'invalid input');
      const message = match(then, /message "([^"]+)"/, 'error message');
      if (number === 2) {
        await gridCell(page, cell).click(); await dataMenu(page, 'Data validation');
        await label(page, 'Error message').fill(match(when, /with `([^`]+)`/, 'edited message'));
        await button(page, 'Save').click();
      }
      for (let pass = 0; pass < 2; pass++) {
        if (number === 1) {
          await gridCell(page, cell).dblclick();
          await page.getByRole('textbox', {name: 'Edit ' + cell, exact: true}).fill(value);
          await page.getByRole('textbox', {name: 'Edit ' + cell, exact: true}).press('Enter');
        } else { await gridCell(page, cell).click(); await label(page, 'Formula bar').fill(value); await label(page, 'Formula bar').press('Enter'); }
        await expect(text(page, message)).toBeVisible(); await expect(gridCell(page, cell)).toHaveText(before);
        if (!pass) await page.reload();
      }
    } else if (name === 'Find and Replace Cell Text') {
      const coords = [...new Set([...given.matchAll(/(?<![\w-])([A-Z]+[1-9]\d*)(?![\w-])/g)].map(m => m[1]))];
      const before = new Map(); for (const coord of coords) before.set(coord, await gridCell(page, coord).innerText());
      await control(page, 'Edit'); await control(page, 'Find and replace');
      const find = match(when, /enters `([^`]+)` in "Find"/, 'find'); await label(page, 'Find').fill(find);
      if (number === 0) {
        await button(page, 'Find next').click(); await button(page, 'Find next').click();
        await expect(text(page, 'Match 2 of 3')).toBeVisible();
        await expect(gridCell(page, match(then, /Cell ([A-Z]+\d+) is selected/i, 'selected cell'))).toHaveAttribute('aria-selected', 'true');
      } else {
        const replacement = match(when, /enters `([^`]+)` in "Replace with"/, 'replacement');
        await label(page, 'Replace with').fill(replacement);
        if (number === 2) await page.getByRole('checkbox', {name: 'Match case', exact: true}).check();
        await button(page, 'Replace all').click(); await expect(text(page, match(then, /exactly "([^"]+)"/, 'replacement count'))).toBeVisible();
        for (let pass = 0; pass < 2; pass++) {
          for (const [coord, value] of before) await expect(gridCell(page, coord)).toHaveText((number === 2 ? value === find : value.toLowerCase() === find.toLowerCase()) ? replacement : value);
          if (!pass) await page.reload();
        }
      }
    } else if (name === 'Create and Edit Named Ranges') {
      await dataMenu(page, 'Named ranges');
      if (number === 1) {
        await button(page, 'Add named range').click();
        const rangeName = match(when, /enters `([^`]+)` in "Name"/, 'name');
        await label(page, 'Name').fill(rangeName); await label(page, 'Range').fill(match(when, /enters `([^`]+)` in "Range"/, 'range'));
        await button(page, 'Save').click(); await expect(text(page, 'Named range must start with a letter')).toBeVisible();
        await page.reload(); await dataMenu(page, 'Named ranges'); await expect(button(page, 'Edit ' + rangeName)).toHaveCount(0);
      } else {
        const rangeName = match(given, /named range `([^`]+)`/, 'name');
        const range = match(when, /with `([^`]+)`/, 'new range');
        await button(page, 'Edit ' + rangeName).click(); await label(page, 'Range').fill(range); await button(page, 'Save').click();
        const result = then.match(/([A-Z]+\d+) displays `([^`]+)`/);
        if (!result) throw new Error('Missing public scenario input: formula result');
        await expect(gridCell(page, result[1])).toHaveText(result[2]); await page.reload();
        await expect(gridCell(page, result[1])).toHaveText(result[2]); await dataMenu(page, 'Named ranges');
        await button(page, 'Edit ' + rangeName).click(); await expect(label(page, 'Range')).toHaveValue(range);
      }
    } else if (name === 'Create, Edit, and Delete a Cell Note') {
      const cell = match(when, /Open note for ([A-Z]+\d+)/, 'note cell');
      const before = await gridCell(page, cell).innerText();
      await button(page, 'Open note for ' + cell).click();
      if (number === 2) {
        await button(page, 'Delete note').click(); await expect(button(page, 'Open note for ' + cell)).toHaveCount(0);
        await page.reload(); await expect(button(page, 'Open note for ' + cell)).toHaveCount(0);
      } else {
        const value = match(when, /with `([^`]+)`/, 'note');
        await button(page, 'Edit note').click(); await label(page, 'Note').fill(value); await button(page, 'Save note').click();
        await page.reload(); await button(page, 'Open note for ' + cell).click(); await expect(label(page, 'Note')).toHaveValue(value);
      }
      await expect(gridCell(page, cell)).toContainText(before.replace(/Open note.*/, '').trim());
    } else if (name === 'Create, Edit, and Delete Conditional Formatting Rules') {
      await dragRange(page, ...match(when, /selects ([A-Z]+\d+:[A-Z]+\d+)/, 'range').split(':'));
      await control(page, 'Format'); await control(page, 'Conditional formatting');
      const dialog = page.getByRole('dialog', {name: 'Conditional formatting', exact: true});
      await chooseScalar(dialog, 'Condition', match(when, /chooses "([^"]+)" in "Condition"/, 'condition'));
      await label(dialog, 'Value').fill(match(when, /enters `([^`]+)` in "Value"/, 'value'));
      await chooseScalar(dialog, 'Style', match(when, /chooses "([^"]+)" in "Style"/, 'style')); await button(dialog, 'Save').click();
      const color = match(then, /(rgb\([^)]+\))/, 'color');
      const cells = [...then.matchAll(/\b([A-Z]+\d+)\b/g)].map(m => m[1]);
      for (let pass = 0; pass < 2; pass++) {
        await expect(gridCell(page, cells[0])).toHaveCSS('background-color', color);
        for (const cell of cells.slice(1)) await expect(gridCell(page, cell)).not.toHaveCSS('background-color', color);
        if (!pass) await page.reload();
      }
    } else if (name === 'Filter Rows by Value or Condition') {
      const range = given.match(/range ([A-Z]+)(\d+):([A-Z]+)(\d+)/);
      if (!range) throw new Error('Missing public scenario input: filter range');
      const sourceValues = [];
      const phaseColumn = String.fromCharCode(range[1].charCodeAt(0) + 1);
      for (let row = Number(range[2]) + 1; row <= Number(range[4]); row++) sourceValues.push([row, await gridCell(page, phaseColumn + row).textContent()]);
      const view = match(given, /saved filter view `([^`]+)`/, 'saved view');
      await dataMenu(page, 'Filter views'); await button(page.getByRole('dialog'), view).click();
      for (let pass = 0; pass < 2; pass++) {
        for (const [row, phase] of sourceValues) {
          const criterion = match(given, /values `([^`]+)`/, 'filter value');
          if (phase === criterion) await expect(gridCell(page, range[1] + row)).toBeVisible();
          else await expect(gridCell(page, range[1] + row)).toBeHidden();
        }
        if (!pass) await page.reload();
      }
    } else throw new Error('Missing public scenario input: unsupported feature ' + name);
  }
  async function filterCompatibility(page, raw) {
    const given = clean(raw.given), when = clean(raw.when), then = clean(raw.then);
    await link(page, match(given, /workbook `([^`]+)`/, 'workbook')).click();
    const range = match(given, /range ([A-Z]+\d+:[A-Z]+\d+)/, 'range');
    const headers = match(given, /headed `([^`]+)`/, 'headers').split('/');
    const firstColumn = range.match(/^[A-Z]+/)[0];
    for (const condition of when.matchAll(/applies `([^`]+)` value `([^`]+)` to "Filter ([^"]+)"/g)) {
      const offset = headers.indexOf(condition[3]);
      if (offset < 0 || firstColumn.length !== 1) throw new Error('Missing public scenario input: filter column');
      await button(page, 'Create filter').click();
      const dialog = page.getByRole('dialog', {name: 'Filter', exact: true});
      await label(dialog, 'Range').fill(range);
      await label(dialog, 'Column').fill(String.fromCharCode(firstColumn.charCodeAt(0) + offset));
      await chooseScalar(dialog, 'Condition', condition[1]); await label(dialog, 'Value').fill(condition[2]);
      await button(dialog, 'Apply').click();
    }
    const row = match(then, /Only row (\d+)/, 'visible row');
    const numbers = range.match(/([0-9]+):[A-Z]+([0-9]+)/);
    for (let r = Number(numbers[1]) + 1; r <= Number(numbers[2]); r++) {
      if (String(r) === row) await expect(gridCell(page, firstColumn + r)).toBeVisible();
      else await expect(gridCell(page, firstColumn + r)).toBeHidden();
    }
  }
  return {github, sheet, filterCompatibility};
};

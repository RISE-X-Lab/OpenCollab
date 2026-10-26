import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {searchDocuments, needsChoice} from '../help/assets/help.mjs';

const index = JSON.parse(fs.readFileSync(new URL('../help/zh-CN/search-index.json', import.meta.url), 'utf8'));

test('every curated phrase and reader question reaches its intended guide', () => {
  let checked = 0;
  for (const route of index.routes) {
    for (const query of route.queries) {
      assert.equal(searchDocuments(index, query)[0]?.doc.id, route.target_id, query);
      checked++;
    }
  }
  assert.ok(checked > 0);
  console.log(`Checked ${checked} documented search phrases`);
});

test('ambiguous delegation offers both child workflow choices', () => {
  assert.equal(needsChoice(index, '子工作流'), true);
  const first = searchDocuments(index, '子工作流').slice(0, 2).map(hit => hit.doc.id);
  assert.deepEqual(first, ['team-run-workflow', 'workflow-composition-hierarchy']);
  assert.equal(needsChoice(index, '隔离'), false);
});

test('API spelling, case and full-width equivalents resolve consistently', () => {
  for (const word of ['SPAWN_AGENT', 'spawn agent', 'ＳＰＡＷＮ＿ＡＧＥＮＴ']) {
    assert.equal(searchDocuments(index, word)[0]?.doc.id, 'dispatch-team-agents');
  }
  assert.equal(searchDocuments(index, 'Workflow语法')[0]?.doc.id, 'workflow-programming-model');
  assert.deepEqual(searchDocuments(index, ''), []);
  assert.deepEqual(searchDocuments(index, 'unrelated-no-matching-topic-7316'), []);
});

test('links resolve on GitHub project subpaths and deeply nested articles', () => {
  const root = new URL('https://rise-x-lab.github.io/OpenCollab/help/zh-CN/search-index.json');
  const nested = new URL('../search-index.json', new URL('isolation-overview/index.html', root));
  assert.equal(nested.href, root.href);
  const hit = searchDocuments(index, '隔离')[0];
  assert.equal(new URL(hit.doc.url, nested).href,
    'https://rise-x-lab.github.io/OpenCollab/help/zh-CN/isolation-overview/index.html');
});

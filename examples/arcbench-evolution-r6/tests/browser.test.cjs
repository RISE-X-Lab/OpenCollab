const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');

test('Chromium, Node adapters and real SQLite delivery fixtures', {timeout: 240000}, () => {
  const root=path.resolve(__dirname,'..');
  const python=process.env.OPENCOLLAB_TEST_PYTHON || 'python3';
  const result=spawnSync(python,['-m','pytest','-q',path.join(__dirname,'test_browser_delivery.py')],{
    cwd:path.resolve(root,'../..'),env:{...process.env,ARCBENCH_BROWSER_TESTS:'1'},
    encoding:'utf8',timeout:230000});
  assert.equal(result.status,0,result.stdout+'\n'+result.stderr);
  console.log(result.stdout);
});

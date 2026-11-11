const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../desktop/plugin.js'), 'utf8');
assert.match(source, /restCall\(ctx, 'GET', '\/prompt-review'\)/, 'wizard loads the actual review');
assert.match(source, /restCall\(ctx, 'POST', '\/prompt-review\/apply'/, 'review requires an explicit apply');
assert.match(source, /Skip review/, 'review remains optional');
assert.match(source, /AGENTS\.md.*project-scoped/, 'project-scoped AGENTS.md is explained');
console.log('prompt review UI contract checks passed');

async function projectTests() {
  const match = source.match(/async function createProjectAgents[\s\S]*?\n}\n/);
  assert.ok(match, 'project create helper exists');
  const create = new Function('return (' + match[0] + ')')();
  let calls = [];
  const files = { writeText: async (...args) => { calls.push(args); return {ok:true}; }, readText: async () => ({ok:true,text:'approved'}) };
  await create(files, {id:'grant', path:'/chosen/project'}, 'approved');
  assert.deepEqual(calls, [['grant', 'AGENTS.md', 'approved', {exclusive:true}]]);
  files.writeText = async () => ({ok:false,error:'exists',message:'exists'});
  await assert.rejects(() => create(files, {id:'grant'}, 'approved'), /exists/);
  await assert.rejects(() => create(files, null, 'approved'));
  files.writeText = async () => ({ok:true});
  files.readText = async () => ({ok:true,text:'changed concurrently'});
  await assert.rejects(() => create(files, {id:'grant'}, 'approved'), /could not be verified/);
  const os = require('node:os');
  const path = require('node:path');
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'butler-project-test-'));
  try {
    const realFiles = {
      writeText: async (id, name, text, opts) => {
        try { fs.writeFileSync(path.join(dir, name), text, {flag: opts.exclusive ? 'wx' : 'w'}); return {ok:true}; }
        catch (error) { return {ok:false,message:error.code}; }
      },
      readText: async (id, name) => ({ok:true,text:fs.readFileSync(path.join(dir, name), 'utf8')}),
    };
    await create(realFiles, {id:'picked'}, 'first');
    await assert.rejects(() => create(realFiles, {id:'picked'}, 'overwrite'), /EEXIST/);
    assert.equal(fs.readFileSync(path.join(dir, 'AGENTS.md'), 'utf8'), 'first');
  } finally { fs.rmSync(dir, {recursive:true,force:true}); }
  assert.match(source, /requestFolder\(\)/);
  assert.match(source, /Choose project folder/);
}
projectTests().catch(err => { console.error(err); process.exitCode = 1; });

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../server.py', import.meta.url), 'utf8');
const jsStart = source.indexOf('BOARD = """\n<script>\n') + 'BOARD = """\n<script>\n'.length;
const jsEnd = source.indexOf('\n</script>', jsStart);
const js = source.slice(jsStart, jsEnd);

function functionSource(name) {
  const from = js.indexOf('function ' + name + '(');
  return js.slice(from, js.indexOf('\n}', from) + 2);
}

test('board columns are the seven local workflow states', () => {
  const cols = js.match(/const COLS = \[([^\]]+)\]/)[1];
  assert.deepEqual(
    cols.match(/'([a-z_]+)'/g).map(s => s.slice(1, -1)),
    ['backlog', 'todo', 'in_progress', 'human_review', 'merging', 'rework', 'done']);
});

test('applyFilter hides only non-matching cards', () => {
  const shown = [];
  const hidden = [];
  const cards = [
    {search: 'gh-1 alpha o/r'},
    {search: 'gh-2 beta o/r'},
  ].map((c, i) => ({
    getAttribute(name) { return name === 'data-search' ? c.search : null; },
    set hidden(v) { (v ? hidden : shown).push(i); this._h = v; },
    get hidden() { return this._h; },
  }));
  const doc = {
    getElementById(id) {
      if (id === 'q') { return {value: doc.q}; }
      return {querySelectorAll(sel) { return cards; }};
    },
    q: '',
  };
  const applyFilter = new Function('document', 'COLS',
    functionSource('applyFilter') + '\nreturn applyFilter;')(doc, ['backlog']);
  doc.q = '';
  hidden.length = shown.length = 0;
  applyFilter();
  assert.deepEqual(shown.sort(), [0, 1], 'empty query must not hide cards');
  doc.q = 'alpha';
  hidden.length = shown.length = 0;
  applyFilter();
  assert.deepEqual(hidden, [1]);
  assert.deepEqual(shown, [0]);
  doc.q = 'nothing';
  hidden.length = shown.length = 0;
  applyFilter();
  assert.deepEqual(hidden.sort(), [0, 1], 'miss hides all');
});

test('resolvePending clears matching states and warns on stale', () => {
  const warnings = [];
  const pending = new Map();
  const resolve = new Function('PENDING', 'showToast', 'Date',
    functionSource('resolvePending') + '\nreturn resolvePending;')(pending, w => warnings.push(w), Date);
  pending.set('GH-1', {state: 'Merging', at: Date.now()});
  resolve({'GH-1': {state: 'Todo'}});
  assert.equal(pending.size, 1, 'different state stays pending');
  resolve({'GH-1': {state: 'Merging'}});
  assert.equal(pending.size, 0);
  pending.set('GH-2', {state: 'Done', at: Date.now() - 61000});
  resolve({'GH-2': {state: 'Rework'}});
  assert.equal(pending.size, 0);
  assert.equal(warnings.length, 1);
});

function makeOps(env) {
  const prelude =
    'let CUR = null, CUR_REV = null, CUR_SOURCE = null, CUR_SOURCE_ERR = null, BUSY = false;\n' +
    'const POSTED = new Map();\n' +
    functionSource('displayedRev') + '\n' +
    functionSource('redrawDrawer') + '\n' +
    'async ' + functionSource('submitMove') + '\n' +
    'async ' + functionSource('submitComment') + '\n' +
    'return {submitMove, submitComment, POSTED,\n' +
    '  openAs(issue, rev, source) { CUR = issue; CUR_REV = rev; CUR_SOURCE = source || null; },\n' +
    '  getRev() { return CUR_REV; }};\n';
  return new Function(
    'postTask', 'FACTS', 'PENDING', 'showToast', 'refreshIssues', 'renderDrawer',
    prelude)(
    env.postTask, env.facts, env.pending || new Map(),
    env.showToast || (() => {}), env.refreshIssues || (async () => {}),
    env.renderDrawer || (() => {}));
}

test('submitMove binds the revision displayed in the open drawer', async () => {
  const posts = [];
  const ops = makeOps({
    postTask: async body => { posts.push(body); return {status: 200, data: {revision: 3, state: 'Merging'}}; },
    facts: {'GH-4': {rev: 2, state: 'Rework'}},  // background refresh bumped FACTS to 2
    renderDrawer: () => {},
  });
  ops.openAs('GH-4', 1, null);  // drawer opened while rev was 1
  await ops.submitMove('GH-4', 'Merging', '');
  assert.equal(posts[0].expected_revision, 1,
    'must submit the revision the user saw, not the refreshed one');
  assert.equal(ops.getRev(), 3, 'success rebinds to the returned revision');
});

test('409 with comment_posted does not repost and rebinds visible revision', async () => {
  const posts = [];
  const rendered = [];
  const facts = {'GH-1': {rev: 7, state: 'Human Review'}};
  const ops = makeOps({
    postTask: async body => {
      posts.push(body);
      return {status: 409, data: {comment_posted: true}};
    },
    facts,
    renderDrawer: (f) => rendered.push({state: f.state, rev: f.rev}),
    refreshIssues: async () => { facts['GH-1'] = {rev: 9, state: 'Rework'}; },
  });
  ops.openAs('GH-1', 7, null);
  await ops.submitMove('GH-1', 'Merging', '批准合并');
  assert.equal(posts.length, 1);
  assert.equal(ops.getRev(), 9, 'drawer rebinds to the freshly shown revision');
  assert.deepEqual(rendered.at(-1), {state: 'Rework', rev: 9});
  await ops.submitMove('GH-1', 'Merging', '批准合并');
  assert.equal(posts[1].expected_revision, 9);
  assert.equal(posts[1].comment, '', 'posted comment must not be sent again');
});

test('submitComment refuses to repost identical published text', async () => {
  const posts = [];
  const toasts = [];
  const ops = makeOps({
    postTask: async body => { posts.push(body); return {status: 200, data: {}}; },
    facts: {},
    showToast: (k, m) => toasts.push(m),
  });
  await ops.submitComment('GH-1', 'same text');
  await ops.submitComment('GH-1', 'same text');
  assert.equal(posts.length, 1, 'identical comment is not reposted');
  assert.match(toasts.at(-1), /已发布/);
});

test('submitMove success tracks the expected state and never resends', async () => {
  const posts = [];
  const pending = new Map();
  let refreshed = 0;
  const ops = makeOps({
    postTask: async body => { posts.push(body); return {status: 200, data: {message: 'ok', revision: 2}}; },
    facts: {'GH-1': {rev: 1}},
    pending,
    refreshIssues: async () => { refreshed++; },
  });
  await ops.submitMove('GH-1', 'Done', '');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].expected_revision, 1);
  assert.equal(pending.get('GH-1').state, 'Done');
  assert.equal(refreshed, 1);
});

test('network failure does not claim success or leave the ui busy', async () => {
  const toasts = [];
  const ops = makeOps({
    postTask: async () => { throw new Error('offline'); },
    facts: {},
    showToast: (kind, msg) => toasts.push(kind + ':' + msg),
  });
  await ops.submitMove('GH-1', 'Done', '');
  assert.match(toasts[0], /err:请求失败/);
});

test('drawer exposes description, blocked reason and merge warning', () => {
  const src = functionSource('renderDrawer');
  assert.match(src, /source\.issue\.body/, 'issue body must be rendered');
  assert.match(src, /f\.blocked/, 'blocked_reason must be rendered');
  assert.match(src, /无法撤销/, 'in-flight merge warning must exist');
  assert.match(src, /来源加载失败/, 'source failure must be visible, not silent');
  assert.match(functionSource('openIssue'), /sourceErr/, 'openIssue tracks source errors');
  assert.match(js, /window\.confirm[\s\S]*无法撤销/, 'Merging move asks for confirmation');
  assert.match(source, /id='cstate'/, 'terminal-state filter exists in the page');
  assert.match(functionSource('renderCompleted'), /wantedState/, 'completed list filters by terminal state');
});

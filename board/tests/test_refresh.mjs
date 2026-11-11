import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../server.py', import.meta.url), 'utf8');
const start = source.indexOf('let ISSUES_REQUEST = null;');
const end = source.indexOf('async function fetchIssues(', start);

test('periodic reads share a request; a completed write queues a fresh read', async () => {
  const pending = [];
  const refresh = new Function('fetchIssues', source.slice(start, end) + '\nreturn refreshIssues;')(
    () => new Promise(resolve => pending.push(resolve))
  );
  const oldRead = refresh();
  assert.equal(refresh(), oldRead);
  const afterWrite = refresh(true);
  assert.equal(pending.length, 1);
  pending[0]();
  await oldRead;
  await Promise.resolve();
  assert.equal(pending.length, 2);
  let completed = false;
  afterWrite.then(() => { completed = true; });
  await Promise.resolve();
  assert.equal(completed, false);
  pending[1]();
  await afterWrite;
  assert.equal(completed, true);
});

test('pending operations require a fresh and matching runtime observation', () => {
  const from = source.indexOf('function confirmOperations(data)');
  const to = source.indexOf('\n}', from) + 2;
  const pending = new Map([['GH-151', {op: 'resume', at: 2000}]]);
  const confirm = new Function('PENDING_OPERATIONS', source.slice(from, to) + '\nreturn confirmOperations;')(pending);
  const facts = {'GH-151': {labels: ['agent']}};
  confirm({facts, github_observed_after: 3, runtime: {'GH-151': 'running'}, state_observed_after: 1});
  assert.equal(pending.size, 1, 'old snapshot cannot confirm a new operation');
  confirm({facts, github_observed_after: 3, runtime: {'GH-151': 'blocked'}, state_observed_after: 3});
  assert.equal(pending.size, 1, 'resume remains pending while blocked');
  confirm({facts, github_observed_after: 3, runtime: {'GH-151': 'running'}, state_observed_after: 3});
  assert.equal(pending.size, 0);
  pending.set('GH-151', {op: 'pause', at: 4000});
  const paused = {'GH-151': {labels: ['needs-human']}};
  confirm({facts: paused, github_observed_after: 5, runtime: {'GH-151': 'running'}, state_observed_after: 5});
  assert.equal(pending.size, 1, 'pause is not confirmed until worker stops');
  confirm({facts: paused, github_observed_after: 5, runtime: {'GH-151': 'blocked'}, state_observed_after: 5});
  assert.equal(pending.size, 0);
});

function functionSource(name) {
  const from = source.indexOf('function ' + name + '(');
  return source.slice(from, source.indexOf('\n}', from) + 2);
}

const operationView = new Function(functionSource('operationView') + '\nreturn operationView;')();
for (const [op, phase, running, lane, text] of [
  ['approve', 'confirmed', false, 'awaiting', '已批准 · 等待核验'],
  ['revoke', 'confirmed', false, 'awaiting', '批准已撤销 · 等待核验'],
  ['pause', 'confirmed', true, 'blocked', '已转人工 · 正在停止执行'],
  ['pause', 'confirmed', false, 'blocked', '等待人工处理'],
  ['resume', 'confirmed', false, 'ready', '已恢复 · 等待调度器核对'],
  ['approve', 'uncertain', false, undefined, '操作结果待确认'],
  ['pause', 'partial', true, undefined, '操作未完整完成 · 正在核对'],
]) {
  test(`${op}/${phase} displays the confirmed result without inventing execution`, () => {
    const view = operationView({op, phase, wasRunning: running}, {state: running ? 'running' : 'blocked'});
    assert.equal(view.state, lane);
    assert.equal(view.text, text);
  });
}
test('resuming published work stays in the merge lane', () => {
  assert.equal(operationView({op: 'resume', phase: 'confirmed'}, {mergePending: true}).state, 'awaiting');
});

for (const [name, mock, outcome] of [
  ['success', async () => ({ok: true, json: async () => ({ok: true})}), 'confirmed'],
  ['rejected', async () => ({ok: false, status: 403, json: async () => ({error: 'denied', outcome: 'not_applied'})}), null],
  ['timeout', async () => { throw new Error('network'); }, 'uncertain'],
  ['partial', async () => ({ok: false, status: 502, json: async () => ({error: 'failed', outcome: 'partial'})}), 'partial'],
  ['invalid response', async () => ({ok: true, json: async () => { throw new Error('invalid'); }}), 'uncertain'],
]) {
  test(`write ${name} handles outcome without repeating the mutation`, async () => {
    const calls = [];
    let requests = 0;
    const src = 'async ' + functionSource('postGithubOperation');
    const post = new Function('fetch', 'trackOperation', src + '\nreturn postGithubOperation;')(
      (...args) => { requests++; return mock(...args); },
      (issue, op, phase = 'confirmed') => calls.push({issue, op, phase})
    );
    try { await post('/api/label', {}, 'GH-1', 'approve'); } catch (err) { }
    assert.equal(requests, 1);
    assert.deepEqual(calls, outcome ? [{issue: 'GH-1', op: 'approve', phase: outcome}] : []);
  });
}

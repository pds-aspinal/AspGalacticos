import test from 'node:test';
import assert from 'node:assert/strict';
import {dueGameweeks, tick} from './worker.mjs';

const bootstrap = {events: [{id: 1, deadline_time: '2026-08-14T17:00:00Z'},
  {id: 8, finished: true, data_checked: true}]};
const fixtures = [{event: 8, finished: true, kickoff_time: '2026-11-09T20:00:00Z'}];
const due = (time, fs = fixtures, bs = bootstrap) => dueGameweeks(bs, fs, new Date(time), 8);

test('winter: final Monday match runs Tuesday at 09:00 UK', () => {
  assert.deepEqual(due('2026-11-10T08:59:00Z'), []);
  assert.deepEqual(due('2026-11-10T09:00:00Z'), [{gw: 8, season: '2026'}]);
  assert.equal(due('2026-11-10T09:10:00Z').length, 1);
  assert.deepEqual(due('2026-11-11T09:00:00Z'), []);
});
test('summer: 9am UK is 08:00 UTC', () => {
  const fs = [{...fixtures[0], kickoff_time: '2026-08-16T15:30:00Z'}];
  assert.deepEqual(due('2026-08-17T07:59:00Z', fs), []);
  assert.equal(due('2026-08-17T08:00:00Z', fs).length, 1);
});
test('clock changes preserve local 9am', () => {
  const fs = [{...fixtures[0], kickoff_time: '2026-10-24T16:30:00Z'}];
  assert.deepEqual(due('2026-10-25T08:59:00Z', fs), []);
  assert.equal(due('2026-10-25T09:00:00Z', fs).length, 1);
});
test('a rearranged final match moves the update to the following morning', () => {
  const fs = [...fixtures, {...fixtures[0], kickoff_time: '2026-11-12T20:00:00Z'}];
  assert.deepEqual(due('2026-11-10T09:00:00Z', fs), []);
  assert.equal(due('2026-11-13T09:00:00Z', fs).length, 1);
});
test('unfinished, undated, unchecked and previously emailed gameweeks are excluded', () => {
  assert.deepEqual(due('2026-11-10T09:00:00Z', [{...fixtures[0], finished: false}]), []);
  assert.deepEqual(due('2026-11-10T09:00:00Z', [{...fixtures[0], kickoff_time: null}]), []);
  assert.deepEqual(due('2026-11-10T09:00:00Z', fixtures,
    {events: [bootstrap.events[0], {...bootstrap.events[1], data_checked: false}]}), []);
  assert.deepEqual(dueGameweeks(bootstrap, fixtures, new Date('2026-11-10T09:00:00Z'), 9), []);
});

const env = {MIN_GAMEWEEK: '8', GITHUB_REPOSITORY: 'owner/repo', GITHUB_REF: 'main', GITHUB_TOKEN: 'test'};
const title = 'GW recap 2026 GW8';
async function simulate(runs, failStatus = false) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url.endsWith('/bootstrap-static/')) return Response.json(bootstrap);
    if (url.endsWith('/fixtures/')) return Response.json(fixtures);
    if (url.includes('/runs?')) return failStatus ? new Response('', {status: 503}) : Response.json({workflow_runs: runs});
    return new Response(null, {status: 204});
  };
  try { await tick(env, new Date('2026-11-10T09:00:00Z')); }
  finally { globalThis.fetch = original; }
  return calls.filter(c => c.options.method === 'POST');
}
test('dispatches explicit season and gameweek', async () => {
  const posts = await simulate([]);
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), {ref: 'main', inputs: {gameweek: '8', season: '2026'}});
});
test('successful or active run prevents another dispatch', async () => {
  assert.equal((await simulate([{display_title: title, status: 'completed', conclusion: 'success'}])).length, 0);
  assert.equal((await simulate([{display_title: title, status: 'queued', conclusion: null}])).length, 0);
});
test('failed runs retry, with a three-attempt limit', async () => {
  const failed = {display_title: title, status: 'completed', conclusion: 'failure'};
  assert.equal((await simulate([failed])).length, 1);
  await assert.rejects(simulate([failed, failed, failed]), /three failed attempts/);
});
test('status API failure stops dispatch', async () => {
  await assert.rejects(simulate([], true), /503/);
});

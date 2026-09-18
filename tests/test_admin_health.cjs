const {test} = require('node:test');
const assert = require('node:assert/strict');
const {suggestions} = require('../static/admin/health.js');

const now = Date.parse('2026-09-18T12:00:00Z');
const week = (gameweek, deadline_time, eligible = false) => ({gameweek, deadline_time, eligible});
const report = overrides => ({system: {archive: {enabled: true}}, ...overrides});

test('only the nearest future deadline prompts capture, with verified forecasts respected', () => {
  const gameweeks = [week(3, '2026-10-01'), week(1, '2026-09-01'), week(2, '2026-09-20')];
  assert.match(suggestions(report({gameweeks}), now)[0].title, /GW2/);
  gameweeks[2].eligible = true;
  assert.deepEqual(suggestions(report({gameweeks}), now), []);
  assert.deepEqual(suggestions(report({gameweeks: [week(1, new Date(now).toISOString())]}), now), []);
});

test('disabled or failed archives prompt repair before a new capture', () => {
  for (const archive of [{enabled: false}, {enabled: true, last_result: {status: 'failed'}}]) {
    const items = suggestions({system: {archive}, gameweeks: [week(2, '2026-09-20')]}, now);
    assert.deepEqual(items.map(i => i.id), ['archive']);
  }
});

test('missing and failed instances of the same model produce one diagnostic', () => {
  const items = suggestions({system: {models: [
    {name: 'ridge', loaded: false, last_inference: 'failed'},
    {name: 'ridge', loaded: true, last_inference: 'failed'},
    {name: 'catboost', loaded: true, last_inference: 'succeeded'},
  ]}}, now);
  assert.equal(items.length, 1);
  assert.equal(items[0].detail.match(/ridge/g).length, 1);
  assert.doesNotMatch(items[0].detail, /catboost/);
});

test('feature gaps are deduplicated and repairs precede improvements', () => {
  const items = suggestions({official_status: 'unavailable', system: {
    feature_coverage: {entirely_missing: ['minutes', 'minutes', 'goals']},
  }, runtime: {server_errors: 3}}, now);
  assert.deepEqual(items.map(i => i.id), ['official', 'runtime', 'features']);
  assert.match(items[2].detail, /^2 features/);
  assert.match(items[1].detail, /since this process started/);
});

test('missing telemetry and disabled optional enrichment invent no failures', () => {
  assert.deepEqual(suggestions({}, now), []);
  assert.deepEqual(suggestions({system: {enrichment_enabled: false}, runtime: {server_errors: 0}}, now), []);
});

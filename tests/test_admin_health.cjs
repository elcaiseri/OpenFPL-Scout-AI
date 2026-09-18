const {test} = require('node:test');
const assert = require('node:assert/strict');
const {suggestions, freshness} = require('../static/admin/health.js');

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

test('freshness uses source timestamps, not the newly assembled report time', () => {
  const rows = freshness({generated_at_utc: new Date(now).toISOString(), selected: {
    captured_at_utc: '2026-09-17T12:00:00Z', actuals_at_utc: '2026-09-18T11:00:00Z', result_state: 'final',
    metadata: {captured_at_utc: '2026-09-17T12:00:00Z', enrichment: {status: 'applied', source_observed_gameweek: 4, required_history_gameweek: 4}},
  }}, now);
  assert.equal(rows[0].age_seconds, 86400);
  assert.equal(rows[1].age_seconds, 3600);
  assert.equal(rows[1].status, 'final');
  assert.match(rows[1].detail, /does not make them stale/);
  assert.equal(rows[2].age_seconds, 86400);
  assert.match(rows[2].detail, /source update time is not recorded/);
  assert.match(rows[2].detail, /through GW4/);
});

test('missing and invalid source timestamps stay unknown', () => {
  for (const row of freshness({selected: {captured_at_utc: 'bad date'}}, now)) {
    assert.equal(row.timestamp, null);
    assert.equal(row.age_seconds, null);
  }
});

test('retrospective estimates use the replay time and retain their label', () => {
  const rows = freshness({selected: {captured_at_utc: '2026-09-01T12:00:00Z',
    replay: {generated_at_utc: '2026-09-18T11:00:00Z'}, forecast_state: 'retrospective-model',
  }}, now);
  assert.equal(rows[0].label, 'Retrospective estimate');
  assert.equal(rows[0].age_seconds, 3600);
  assert.equal(rows[0].status, 'retrospective-model');
});

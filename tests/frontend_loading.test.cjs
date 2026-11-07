const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/script.js'), 'utf8');
const events = [{ id: 1, is_current: true, deadline_time: '2026-11-07T10:00:00Z' }];
const squad = {
    gameweek: 1,
    frozen: true,
    scout_team: [{ id: 1, web_name: 'Player', team_name: 'Arsenal', element_type: 'Midfielder', expected_points: 6 }]
};
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function loadApp(fetch) {
    const context = vm.createContext({
        fetch, AbortController, TypeError, setTimeout, clearTimeout,
        console: { warn() {}, error() {} },
        document: { addEventListener() {} },
        window: {}
    });
    vm.runInContext(source, context);
    const app = context.window.FPLScoutApp;
    app.CONFIG.retryDelayMs = 1;
    app.appState.events = events;
    app.appState.eventsCache = events;
    app.appState.eventsTimestamp = Date.now();
    return app;
}

test('a gateway error and a network failure recover without refreshing', async () => {
    let requests = 0;
    const app = loadApp(async () => {
        requests++;
        if (requests === 1) return new Response('Bad gateway', { status: 502 });
        if (requests === 2) throw new TypeError('Failed to fetch');
        return response(squad);
    });

    const result = await app.utils.fetchJson('/api/scout');

    assert.equal(requests, 3);
    assert.equal(result.scout_team[0].expected_points, 6);
});

test('validation errors are shown without retrying', async () => {
    let requests = 0;
    const app = loadApp(async () => {
        requests++;
        return response({ detail: 'No eligible squad' }, 422);
    });

    await assert.rejects(app.utils.fetchJson('/api/scout'), /No eligible squad/);
    assert.equal(requests, 1);
});

test('temporary failures stop after two retries', async () => {
    let requests = 0;
    const app = loadApp(async () => {
        requests++;
        return response({ detail: 'FPL temporarily unavailable' }, 503);
    });

    await assert.rejects(app.utils.fetchJson('/api/scout'), /FPL temporarily unavailable/);
    assert.equal(requests, 3);
});

test('a timed out read is aborted and retried', async () => {
    let requests = 0;
    const app = loadApp(async (_url, { signal }) => {
        requests++;
        if (requests > 1) return response({ results: events });
        return new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () => {
                const error = new Error('Request timed out');
                error.name = 'AbortError';
                reject(error);
            }, { once: true });
        });
    });

    const result = await app.utils.fetchJson('/api/fpl/gameweeks', { timeoutMs: 5 });

    assert.equal(requests, 2);
    assert.equal(result.results[0].id, 1);
});

test('a timeout while reading the response body also retries', async () => {
    let requests = 0;
    const app = loadApp(async (_url, { signal }) => {
        requests++;
        if (requests > 1) return response({ results: events });
        return {
            ok: true,
            status: 200,
            json: () => new Promise((_resolve, reject) => {
                signal.addEventListener('abort', () => {
                    const error = new Error('Body download timed out');
                    error.name = 'AbortError';
                    reject(error);
                }, { once: true });
            })
        };
    });

    const result = await app.utils.fetchJson('/api/fpl/gameweeks', { timeoutMs: 5 });

    assert.equal(requests, 2);
    assert.equal(result.results[0].id, 1);
});

test('the saved squad is published before slow player metadata or fixtures', { timeout: 2000 }, async () => {
    const players = deferred();
    const fixtures = deferred();
    const published = deferred();
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) return response(squad);
        if (url.startsWith('/api/fpl/players')) return players.promise;
        if (url.startsWith('/api/fpl/fixtures')) return fixtures.promise;
        throw new Error('Unexpected endpoint: ' + url);
    });
    const loading = app.dataLoader.loadDashboard(1, false, published.resolve);

    const initial = await published.promise;
    assert.equal(initial.scout_team[0].expected_points, 6);
    assert.equal(initial.fixturesLoading, true);

    players.resolve(response({ results: [{ id: 1, position: 3 }] }));
    fixtures.resolve(response({ results: [] }));
    const complete = await loading;
    assert.equal(complete.scout_team[0].position, 3);
    assert.equal(complete.fixturesAvailable, true);
    assert.equal(app.appState.dashboardCache.get(1), complete);
});

test('failed metadata and fixtures do not hide a squad or poison its cache', async () => {
    let available = false;
    const calls = new Map();
    const app = loadApp(async url => {
        calls.set(url, (calls.get(url) || 0) + 1);
        if (url.startsWith('/api/scout?')) return response(squad);
        if (available) return response({ results: [] });
        return response({ detail: 'Upstream unavailable' }, 503);
    });

    const partial = await app.dataLoader.loadDashboard(1);
    assert.equal(partial.scout_team[0].id, 1);
    assert.equal(partial.fixturesAvailable, false);
    assert.equal(app.appState.dashboardCache.has(1), false);
    assert.equal(app.appState.referenceCache, null);
    assert.equal(calls.get(app.CONFIG.endpoints.players), 3);

    available = true;
    const complete = await app.dataLoader.loadDashboard(1);
    assert.equal(complete.fixturesAvailable, true);
    assert.equal(app.appState.dashboardCache.get(1), complete);
    assert.equal(calls.get(app.CONFIG.endpoints.players), 4);
});

test('archive collection uses one independent POST while already in flight', async () => {
    const collection = deferred();
    let requests = 0;
    const app = loadApp(async (url, options) => {
        requests++;
        assert.equal(url, app.CONFIG.endpoints.syncResults);
        assert.equal(options.method, 'POST');
        return collection.promise;
    });

    const first = app.dataLoader.syncResults();
    assert.equal(app.dataLoader.syncResults(), first);
    assert.equal(requests, 1);
    collection.resolve(response({ status: 'throttled' }));
    await first;
    assert.equal(app.appState.resultsSyncPromise, null);
});

test('a failed archive POST is not retried and does not reject the UI load', async () => {
    let requests = 0;
    const app = loadApp(async () => {
        requests++;
        return response({ detail: 'Sync unavailable' }, 503);
    });

    await app.dataLoader.syncResults();

    assert.equal(requests, 1);
    assert.equal(app.appState.resultsSyncPromise, null);
});

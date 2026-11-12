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

function abortError() {
    const error = new Error('Request aborted');
    error.name = 'AbortError';
    return error;
}

// Resolves after `delayMs` unless the request is aborted first.
function slowResponse(signal, payload, delayMs) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve(response(payload)), delayMs);
        signal.addEventListener('abort', () => {
            clearTimeout(timer);
            reject(abortError());
        }, { once: true });
    });
}

async function until(predicate) {
    while (!predicate()) await new Promise(resolve => setImmediate(resolve));
}

// Just enough DOM for the dashboard renderer to run.
function fakeElement() {
    return {
        hidden: false, disabled: false, textContent: '', innerHTML: '', value: '', title: '',
        dataset: {},
        style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {} },
        setAttribute() {}, removeAttribute() {}, addEventListener() {}, focus() {},
        appendChild() {},
        querySelectorAll: () => []
    };
}

function fakeDocument() {
    const elements = new Map();
    return {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, fakeElement());
            return elements.get(id);
        },
        querySelectorAll: () => [],
        createElement: () => fakeElement(),
        createRange: () => ({ selectNodeContents() {}, getBoundingClientRect: () => ({ width: 0 }) }),
        addEventListener() {}
    };
}

function loadApp(fetch) {
    const document = fakeDocument();
    const context = vm.createContext({
        fetch, AbortController, TypeError, setTimeout, clearTimeout,
        // A real deadline countdown interval would keep the test process alive.
        setInterval: () => 0,
        clearInterval() {},
        requestAnimationFrame: () => 0,
        console: { warn() {}, error() {} },
        document,
        window: {}
    });
    vm.runInContext(source, context);
    const app = context.window.FPLScoutApp;
    app.document = document;
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

test('team rating waits for slow inference instead of timing out', async () => {
    let requests = 0;
    const app = loadApp(async (_url, { signal }) => {
        requests++;
        return slowResponse(signal, { rating: 70, gameweek: 1 }, 20);
    });
    app.CONFIG.requestTimeoutMs = 5;

    // The same delay times out an ordinary read, so the timeout is active.
    await assert.rejects(app.utils.fetchJson('/api/fpl/gameweeks'), /timed out/);
    assert.equal(requests, 3);

    requests = 0;
    const rating = await app.dataLoader.loadTeamRating(1234567, 1);
    assert.equal(rating.rating, 70);
    assert.equal(requests, 1);
});

test('inference reads are not rerun after a server error or timeout', async () => {
    for (const status of [500, 504]) {
        let scoutRequests = 0;
        let ratingRequests = 0;
        const app = loadApp(async url => {
            if (url.startsWith('/api/scout?')) {
                scoutRequests++;
                return response({ detail: `Scout failed (${status})` }, status);
            }
            if (url.startsWith('/api/scout/team-rating')) {
                ratingRequests++;
                return response({ detail: `Rating failed (${status})` }, status);
            }
            return response({ results: [] });
        });

        await assert.rejects(app.dataLoader.loadDashboard(1), new RegExp(`Scout failed \\(${status}\\)`));
        await assert.rejects(app.dataLoader.loadTeamRating(1234567, 1), new RegExp(`Rating failed \\(${status}\\)`));
        assert.equal(scoutRequests, 1);
        assert.equal(ratingRequests, 1);
    }
});

test('a squad read still recovers from a gateway failure', async () => {
    let requests = 0;
    const app = loadApp(async url => {
        if (!url.startsWith('/api/scout?')) return response({ results: [] });
        requests++;
        return requests === 1 ? response({ detail: 'Bad gateway' }, 502) : response(squad);
    });

    const dashboard = await app.dataLoader.loadDashboard(1);

    assert.equal(requests, 2);
    assert.equal(dashboard.scout_team[0].id, 1);
});

test('gameweek controls unlock once the squad shows, and a slower load cannot overwrite a newer one', { timeout: 2000 }, async () => {
    const firstPlayers = deferred();
    const firstFixtures = deferred();
    let playerRequests = 0;
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) {
            const gameweek = Number(new URL(url, 'http://localhost').searchParams.get('gameweek'));
            return response({ ...squad, gameweek });
        }
        if (url === app.CONFIG.endpoints.syncResults) return response({ status: 'throttled' });
        if (url.startsWith('/api/fpl/players')) {
            playerRequests++;
            return playerRequests === 1 ? firstPlayers.promise : response({ results: [] });
        }
        if (url === app.CONFIG.endpoints.fixtures(1)) return firstFixtures.promise;
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    });
    const select = app.document.getElementById('gameweek-select');

    const first = app.gameweekManager.load(1);
    assert.equal(select.disabled, true);

    // GW1's squad is shown while its player metadata and fixtures still load.
    await until(() => app.appState.currentData);
    assert.equal(app.appState.currentData.fixturesLoading, true);
    assert.equal(app.appState.isLoading, false);
    assert.equal(select.disabled, false);

    await app.gameweekManager.load(2);
    assert.equal(app.appState.currentGameweek, 2);

    firstPlayers.resolve(response({ results: [] }));
    firstFixtures.resolve(response({ results: [] }));
    await first;

    assert.equal(app.appState.currentGameweek, 2);
    assert.equal(app.appState.currentData.gameweek, 2);
    assert.equal(app.appState.isLoading, false);
    assert.equal(select.disabled, false);
    assert.equal(app.appState.dashboardCache.get(1).gameweek, 1);
});

test('a failed Gameweek stays selected, so Try again reloads it instead of the previous squad', async () => {
    const scoutRequests = [];
    let secondAvailable = false;
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) {
            const gameweek = Number(new URL(url, 'http://localhost').searchParams.get('gameweek'));
            scoutRequests.push(gameweek);
            if (gameweek === 2 && !secondAvailable) return response({ detail: 'No eligible squad' }, 422);
            return response({ ...squad, gameweek });
        }
        if (url === app.CONFIG.endpoints.syncResults) return response({ status: 'throttled' });
        return response({ results: [] });
    });

    await app.gameweekManager.load(1);
    await app.gameweekManager.load(2);

    assert.equal(app.appState.currentGameweek, 2);
    assert.equal(app.appState.currentData, null);
    assert.equal(app.document.getElementById('gameweek-select').value, '2');
    assert.equal(app.document.getElementById('gameweek-info').textContent, 'Gameweek 2');

    secondAvailable = true;
    await app.interactions.retry();

    assert.deepEqual([...scoutRequests], [1, 2, 2]);
    assert.equal(app.appState.currentData.gameweek, 2);
});

test('club codes stay distinct before fixtures teach the official ones', () => {
    const app = loadApp(async () => response({}));

    assert.equal(app.utils.teamCode('Manchester City'), 'MCI');
    assert.equal(app.utils.teamCode('Manchester United'), 'MUN');
    assert.equal(app.utils.opponentCode('Man Utd (H) / Man City (A)'), 'MUN (H) / MCI (A)');
    // A club the table does not know still gets a readable code.
    assert.equal(app.utils.teamCode('Coventry'), 'COV');

    app.dataLoader.learnTeamCodes(
        [{ home_team: { id: 1, name: 'Man City', short_name: 'MC1' }, away_team: null }],
        [{ team_id: 1, team_name: 'Manchester City' }]
    );
    assert.equal(app.utils.teamCode('Manchester City'), 'MC1');
});

test('the app opens on the next deadline, as the API default does', () => {
    const app = loadApp(async () => response({}));
    const opening = list => app.gameweekManager.openingGameweek(list).id;

    // International break: GW10 is current and finished, GW11's deadline is next.
    assert.equal(opening([
        { id: 10, is_current: true, finished: true },
        { id: 11, is_next: true },
        { id: 12 }
    ]), 11);
    // Matches still in play: the next deadline is the one a manager can act on.
    assert.equal(opening([
        { id: 10, is_current: true, finished: false },
        { id: 11, is_next: true }
    ]), 11);
    // Pre-season and after the final deadline.
    assert.equal(opening([{ id: 1, is_next: true }, { id: 2 }]), 1);
    assert.equal(opening([{ id: 37, finished: true }, { id: 38, is_current: true, finished: true }]), 38);
    // No flags at all: the first unfinished Gameweek, then the last one.
    assert.equal(opening([{ id: 4, finished: true }, { id: 5 }]), 5);
    assert.equal(opening([{ id: 4, finished: true }, { id: 5, finished: true }]), 5);
});

test('start-up requests the next deadline squad and keeps the finished Gameweek one click back', async () => {
    const season = [
        { id: 9, finished: true, deadline_time: '2026-10-31T11:00:00Z' },
        { id: 10, is_current: true, finished: true, deadline_time: '2026-11-06T18:30:00Z' },
        { id: 11, is_next: true, deadline_time: '2026-11-21T11:00:00Z' },
        { id: 12, deadline_time: '2026-11-27T18:30:00Z' },
        { id: 13, deadline_time: '2026-12-02T19:30:00Z' }
    ];
    const scoutRequests = [];
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) {
            const gameweek = Number(new URL(url, 'http://localhost').searchParams.get('gameweek'));
            scoutRequests.push(gameweek);
            return response({ ...squad, gameweek, frozen: gameweek < 11 });
        }
        if (url === app.CONFIG.endpoints.syncResults) return response({ status: 'throttled' });
        return response({ results: [] });
    });
    app.appState.events = season;
    app.appState.eventsCache = season;

    await app.app.start();

    assert.deepEqual(scoutRequests, [11]);
    assert.equal(app.appState.currentGameweek, 11);
    assert.equal(app.appState.currentData.gameweek, 11);
    assert.deepEqual(app.appState.visibleEvents.map(event => event.id), [10, 11, 12]);
});

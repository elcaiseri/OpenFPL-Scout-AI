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

function loadApp(fetch, globals = {}) {
    const document = fakeDocument();
    const context = vm.createContext({
        fetch, AbortController, TypeError, setTimeout, clearTimeout,
        // A real deadline countdown interval would keep the test process alive.
        setInterval: () => 0,
        clearInterval() {},
        requestAnimationFrame: () => 0,
        console: { warn() {}, error() {} },
        document,
        window: {},
        ...globals
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

test('the saved squad is published before slow fixtures', { timeout: 2000 }, async () => {
    const fixtures = deferred();
    const published = deferred();
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) return response(squad);
        if (url.startsWith('/api/fpl/fixtures')) return fixtures.promise;
        throw new Error('Unexpected endpoint: ' + url);
    });
    const loading = app.dataLoader.loadDashboard(1, false, published.resolve);

    const initial = await published.promise;
    assert.equal(initial.scout_team[0].expected_points, 6);
    assert.equal(initial.fixturesLoading, true);

    fixtures.resolve(response({ results: [] }));
    const complete = await loading;
    assert.equal(complete.scout_team[0].position, 'Midfielder');
    assert.equal(complete.fixturesAvailable, true);
    assert.equal(app.appState.dashboardCache.get(1), complete);
});

test('squad players keep the official details the squad arrives with, without a player list', async () => {
    const requested = [];
    const app = loadApp(async url => {
        requested.push(url);
        if (url.startsWith('/api/scout?')) {
            return response({
                ...squad,
                scout_team: [{
                    ...squad.scout_team[0], team_id: 1, position: 'Midfielder',
                    price: 7.5, selected_by_percent: 12.3, status: 'd', news: 'Knock'
                }]
            });
        }
        if (url.startsWith('/api/fpl/fixtures')) {
            return response({ results: [{
                id: 9, kickoff_time: '2026-11-21T15:00:00Z',
                home_team: { id: 1, name: 'Arsenal', short_name: 'ARS', difficulty: 3 },
                away_team: { id: 2, name: 'Chelsea', short_name: 'CHE', difficulty: 4 }
            }] });
        }
        throw new Error('Unexpected endpoint: ' + url);
    });

    const dashboard = await app.dataLoader.loadDashboard(1);

    const [player] = dashboard.scout_team;
    assert.equal(player.price, 7.5);
    assert.equal(player.status, 'd');
    assert.equal(player.news, 'Knock');
    assert.equal(player.fixture_id, 9);
    assert.equal(player.difficulty, 3);
    assert.equal(requested.some(url => url.startsWith('/api/fpl/players')), false);
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

    available = true;
    const complete = await app.dataLoader.loadDashboard(1);
    assert.equal(complete.fixturesAvailable, true);
    assert.equal(app.appState.dashboardCache.get(1), complete);
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
    const firstFixtures = deferred();
    const app = loadApp(async url => {
        if (url.startsWith('/api/scout?')) {
            const gameweek = Number(new URL(url, 'http://localhost').searchParams.get('gameweek'));
            return response({ ...squad, gameweek });
        }
        if (url === app.CONFIG.endpoints.syncResults) return response({ status: 'throttled' });
        if (url === app.CONFIG.endpoints.fixtures(1)) return firstFixtures.promise;
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    });
    const select = app.document.getElementById('gameweek-select');

    const first = app.gameweekManager.load(1);
    assert.equal(select.disabled, true);

    // GW1's squad is shown while its fixtures still load.
    await until(() => app.appState.currentData);
    assert.equal(app.appState.currentData.fixturesLoading, true);
    assert.equal(app.appState.isLoading, false);
    assert.equal(select.disabled, false);

    await app.gameweekManager.load(2);
    assert.equal(app.appState.currentGameweek, 2);

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

test('the app opens on a live Gameweek, and on the next deadline once it finishes', () => {
    const app = loadApp(async () => response({}));
    const opening = list => app.gameweekManager.openingGameweek(list).id;

    // Matches still in play: the live Gameweek.
    assert.equal(opening([
        { id: 10, is_current: true, finished: false },
        { id: 11, is_next: true }
    ]), 10);
    // International break: GW10 is current and finished, GW11's deadline is next.
    assert.equal(opening([
        { id: 10, is_current: true, finished: true },
        { id: 11, is_next: true },
        { id: 12 }
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

test('the hero buttons open the squad section in their own mode', () => {
    const app = loadApp(async () => response({}));
    const panel = app.document.getElementById('team-rating-panel');
    assert.equal(app.appState.squadMode, 'scout');

    app.interactions.openSquadMode('rating');
    assert.equal(app.appState.squadMode, 'rating');
    assert.equal(panel.hidden, false);

    app.interactions.openSquadMode('scout');
    assert.equal(app.appState.squadMode, 'scout');
    assert.equal(panel.hidden, true);
});

function memoryStorage() {
    const items = new Map();
    return {
        items,
        getItem: key => (items.has(key) ? items.get(key) : null),
        setItem: (key, value) => { items.set(key, String(value)); }
    };
}

test('rated team IDs are suggested newest first, once each, five at most', () => {
    const localStorage = memoryStorage();
    const app = loadApp(async () => response({}), { localStorage });
    const suggestions = app.document.getElementById('team-id-suggestions');

    app.recentTeams.remember({ entry_id: 1, team_name: 'Team <One>' });
    app.recentTeams.remember({ entry_id: 2, team_name: 'Two' });
    app.recentTeams.remember({ entry_id: 1, team_name: 'Team <One>' });
    assert.deepEqual(Array.from(app.recentTeams.read(), item => item.id), [1, 2]);
    // Team names are FPL users' own text, so the label is escaped.
    assert.equal(
        suggestions.innerHTML,
        '<option value="1" label="Team &lt;One&gt;"></option><option value="2" label="Two"></option>'
    );

    for (let id = 3; id <= 8; id++) app.recentTeams.remember({ entry_id: id, team_name: `T${id}` });
    assert.deepEqual(Array.from(app.recentTeams.read(), item => item.id), [8, 7, 6, 5, 4]);

    app.recentTeams.remember({ entry_id: 'abc' });
    app.recentTeams.remember({ entry_id: 0 });
    assert.equal(app.recentTeams.read()[0].id, 8);

    // A later visit reads the same list back.
    const later = loadApp(async () => response({}), { localStorage });
    later.recentTeams.render();
    assert.match(later.document.getElementById('team-id-suggestions').innerHTML, /^<option value="8" label="T8">/);
});

test('suggestions survive blocked or missing storage for the current visit', () => {
    const blocked = {
        getItem() { throw new Error('SecurityError'); },
        setItem() { throw new Error('QuotaExceededError'); }
    };
    for (const globals of [{ localStorage: blocked }, {}]) {
        const app = loadApp(async () => response({}), globals);
        assert.equal(app.recentTeams.read().length, 0);
        app.recentTeams.remember({ entry_id: 4069384, team_name: 'Mine' });
        assert.equal(
            app.document.getElementById('team-id-suggestions').innerHTML,
            '<option value="4069384" label="Mine"></option>'
        );
    }
});

test('only a successful rating is remembered', async () => {
    const localStorage = memoryStorage();
    const app = loadApp(async url => {
        const entryId = Number(new URL(url, 'http://localhost').searchParams.get('entry_id'));
        if (entryId === 999) return response({ detail: 'This FPL team ID was not found.' }, 404);
        // A rating that comes back without players also counts as a failure.
        if (entryId === 998) return response({ entry_id: entryId, team_name: 'Empty', gameweek: 1, squad: [] });
        return response({
            entry_id: entryId, team_name: 'Mine', gameweek: 1, picks_gameweek: 1,
            squad: squad.scout_team.map(player => ({ ...player, pick_position: 1 }))
        });
    }, { localStorage });

    await app.teamRating.rate(999);
    await app.teamRating.rate(998);
    assert.equal(app.recentTeams.read().length, 0);

    await app.teamRating.rate(4069384);
    assert.deepEqual(Array.from(app.recentTeams.read(), item => item.id), [4069384]);
});

function savedSquadEntry(overrides = {}) {
    return JSON.stringify({
        schema: 1,
        savedAt: Date.now(),
        data: {
            ...squad,
            scout_team: [{ ...squad.scout_team[0], web_name: 'Saved' }],
            event: events[0],
            fixtures: [],
            fixturesAvailable: true,
            syncedAt: new Date().toISOString()
        },
        ...overrides
    });
}

function startUpFetch(scoutResponse) {
    return async url => {
        if (url.startsWith('/api/scout?')) return scoutResponse.promise;
        if (url === '/api/scout/sync-results') return response({ status: 'throttled' });
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    };
}

test('a return visit shows the saved squad at once, then the current one, and saves it', async () => {
    const localStorage = memoryStorage();
    localStorage.setItem('openfpl.lastSquad', savedSquadEntry());
    const scoutResponse = deferred();
    const app = loadApp(startUpFetch(scoutResponse), { localStorage });
    const pitch = app.document.getElementById('pitch');

    const starting = app.app.start();
    assert.equal(app.appState.currentData.scout_team[0].web_name, 'Saved');
    assert.equal(app.document.getElementById('signal-state').textContent, 'Refreshing');

    // The saved squad stays on the pitch while the server predicts.
    await until(() => app.appState.isLoading);
    assert.match(pitch.innerHTML, /Saved/);

    scoutResponse.resolve(response(squad));
    await starting;
    assert.equal(app.appState.currentData.scout_team[0].web_name, 'Player');
    assert.match(pitch.innerHTML, /Player/);
    const saved = JSON.parse(localStorage.getItem('openfpl.lastSquad'));
    assert.equal(saved.data.scout_team[0].web_name, 'Player');
    assert.equal(saved.data.events, undefined);
});

test('an expired or older-format saved squad is not shown', async () => {
    for (const entry of [
        savedSquadEntry({ savedAt: Date.now() - 13 * 60 * 60 * 1000 }),
        savedSquadEntry({ schema: 0 }),
        '{not json'
    ]) {
        const localStorage = memoryStorage();
        localStorage.setItem('openfpl.lastSquad', entry);
        const scoutResponse = deferred();
        const app = loadApp(startUpFetch(scoutResponse), { localStorage });

        const starting = app.app.start();
        assert.equal(app.appState.currentData, null);
        await until(() => app.appState.isLoading);
        assert.match(app.document.getElementById('pitch').innerHTML, /class="loading[ "]/);

        scoutResponse.resolve(response(squad));
        await starting;
        assert.equal(app.appState.currentData.scout_team[0].web_name, 'Player');
    }
});

test('start-up works when storage is blocked', async () => {
    const blocked = {
        getItem() { throw new Error('SecurityError'); },
        setItem() { throw new Error('QuotaExceededError'); }
    };
    const scoutResponse = deferred();
    const app = loadApp(startUpFetch(scoutResponse), { localStorage: blocked });

    const starting = app.app.start();
    scoutResponse.resolve(response(squad));
    await starting;

    assert.equal(app.appState.currentData.scout_team[0].web_name, 'Player');
});

test('a stale squad asks for one refresh in its own request and is not kept', async () => {
    const refresh = deferred();
    const requests = [];
    const app = loadApp(async (url, options) => {
        requests.push([options.method || 'GET', url]);
        if (url.startsWith('/api/scout?')) return response({ ...squad, frozen: false, refresh_due: true });
        if (url === '/api/scout/refresh?gameweek=1') return refresh.promise;
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    });

    await app.gameweekManager.load(1);
    // Loading the same Gameweek again does not send a second refresh.
    await app.gameweekManager.load(1);

    const refreshes = requests.filter(([, url]) => url.startsWith('/api/scout/refresh'));
    assert.deepEqual(refreshes, [['POST', '/api/scout/refresh?gameweek=1']]);
    assert.equal(app.appState.dashboardCache.has(1), false);
    assert.equal(app.appState.currentData.scout_team[0].web_name, 'Player');

    refresh.resolve(response({ status: 'refreshed' }));
    await app.appState.refreshPromises.get(1);
    assert.equal(app.appState.refreshPromises.size, 0);
});

test('a fresh squad sends no refresh, and a failed refresh does not disturb the page', async () => {
    for (const refreshDue of [false, true]) {
        const requests = [];
        const app = loadApp(async (url, options) => {
            requests.push(url);
            if (url.startsWith('/api/scout?')) return response({ ...squad, frozen: false, refresh_due: refreshDue });
            if (url.startsWith('/api/scout/refresh')) return response({ detail: 'FPL is updating' }, 502);
            if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
            throw new Error('Unexpected endpoint: ' + url);
        });

        await app.gameweekManager.load(1);
        await Promise.all(app.appState.refreshPromises.values());

        assert.equal(requests.filter(url => url.startsWith('/api/scout/refresh')).length, refreshDue ? 1 : 0);
        assert.equal(app.appState.currentData.scout_team[0].web_name, 'Player');
        assert.equal(app.appState.isLoading, false);
    }
});

// A browser address bar the app reads and rewrites.
function addressBar(href) {
    const location = new URL(href);
    const history = {
        replaceState(_state, _title, url) { location.href = String(url); }
    };
    return { location, history };
}

const planner = [
    { id: 1, is_current: true, finished: true, deadline_time: '2026-11-07T10:00:00Z' },
    { id: 2, is_next: true, deadline_time: '2026-11-21T10:00:00Z' },
    { id: 3, deadline_time: '2026-11-28T10:00:00Z' }
];

function ratingResponse(entryId, gameweek, extra = {}) {
    return response({
        entry_id: entryId, team_name: 'Mine', gameweek, picks_gameweek: gameweek, rating: 81,
        squad: squad.scout_team.map(player => ({ ...player, pick_position: 1 })),
        ...extra
    });
}

function plannerApp(href, requests = [], globals = {}) {
    const { location, history } = addressBar(href);
    const app = loadApp(async url => {
        requests.push(url);
        const params = new URL(url, 'http://localhost').searchParams;
        if (url.startsWith('/api/scout/team-rating')) {
            return ratingResponse(Number(params.get('entry_id')), Number(params.get('gameweek')));
        }
        if (url.startsWith('/api/scout?')) return response({ ...squad, gameweek: Number(params.get('gameweek')) });
        if (url === '/api/scout/sync-results') return response({ status: 'throttled' });
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    }, { window: { location, history }, URL, URLSearchParams, ...globals });
    app.appState.events = planner;
    app.appState.eventsCache = planner;
    return { app, location };
}

test('a shared link opens its Gameweek and rates its team without raising the keyboard', async () => {
    const requests = [];
    const { app, location } = plannerApp('https://openfpl.kassem.dev/?gw=3&team=4069384', requests);
    let focused = false;
    app.document.getElementById('team-id-input').focus = () => { focused = true; };

    await app.app.start();

    assert.equal(app.appState.openingGameweek, 2);
    assert.equal(app.appState.currentGameweek, 3);
    assert.ok(requests.includes('/api/scout?gameweek=3'));
    assert.ok(requests.includes('/api/scout/team-rating?entry_id=4069384&gameweek=3'));
    assert.equal(app.appState.ratingData.entry_id, 4069384);
    assert.equal(app.document.getElementById('team-id-input').value, '4069384');
    assert.equal(focused, false);
    assert.equal(location.search, '?gw=3&team=4069384');
});

test('the address bar follows the Gameweek and rating, and leaves out the opening Gameweek', async () => {
    const { app, location } = plannerApp('https://openfpl.kassem.dev/');

    await app.app.start();
    assert.equal(location.search, '');

    await app.gameweekManager.load(3);
    assert.equal(location.search, '?gw=3');

    app.teamRating.setMode('rating');
    await app.teamRating.rate(123);
    assert.equal(location.search, '?gw=3&team=123');

    app.teamRating.setMode('scout');
    assert.equal(location.search, '?gw=3');

    await app.gameweekManager.load(2);
    assert.equal(location.search, '');
});

test('a shared Gameweek the planner no longer offers, or a malformed one, opens the current one', async () => {
    for (const query of ['?gw=30', '?gw=abc&team=-4', '?gw=0']) {
        const requests = [];
        const { app } = plannerApp(`https://openfpl.kassem.dev/${query}`, requests);

        await app.app.start();

        assert.equal(app.appState.currentGameweek, 2, query);
        assert.equal(requests.some(url => url.includes('team-rating')), false, query);
    }
});

test('suggested transfers open from their button, are listed safely, and the button closes with transfers', () => {
    const app = loadApp(async () => response({}));
    const panel = app.document.getElementById('rating-transfers');
    const list = app.document.getElementById('rating-transfer-list');
    const toggle = app.document.getElementById('transfers-toggle');
    const note = app.document.getElementById('transfers-toggle-note');
    const transfer = {
        out: { web_name: '<img src=x>', team_name: 'Arsenal', price: 5.9, expected_points: 2.1 },
        in: { web_name: 'Pickford', team_name: 'Everton', price: 5.4, expected_points: 4.2 },
        gain: 2.1349
    };
    const show = data => {
        app.appState.ratingData = data;
        app.teamRating.transfers(data);
    };

    // Open: the button works, and the suggestions wait for it.
    show({ gameweek: 11, bank: 0.5, transfers_open: true, transfers: [transfer] });
    assert.equal(toggle.disabled, false);
    assert.equal(toggle.textContent, 'Show suggested transfers');
    assert.equal(note.textContent, 'Available until the Gameweek 11 deadline.');
    assert.equal(panel.hidden, true);

    app.teamRating.toggleTransfers();
    assert.equal(panel.hidden, false);
    assert.equal(toggle.textContent, 'Hide suggested transfers');
    assert.match(list.innerHTML, /&lt;img src=x&gt;/);
    assert.match(list.innerHTML, /ARS · £5\.9m/);
    assert.match(list.innerHTML, /\+2\.13 xPts/);
    assert.equal(app.document.getElementById('rating-bank').textContent, '£0.5m in the bank at the last deadline');

    app.teamRating.toggleTransfers();
    assert.equal(panel.hidden, true);
    app.teamRating.toggleTransfers();
    show({ gameweek: 11, bank: 0.5, transfers_open: true, transfers: [] });
    assert.match(list.innerHTML, /No affordable single transfer/);

    // Closed: the button cannot be used, says why, and the suggestions go.
    for (const closed of [
        { gameweek: 11, bank: 0.5, transfers_open: false, frozen: false, transfers: [transfer] },
        { gameweek: 11, bank: 0.5, transfers_open: false, frozen: true, transfers: [transfer] }
    ]) {
        show(closed);
        assert.equal(toggle.disabled, true);
        assert.equal(toggle.textContent, 'Suggested transfers closed');
        assert.equal(note.textContent, 'Transfers are closed: the Gameweek 11 deadline has passed.');
        assert.equal(panel.hidden, true);
        app.teamRating.toggleTransfers();
        assert.equal(panel.hidden, true);
    }

    show({ gameweek: 11, bank: null, transfers_open: true, transfers: [transfer] });
    assert.equal(toggle.disabled, true);
    assert.match(note.textContent, /has not published this team's bank/);
    assert.equal(panel.hidden, true);
});

test('a new rating starts with its suggestions closed, and showing the same one again keeps them open', () => {
    const app = loadApp(async () => response({}));
    const panel = app.document.getElementById('rating-transfers');
    const rating = entryId => ({
        entry_id: entryId, team_name: 'Mine', gameweek: 11, picks_gameweek: 10, rating: 80,
        squad: squad.scout_team.map(player => ({ ...player, pick_position: 1 })),
        bank: 0.5, transfers_open: true, transfers: []
    });
    const first = rating(1);

    app.teamRating.render(first);
    app.teamRating.toggleTransfers();
    assert.equal(panel.hidden, false);

    app.teamRating.render(first);
    assert.equal(panel.hidden, false);

    app.teamRating.render(rating(2));
    assert.equal(panel.hidden, true);
});

test('a squad over the club limit is told why only those sales are suggested', () => {
    const app = loadApp(async () => response({}));
    const note = app.document.getElementById('rating-club-limit');
    const list = app.document.getElementById('rating-transfer-list');

    const overLimit = { bank: 0.5, transfers_open: true, transfers: [], clubs_over_limit: { Arsenal: 4 } };
    app.appState.ratingData = overLimit;
    app.teamRating.toggleTransfers();
    assert.equal(note.hidden, false);
    assert.equal(note.textContent, 'Your squad has 4 Arsenal players. '
        + 'FPL requires your next transfer to sell one of them, so only those sales are suggested.');
    assert.match(list.innerHTML, /No affordable sale of one of those players/);

    app.teamRating.transfers({ bank: 0.5, transfers_open: true, transfers: [], clubs_over_limit: {} });
    assert.equal(note.hidden, true);
});

test('a rating shown again after the AI squad keeps its team in the address bar and in the shared link', async () => {
    let shared = null;
    const navigator = { clipboard: { writeText: async url => { shared = url; } } };
    const { app, location } = plannerApp('https://openfpl.kassem.dev/', [], { navigator });
    // Keep the Share button's click handler, which the fake DOM would drop.
    const shareButton = app.document.getElementById('share-rating');
    shareButton.addEventListener = (type, handler) => { shareButton[`on${type}`] = handler; };
    app.interactions.init();
    await app.app.start();
    await app.gameweekManager.load(3);

    app.teamRating.setMode('rating');
    await app.teamRating.rate(123);
    app.teamRating.setMode('scout');
    assert.equal(location.search, '?gw=3');
    app.teamRating.setMode('rating');
    assert.equal(location.search, '?gw=3&team=123');

    // The Share button links the rating on screen, whatever the address bar says.
    location.href = 'https://openfpl.kassem.dev/';
    shareButton.onclick();
    await until(() => shared);
    assert.equal(shared, 'https://openfpl.kassem.dev/?team=123&gw=3');
});

function listPlayers() {
    return [
        { id: 1, web_name: 'Bowen', element_type: 'Midfielder', price: 7.5, selected_by_percent: 20, expected_points: 5 },
        { id: 2, web_name: 'Areola', element_type: 'Goalkeeper', price: null, selected_by_percent: 3, expected_points: 3 },
        { id: 3, web_name: 'Cash', element_type: 'Defender', price: 4.5, selected_by_percent: 9, expected_points: 6 }
    ];
}

const rowNames = html => Array.from(html.matchAll(/<strong>(\w+)/g), match => match[1]);

test('the list sorts by column, keeps each xPts rank, and puts missing values last', () => {
    const app = loadApp(async () => response({}));
    const body = app.document.getElementById('squad-table-body');
    app.dashboardRenderer.squad(listPlayers());

    assert.deepEqual(rowNames(body.innerHTML), ['Cash', 'Bowen', 'Areola']);

    app.tableSorting.sortBy('price');
    assert.deepEqual(rowNames(body.innerHTML), ['Bowen', 'Cash', 'Areola']);
    app.tableSorting.sortBy('price');
    assert.deepEqual(rowNames(body.innerHTML), ['Cash', 'Bowen', 'Areola']);
    assert.equal(app.appState.tableSort.direction, 'asc');

    app.tableSorting.sortBy('player');
    assert.deepEqual(rowNames(body.innerHTML), ['Areola', 'Bowen', 'Cash']);
    // Bowen is still ranked second by xPts.
    assert.match(body.innerHTML, /table-rank">02<\/span>\s*<span><strong>Bowen/);

    app.tableSorting.sortBy('position');
    assert.deepEqual(rowNames(body.innerHTML), ['Areola', 'Cash', 'Bowen']);

    // A new squad keeps the chosen order.
    app.dashboardRenderer.squad(listPlayers().reverse());
    assert.deepEqual(rowNames(body.innerHTML), ['Areola', 'Cash', 'Bowen']);
});

test('the scout squad shows its starting XI on the pitch and its bench below', () => {
    const app = loadApp(async () => response({}));
    const positions = ['Goalkeeper', 'Defender', 'Defender', 'Defender', 'Defender',
        'Midfielder', 'Midfielder', 'Midfielder', 'Midfielder', 'Forward', 'Forward',
        'Goalkeeper', 'Defender', 'Midfielder', 'Forward'];
    const players = positions.map((element_type, index) => ({
        id: index + 1, web_name: `P${index + 1}`, element_type,
        expected_points: 15 - index, pick_position: index + 1,
        role: index === 0 ? 'captain' : index === 1 ? 'vice' : ''
    }));
    app.dashboardRenderer.squad(players);

    const pitch = app.document.getElementById('pitch').innerHTML;
    const [field, bench] = pitch.split('class="pitch-bench"');
    assert.equal(field.match(/class="player-card/g).length, 11);
    assert.deepEqual(Array.from(bench.matchAll(/aria-label="(P\d+)/g), match => match[1]),
        ['P12', 'P13', 'P14', 'P15']);
    assert.equal(app.document.getElementById('formation-chip').textContent, '4-4-2');
    assert.match(app.document.getElementById('squad-subtitle').textContent, /Starting XI and 4 on the bench/);
    assert.match(app.document.getElementById('squad-table-body').innerHTML, /P12[\s\S]*?SUB/);
});

test('cards carry a short key, and the briefing opens the player it names', () => {
    const app = loadApp(async () => response({}));
    app.dashboardRenderer.squad(listPlayers());

    const pitch = app.document.getElementById('pitch').innerHTML;
    assert.doesNotMatch(pitch, /data-player="\{/);
    const key = pitch.match(/data-player="(\d+)"[^>]*aria-label="Cash/)[1];

    app.interactions.showPlayer({ dataset: { player: key } });
    assert.equal(app.document.getElementById('dialog-player-name').textContent, 'Cash');

    // A key from an earlier squad no longer opens anything.
    app.dashboardRenderer.squad(listPlayers().slice(0, 1));
    app.document.getElementById('dialog-player-name').textContent = '';
    app.interactions.showPlayer({ dataset: { player: '3' } });
    assert.equal(app.document.getElementById('dialog-player-name').textContent, '');
});

test('sorting the list never adds player entries beyond the squad on screen', () => {
    const app = loadApp(async () => response({}));
    app.dashboardRenderer.squad(listPlayers());
    assert.equal(app.appState.shownPlayers.size, 3);

    for (let sort = 0; sort < 100; sort++) app.tableSorting.sortBy(sort % 2 ? 'price' : 'player');

    assert.equal(app.appState.shownPlayers.size, 3);
    assert.deepEqual(Array.from(app.appState.shownPlayers.keys()).sort(), ['1', '2', '3']);
});

test('loading shows placeholder cards in formation under its status', () => {
    const app = loadApp(async () => response({}));

    app.dashboardRenderer.loading('Running local AI for Gameweek 2…');

    const pitch = app.document.getElementById('pitch').innerHTML;
    assert.match(pitch, /role="status"[^>]*>[\s\S]*Running local AI for Gameweek 2…/);
    assert.equal((pitch.match(/player-card placeholder/g) || []).length, 15);
    assert.equal((pitch.match(/aria-hidden="true"><span class="player-card/g) || []).length, 4);
});

test('transfer suggestions close when the deadline passes on an open page, and a saved rating is fetched again', async () => {
    const deadline = Date.parse('2026-11-21T11:00:00Z');
    const gameweeks = [
        { id: 10, is_current: true, finished: true, deadline_time: '2026-11-07T11:00:00Z' },
        { id: 11, is_next: true, deadline_time: '2026-11-21T11:00:00Z' },
        { id: 12, deadline_time: '2026-11-28T11:00:00Z' }
    ];
    const transfer = {
        out: { web_name: 'Gonzalo', team_name: 'Fulham', price: 5.9, expected_points: 2.75 },
        in: { web_name: 'Evanilson', team_name: 'Bournemouth', price: 6.5, expected_points: 6.84 },
        gain: 4.04
    };
    let open = true;
    let tick = null;
    const ratingRequests = [];
    const app = loadApp(async url => {
        const params = new URL(url, 'http://localhost').searchParams;
        if (url.startsWith('/api/scout/team-rating')) {
            ratingRequests.push(url);
            // The server decides from the real deadline on every request.
            return response({
                entry_id: 123, team_name: 'Mine', gameweek: 11, picks_gameweek: 10, rating: 80,
                squad: squad.scout_team.map(player => ({ ...player, pick_position: 1 })),
                bank: 0.5, transfers_open: open, transfers: open ? [transfer] : []
            });
        }
        if (url.startsWith('/api/scout?')) return response({ ...squad, gameweek: Number(params.get('gameweek')) });
        if (url === '/api/scout/sync-results') return response({ status: 'throttled' });
        if (url.startsWith('/api/fpl/fixtures')) return response({ results: [] });
        throw new Error('Unexpected endpoint: ' + url);
    }, { setInterval: callback => { tick = callback; return 1; } });
    app.appState.events = gameweeks;
    app.appState.eventsCache = gameweeks;
    let now = deadline - 60000;
    app.utils.now = () => now;
    const panel = app.document.getElementById('rating-transfers');

    const toggle = app.document.getElementById('transfers-toggle');

    await app.app.start();
    app.teamRating.setMode('rating');
    await app.teamRating.rate(123);
    assert.equal(toggle.disabled, false);
    assert.equal(panel.hidden, true);
    app.teamRating.toggleTransfers();
    assert.equal(panel.hidden, false);
    // Before the deadline the saved rating is reused.
    await app.teamRating.rate(123);
    assert.equal(ratingRequests.length, 1);

    // A minute after the deadline, the next countdown tick closes the panel.
    now = deadline + 60000;
    open = false;
    tick();
    assert.equal(app.document.getElementById('deadline-countdown').textContent, 'Deadline passed');
    assert.equal(panel.hidden, true);
    assert.equal(toggle.disabled, true);
    assert.equal(toggle.textContent, 'Suggested transfers closed');
    // The disabled button cannot reopen them.
    app.teamRating.toggleTransfers();
    assert.equal(panel.hidden, true);

    // Showing the saved rating again keeps it closed.
    app.teamRating.setMode('scout');
    app.teamRating.setMode('rating');
    assert.equal(panel.hidden, true);
    assert.equal(toggle.disabled, true);

    // Rating the same team asks the server again instead of reusing it.
    await app.teamRating.rate(123);
    assert.equal(ratingRequests.length, 2);
    assert.equal(app.appState.ratingData.transfers_open, false);
    assert.equal(panel.hidden, true);
});

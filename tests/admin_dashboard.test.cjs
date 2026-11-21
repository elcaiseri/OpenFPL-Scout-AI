const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/admin/admin.js'), 'utf8');
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });

function memoryStorage() {
    const items = new Map();
    return {
        items,
        getItem: key => (items.has(key) ? items.get(key) : null),
        setItem: (key, value) => { items.set(key, String(value)); },
        removeItem: key => { items.delete(key); }
    };
}

// The dashboard only starts when the page has an #app element; without a
// document it just defines its modules, which is what these tests need.
function loadAdmin(fetch, globals = {}) {
    const context = vm.createContext({
        fetch, AbortController, TypeError, Response, setTimeout, clearTimeout,
        setInterval: () => 0,
        clearInterval() {},
        console: { warn() {}, error() {} },
        window: {},
        localStorage: memoryStorage(),
        sessionStorage: memoryStorage(),
        ...globals
    });
    vm.runInContext(source, context);
    const admin = context.window.OpenFPLAdmin;
    admin.CONFIG.retryDelayMs = 1;
    admin.context = context;
    return admin;
}

test('the token is kept for the session, or on the device when remembered', () => {
    const admin = loadAdmin(async () => response({}));
    const { auth, context } = admin;

    auth.save('  secret  ', false);
    assert.equal(auth.token, 'secret');
    assert.equal(context.sessionStorage.getItem('openfpl.admin.token'), 'secret');
    assert.equal(context.localStorage.getItem('openfpl.admin.token'), null);

    auth.save('kept', true);
    assert.equal(context.localStorage.getItem('openfpl.admin.token'), 'kept');
    auth.token = null;
    context.sessionStorage.removeItem('openfpl.admin.token');
    assert.equal(auth.load(), 'kept');

    auth.clear();
    assert.equal(auth.load(), null);
    assert.deepEqual({ ...auth.headers() }, {});
});

test('requests carry the bearer token, a JSON body and the query string', async () => {
    const calls = [];
    const admin = loadAdmin(async (url, init) => {
        calls.push({ url, init });
        return response({ ok: true });
    });
    admin.auth.save('secret', false);

    const result = await admin.api.post('/api/admin/simulate', { gameweek: 3 }, { force: true, empty: '' });

    assert.deepEqual({ ...result }, { ok: true });
    assert.equal(calls[0].url, '/api/admin/simulate?force=true');
    assert.equal(calls[0].init.method, 'POST');
    assert.equal(calls[0].init.headers.Authorization, 'Bearer secret');
    assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
    assert.equal(calls[0].init.body, '{"gameweek":3}');
    assert.equal(calls[0].init.cache, 'no-store');
});

test('public calls can skip the token', async () => {
    let headers;
    const admin = loadAdmin(async (url, init) => { headers = init.headers; return response({}); });
    admin.auth.save('secret', false);
    await admin.api.get('/api/scout', { gameweek: 3 }, { useAuth: false });
    assert.equal(headers.Authorization, undefined);
});

test('GETs retry gateway errors and network failures; POSTs never retry', async () => {
    let requests = 0;
    const admin = loadAdmin(async () => {
        requests++;
        if (requests === 1) return new Response('Bad gateway', { status: 502 });
        if (requests === 2) throw new TypeError('Failed to fetch');
        return response({ fine: true });
    });
    assert.equal((await admin.api.get('/api/admin/overview')).fine, true);
    assert.equal(requests, 3);

    requests = 0;
    const failing = loadAdmin(async () => { requests++; return new Response('nope', { status: 503 }); });
    await assert.rejects(failing.api.post('/api/admin/predictions/recompute'), { status: 503 });
    assert.equal(requests, 1);
});

test('a rejected token signs the owner out; FastAPI details become the message', async () => {
    let signedOut = 0;
    const admin = loadAdmin(async (url) => (url.includes('overview')
        ? response({ detail: 'Invalid API key' }, 401)
        : response({ detail: [{ msg: 'field required' }, { msg: 'too large' }] }, 422)));
    admin.api.onUnauthorized = () => { signedOut++; };

    await assert.rejects(admin.api.get('/api/admin/overview'), { status: 401 });
    assert.equal(signedOut, 1);
    await assert.rejects(admin.api.get('/api/admin/models'), { status: 422, message: 'field required; too large' });
    assert.equal(signedOut, 1);
});

test('formatting helpers are safe with missing values', () => {
    const { utils } = loadAdmin(async () => response({}));
    assert.equal(utils.fmt(null), '–');
    assert.equal(utils.fmt(1234.567, 2), '1,234.57');
    assert.equal(utils.fmtPct(0.125, 1), '12.5%');
    assert.equal(utils.fmtSigned(-1.5, 1), '−1.5');
    assert.equal(utils.fmtSigned(0, 1), '±0.0');
    assert.equal(utils.fmtBytes(1536), '1.5 KB');
    assert.equal(utils.fmtDuration(3725), '1h 2m');
    assert.equal(utils.fmtCompact(12500), '12.5K');
    assert.equal(utils.escapeHtml('<img src=x onerror="a">'), '&lt;img src=x onerror=&quot;a&quot;&gt;');
    assert.equal(utils.qs({ a: 1, b: null, c: '', d: 'x y' }), '?a=1&d=x%20y');
    assert.equal(utils.csv([{ a: 'x,y', b: 'say "hi"' }], ['a', 'b']), 'a,b\n"x,y","say ""hi"""');
    assert.equal(utils.positionName('Midfielder'), 'MID');
    assert.equal(utils.positionName(1), 'GK');
});

test('axis ticks always cover the data', () => {
    const { charts } = loadAdmin(async () => response({}));
    for (const [low, high] of [[0, 15.2], [3.9, 15.8], [0.55, 0.71], [0, 1], [-2.4, 3.3], [5, 5]]) {
        const ticks = charts.niceTicks(low, high, 5);
        assert.ok(ticks[0] <= low, `${ticks} starts above ${low}`);
        assert.ok(ticks[ticks.length - 1] >= high, `${ticks} ends below ${high}`);
        assert.ok(ticks.length <= 12);
    }
});

test('service health only degrades on server errors, not rejected requests', () => {
    const { app } = loadAdmin(async () => response({}));
    const base = {
        models: { enabled: 4, minimum_successful: 3 },
        last_inference: { failed_models: {} },
        requests: { total_requests: 100, server_errors: 0, client_errors: 40 },
        upstream: {},
        archive: { last_result: { status: 'saved' } }
    };
    assert.equal(app.healthOf(base).kind, 'good');
    assert.equal(app.healthOf({ ...base, requests: { total_requests: 100, server_errors: 10 } }).kind, 'warning');
    assert.equal(app.healthOf({ ...base, last_inference: { failed_models: { mlp: 'boom' } } }).kind, 'warning');
    assert.equal(app.healthOf({ ...base, models: { enabled: 2, minimum_successful: 3 } }).kind, 'critical');
});

test('pitch rows group starters by position and keep the bench order', () => {
    const { ui } = loadAdmin(async () => response({}));
    const players = [
        { id: 1, element_type: 1 }, { id: 2, element_type: 'Defender' }, { id: 3, element_type: 'Defender' },
        { id: 4, element_type: 2 }, { id: 5, position: 'MID' }, { id: 6, element_type: 3 }, { id: 7, element_type: 3 },
        { id: 8, element_type: 3 }, { id: 9, element_type: 4 }, { id: 10, element_type: 4 }, { id: 11, element_type: 3 },
        { id: 14, element_type: 2 }, { id: 12, element_type: 1 }, { id: 15, element_type: 4 }, { id: 13, element_type: 3 }
    ];
    const { rows, bench } = ui.pitchRows(players, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    const plain = value => JSON.parse(JSON.stringify(value));
    assert.deepEqual(plain(rows.map(row => row.map(player => player.id))), [[1], [2, 3, 4], [5, 6, 7, 8, 11], [9, 10]]);
    assert.deepEqual(plain(bench.map(player => player.id)), [14, 12, 15, 13]);
});

/* A working squad of 15: ids 1-15 in FPL slot order (1-4-4-2, bench GK, DEF, MID, FWD). */
const LAYOUT = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4];
function publishedTeam() {
    return {
        entry_id: 77,
        gameweek: 12,
        picks_gameweek: 11,
        published_picks: LAYOUT.map((_, index) => ({ id: index + 1, position: index + 1, is_captain: index === 5, is_vice_captain: index === 6 })),
        squad: LAYOUT.map((position, index) => ({ id: index + 1, web_name: `P${index + 1}`, team_name: `Club ${index + 1}`, element_type: position, price: 5.0 })),
    };
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const order = (value) => plain([...value.picks].sort((a, b) => a.position - b.position).map((pick) => pick.id));

test('a working squad starts from the published picks and survives storage', () => {
    const admin = loadAdmin(async () => response({}));
    const { draft } = admin;
    const value = draft.fromPublished(publishedTeam());
    assert.equal(value.gameweek, 12);
    assert.equal(value.base_picks_gameweek, 11);
    assert.equal(draft.formationOk(value), true);
    assert.equal(draft.formation(value), '4-4-2');
    draft.save(77, value);
    const loaded = draft.load(77);
    assert.deepEqual(order(loaded), order(value));
    assert.ok(loaded.updated_at);
    admin.context.localStorage.setItem('openfpl.admin.draft.v1.78', '{"version":1,"picks":"broken"}');
    assert.equal(draft.load(78), null);
    draft.clear(77);
    assert.equal(draft.load(77), null);
});

test('substitutions keep a legal formation and pass the armband on', () => {
    const { draft } = loadAdmin(async () => response({}));
    const value = draft.fromPublished(publishedTeam());
    // The captain (P6, MID) comes off for the bench defender (P13): 5-3-2 is legal.
    const swapped = draft.swap(value, 6, 13);
    assert.equal(swapped.error, undefined);
    const picks = Object.fromEntries(swapped.draft.picks.map((pick) => [pick.id, pick]));
    assert.ok(picks[13].position <= 11 && picks[13].is_captain);
    assert.ok(picks[6].position > 11 && !picks[6].is_captain);
    assert.equal(draft.formation(swapped.draft), '5-3-2');
    assert.deepEqual(order(value), Array.from({ length: 15 }, (_, index) => index + 1)); // input untouched
    // A keeper only swaps with the keeper; a 3rd forward off for a defender breaks 1+ FWD? no: 2 FWD start.
    assert.match(draft.swap(value, 1, 13).error, /goalkeeper/);
    assert.equal(draft.swap(value, 1, 12).error, undefined);
    const noForward = draft.swap(draft.swap(value, 10, 13).draft, 11, 14);
    assert.match(noForward.error, /illegal formation/);
    assert.match(draft.swap(value, 2, 3).error, /already in the starting XI/);
});

test('bench order, armband and chip edits', () => {
    const { draft } = loadAdmin(async () => response({}));
    const value = draft.fromPublished(publishedTeam());
    const moved = draft.moveBench(value, 15, -1).draft;
    assert.deepEqual(order(moved).slice(11), [12, 13, 15, 14]);
    assert.match(draft.moveBench(value, 13, -1).error, /end of the bench/);
    assert.match(draft.moveBench(value, 12, 1).error, /outfield/);
    const benchSwap = draft.swap(value, 13, 15).draft;
    assert.deepEqual(order(benchSwap).slice(11), [12, 15, 14, 13]);

    const captain = draft.setRole(value, 7, 'is_captain').draft; // the vice becomes captain
    const roles = (item) => plain(item.picks.filter((pick) => pick.is_captain || pick.is_vice_captain).map((pick) => [pick.id, pick.is_captain ? 'C' : 'V']).sort());
    assert.deepEqual(roles(captain), [[6, 'V'], [7, 'C']]);
    assert.deepEqual(roles(draft.setRole(value, 9, 'is_vice_captain').draft), [[6, 'C'], [9, 'V']]);
    assert.match(draft.setRole(value, 13, 'is_captain').error, /starter/);
    assert.equal(draft.setChip(value, '3xc').draft.chip, '3xc');
    assert.equal(draft.setChip(value, 'nonsense').draft.chip, null);
    assert.equal(draft.setPurchasePrice(value, 3, 4.55).draft.purchase_prices[3], 4.6);
    assert.equal(draft.setPurchasePrice(draft.setPurchasePrice(value, 3, 4.5).draft, 3, null).draft.purchase_prices[3], undefined);
});

test('transfers chain, undo and are sent as plain picks', () => {
    const { draft } = loadAdmin(async () => response({}));
    const value = draft.fromPublished(publishedTeam());
    const striker = { id: 101, web_name: 'New', team_name: 'Club X', element_type: 4, price: 7.5 };
    const one = draft.transfer(value, 10, striker).draft;
    assert.ok(one.picks.some((pick) => pick.id === 101));
    assert.deepEqual(plain(one.transfers), [{ out: 10, in: 101 }]);
    assert.match(draft.transfer(one, 11, striker).error, /already in your squad/);
    assert.match(draft.transfer(value, 2, striker).error, /same position/);
    // Selling the new striker for another one keeps a single transfer from P10.
    const chained = draft.transfer(one, 101, { id: 102, web_name: 'Other', element_type: 'FWD', price: 6 }).draft;
    assert.deepEqual(plain(chained.transfers), [{ out: 10, in: 102 }]);
    const undone = draft.undoTransfer(chained, 102).draft;
    assert.deepEqual(plain(undone.transfers), []);
    assert.deepEqual(order(undone), order(value));
    assert.match(draft.undoTransfer(value, 5).error, /isn't a transfer/);

    const request = draft.toRequest(draft.setField(draft.setField(one, 'free_transfers', 2).draft, 'transfers_made', 2).draft);
    assert.deepEqual(Object.keys(request).sort(), ['bank_override', 'chip', 'free_transfers', 'picks', 'purchase_prices', 'transfers_made']);
    assert.equal(request.transfers_made, 2);
    assert.equal(request.free_transfers, 2);
    assert.deepEqual(Object.keys(request.picks[0]).sort(), ['id', 'is_captain', 'is_vice_captain', 'position']);
});

test('a working squad closes with its deadline and goes once FPL shows the team', () => {
    const admin = loadAdmin(async () => response({}));
    const { draft } = admin;
    const value = draft.fromPublished(publishedTeam());
    assert.equal(draft.status(value, { nextOpen: 12, latestPicks: 11 }), 'active');
    // Past the deadline, but FPL hasn't published the GW12 picks yet: kept.
    assert.equal(draft.status(value, { nextOpen: 13, latestPicks: 11 }), 'closed');
    assert.equal(draft.status(value, { nextOpen: 13, latestPicks: 12 }), 'published');
    assert.equal(draft.status(null, { nextOpen: 40 }), null);
    // A closed squad has its own slot, so a new working squad can't overwrite it.
    draft.saveClosed(77, value);
    draft.save(77, { ...value, gameweek: 13 });
    assert.equal(draft.loadClosed(77).gameweek, 12);
    assert.equal(draft.load(77).gameweek, 13);
    draft.clearClosed(77);
    assert.equal(draft.loadClosed(77), null);
    assert.equal(draft.load(77).gameweek, 13);
});

test('export and import round trip, and foreign files are refused', () => {
    const { draft } = loadAdmin(async () => response({}));
    const value = draft.fromPublished(publishedTeam());
    const text = draft.exportJson(value, { 5: { lock: true, web_name: 'P5' }, 9: { junk: 1 } });
    const back = draft.importJson(text);
    assert.deepEqual(order(back.draft), order(value));
    assert.deepEqual(plain(back.overrides), { 5: { lock: true, web_name: 'P5' } });
    assert.throws(() => draft.importJson('{"kind":"something-else"}'), /isn't an OpenFPL/);
    assert.throws(() => draft.importJson(JSON.stringify({ kind: 'openfpl-working-squad', draft: { version: 1 } })), /damaged/);
});

test('personal overrides: locks exclude bans, availability expires with its gameweek', () => {
    const { overrides } = loadAdmin(async () => response({}));
    let all = overrides.set({}, 5, { lock: true, web_name: 'P5' }, 12);
    all = overrides.set(all, 5, { ban: true }, 12);
    assert.deepEqual(plain(all[5]), { ban: true, web_name: 'P5' });
    all = overrides.set(all, 7, { availability: 150, xp_pct: 80 }, 12);
    assert.deepEqual(plain(all[7]), { availability: 100, xp_pct: 80, set_for_gw: 12 });
    all = overrides.set(all, 8, { xp_pct: 100 }, 12);
    all = overrides.set(all, 8, { xp_pct: null }, 12);
    assert.equal(all[8], undefined); // nothing left to keep
    assert.deepEqual(plain(overrides.toRequest(all)), {
        5: { lock: false, ban: true, availability: null, xp_pct: null },
        7: { lock: false, ban: false, availability: 100, xp_pct: 80 },
    });
    const same = overrides.expire(all, 12);
    assert.deepEqual(plain(same.expired), []);
    const later = overrides.expire(all, 13);
    assert.deepEqual(plain(later.expired), [{ id: 7, availability: 100, set_for_gw: 12 }]);
    assert.deepEqual(plain(later.overrides[7]), { xp_pct: 80, set_for_gw: 12 });
    assert.equal(overrides.remove(all, 5)[5], undefined);
    assert.ok(all[5], 'remove returns a copy');
});

test('alerts list what changed since the last snapshot', () => {
    const { alerts } = loadAdmin(async () => response({}));
    const before = [
        { id: 1, web_name: 'A', status: 'a', news: '', chance_of_playing_next_round: null, price: 7.0 },
        { id: 2, web_name: 'B', status: 'a', news: '', chance_of_playing_next_round: null, price: 5.5 },
    ];
    const snapshot = alerts.snapshot(before, new Date('2026-11-01T10:00:00Z'));
    assert.equal(snapshot.at, '2026-11-01T10:00:00.000Z');
    const after = [
        { ...before[0], status: 'd', news: 'Knock - 75% chance of playing', chance_of_playing_next_round: 75 },
        { ...before[1], price: 5.6 },
        { id: 3, web_name: 'New', status: 'a', news: '', price: 6.0 },
    ];
    const changes = plain(alerts.diff(snapshot, after));
    assert.deepEqual(changes.map((item) => [item.id, item.kind]), [[1, 'news'], [1, 'status'], [1, 'chance'], [2, 'price']]);
    assert.equal(changes[1].text, 'A: available → doubtful');
    assert.equal(changes[2].text, 'A: chance of playing no flag → 75%');
    assert.equal(changes[3].delta, 0.1);
    assert.deepEqual(plain(alerts.diff(null, after)), []);
});

test('sortable headers flip direction, and new columns start sensibly', () => {
    const { ui } = loadAdmin(async () => response({}));
    const plain = (value) => JSON.parse(JSON.stringify(value));
    assert.deepEqual(plain(ui.nextSort('xp', 'desc', { key: 'xp', num: true })), { sortKey: 'xp', sortDir: 'asc' });
    assert.deepEqual(plain(ui.nextSort('xp', 'asc', { key: 'name' })), { sortKey: 'name', sortDir: 'asc' });
    assert.deepEqual(plain(ui.nextSort('name', 'asc', { key: 'price', num: true })), { sortKey: 'price', sortDir: 'desc' });
});

test('Tab wraps around inside the open drawer', () => {
    const { drawer } = loadAdmin(async () => response({}));
    const [close, first, last] = ['close', 'first', 'last'];
    const items = [close, first, last];
    assert.equal(drawer.nextFocus(items, last, false), close);
    assert.equal(drawer.nextFocus(items, close, true), last);
    assert.equal(drawer.nextFocus(items, first, false), null); // the browser moves on
    assert.equal(drawer.nextFocus(items, 'outside', false), close);
    assert.equal(drawer.nextFocus(items, 'outside', true), last);
    assert.equal(drawer.nextFocus([], close, false), null);
});

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

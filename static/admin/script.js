/* Owner data and credentials live only in memory. All data rendering uses text nodes. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const fmt = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('en-GB', { maximumFractionDigits: digits, minimumFractionDigits: digits });
    const timestamp = value => value ? new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded';
    const human = value => String(value || 'Not recorded').replaceAll('_', ' ').replaceAll('-', ' ');
    let ownerKey = '', report = null, controller = null, requestId = 0;

    function node(tag, text, className) {
        const element = document.createElement(tag);
        if (text != null) element.textContent = text;
        if (className) element.className = className;
        return element;
    }
    function message(text) {
        $('message').textContent = text;
        $('message').hidden = !text;
    }
    function badge(id, text, tone = '') {
        $(id).textContent = text;
        $(id).className = `badge ${tone}`;
    }
    function metric(label, value, note) {
        const card = node('article', null, 'metric');
        card.append(node('div', label, 'label'), node('div', value, 'value'), node('p', note));
        return card;
    }
    function lock() {
        requestId++;
        controller?.abort();
        controller = null;
        ownerKey = '';
        report = null;
        $('owner-key').value = '';
        $('dashboard').hidden = true;
        $('login').hidden = false;
        $('lock').hidden = true;
        ['summary', 'trend', 'scatter', 'week-overview', 'squad', 'positions', 'players', 'models', 'pipeline', 'runtime', 'warnings'].forEach(id => $(id).replaceChildren());
        ['season', 'gameweek'].forEach(id => $(id).replaceChildren());
        $('search').value = '';
        $('position').value = '';
        $('scope').value = 'all';
        $('evaluation-mode').value = 'all';
        $('evaluation-context').textContent = '';
        $('unlock').disabled = false;
        $('refresh').disabled = false;
        $('dashboard').classList.remove('loading');
        message('');
        $('owner-key').focus();
    }
    async function refresh(initial = false) {
        if (!ownerKey) return;
        const currentId = ++requestId;
        controller?.abort();
        controller = new AbortController();
        const activeController = controller;
        const timeout = setTimeout(() => activeController.abort(), 90000);
        $('unlock').disabled = true;
        $('refresh').disabled = true;
        $('dashboard').classList.add('loading');
        const query = new URLSearchParams();
        if (!initial && $('season').value) query.set('season', $('season').value);
        if (!initial && $('gameweek').value) query.set('gameweek', $('gameweek').value);
        message(initial ? 'Unlocking your dashboard…' : 'Refreshing official results and archive…');
        try {
            const response = await fetch(`/api/admin/dashboard?${query}`, {
                headers: { Authorization: `Bearer ${ownerKey}` },
                credentials: 'omit', cache: 'no-store', signal: activeController.signal,
            });
            if (currentId !== requestId) return;
            if (response.status === 401) {
                lock();
                message('That owner key was not accepted. Enter the dedicated dashboard key.');
                return;
            }
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(typeof error.detail === 'string' ? error.detail : 'The dashboard could not load. Please retry.');
            }
            const payload = await response.json();
            if (currentId !== requestId) return;
            report = payload;
            render();
            $('login').hidden = true;
            $('dashboard').hidden = false;
            $('lock').hidden = false;
            message('');
        } catch (error) {
            if (currentId !== requestId) return;
            message(`${error.name === 'AbortError' ? 'The request timed out. Please retry.' : error.message}${report ? ' The previous data is still shown below.' : ''}`);
            if (initial) ownerKey = '';
        } finally {
            clearTimeout(timeout);
            if (currentId === requestId) {
                $('unlock').disabled = false;
                $('refresh').disabled = false;
                $('dashboard').classList.remove('loading');
            }
        }
    }
    function options(id, values, selected) {
        $(id).replaceChildren(...values.map(([value, label]) => {
            const option = node('option', label);
            option.value = value;
            option.selected = String(value) === String(selected);
            return option;
        }));
    }
    function render() {
        const data = report, week = data.selected;
        options('season', data.seasons.map(s => [s, s.replace('-', ' / ')]), data.season);
        options('gameweek', data.gameweeks.map(w => [w.gameweek, `GW ${w.gameweek}${w.prediction_count ? '' : ' · no forecast'}`]), week?.gameweek);
        $('updated').textContent = `Snapshot ${timestamp(data.generated_at_utc)} · All times shown in your timezone`;
        $('warnings').replaceChildren(...data.warnings.map(w => node('p', w)));
        $('warnings').hidden = !data.warnings.length;
        renderSeason();
        $('week-label').textContent = week ? `GAMEWEEK ${week.gameweek} REVIEW` : 'GAMEWEEK REVIEW';
        badge('result-badge', human(week?.result_state), week?.result_state === 'final' ? 'good' : 'warn');
        $('week-context').textContent = week?.prediction_count
            ? `${week.eligible ? 'Saved before deadline' : week.forecast_state === 'post-deadline' ? 'Saved after deadline · retrospective comparison' : 'Timing unknown · retrospective comparison'}. Run: ${timestamp(week.captured_at_utc)}. Deadline: ${timestamp(week.deadline_time)}. Scores: ${timestamp(week.actuals_at_utc)}.`
            : 'No forecast was archived for this gameweek. Forecasts are saved when the public scout runs.';
        renderScatter(week?.players || []);
        const stats = week?.metrics || {}, count = week?.prediction_count || 0;
        $('week-overview').replaceChildren(
            metric('GW · MEAN ABSOLUTE ERROR', fmt(stats.mae), week?.result_state !== 'final' ? 'Awaiting final scores · excluded from season totals' : week.eligible ? 'Verified final result' : 'Retrospective comparison · excluded from verified accuracy'),
            metric('GW · PREDICTION BIAS', stats.bias == null ? '—' : `${stats.bias > 0 ? '+' : ''}${fmt(stats.bias)}`, 'Positive = overprediction; negative = underprediction'),
            metric('MATCHED ACTUAL SCORES', `${stats.count || 0} / ${count}`, 'Missing scores are excluded from error metrics'),
            metric('FORECAST STATUS', week?.eligible ? 'Verified' : week?.forecast_state === 'post-deadline' ? 'Late run' : count ? 'Unverified' : 'Missing', week?.preserved ? 'Preserved pre-deadline snapshot' : week?.forecast_state === 'post-deadline' ? 'Saved after the official deadline' : 'Legacy archive or no saved snapshot'),
        );
        renderSquad(week?.squad);
        $('positions').replaceChildren(...(week?.positions || []).map(p => {
            const row = node('div', null, 'position-row');
            const meter = node('meter');
            meter.min = 0; meter.max = Math.max(5, ...(week.positions.map(v => v.mae || 0)));
            meter.value = p.mae || 0;
            meter.setAttribute('aria-label', `${p.position} mean absolute error: ${fmt(p.mae)} points`);
            row.append(node('strong', p.position), meter, node('span', fmt(p.mae)), node('small', `${p.count} scores`));
            return row;
        }), node('p', 'MAE in points, for matched players in the selected gameweek. Includes players with zero minutes.', 'fine'));
        renderPlayers();
        renderSystem(data);
    }
    function renderSeason() {
        if (!report) return;
        const verified = $('evaluation-mode').value === 'verified';
        const summary = verified ? report.summary : report.comparison_summary;
        const retrospective = report.comparison_summary.post_deadline_gameweeks;
        const unknown = report.comparison_summary.unknown_timing_gameweeks;
        $('evaluation-label').textContent = verified ? 'VERIFIED PRE-DEADLINE ACCURACY' : 'ARCHIVED RUNS VS FINAL RESULTS';
        $('evaluation-context').textContent = verified
            ? `${summary.evaluated_gameweeks} finalized gameweeks have forecasts saved before their deadlines. ${retrospective + unknown} gameweeks are available in All archived runs. Upcoming forecasts enter this view when scores are final.`
            : `${summary.evaluated_gameweeks} gameweeks compared · ${report.comparison_summary.verified_gameweeks} saved before deadline · ${retrospective} saved after deadline · ${unknown} with unknown timing. Late or undated runs are retrospective comparisons and do not measure pre-deadline forecasting accuracy.`;
        const prefix = verified ? 'VERIFIED' : 'ARCHIVE';
        $('summary').replaceChildren(
            metric(`${prefix} · MEAN ABSOLUTE ERROR`, fmt(summary.mae), 'Points away from the actual score, on average'),
            metric(`${prefix} · RMSE`, fmt(summary.rmse), 'Larger misses carry more weight'),
            metric(`${prefix} · WITHIN 2 POINTS`, summary.within_two_pct == null ? '—' : `${fmt(summary.within_two_pct, 1)}%`, 'Of matched players with finalized scores'),
            metric(verified ? 'VERIFIED FINAL GAMEWEEKS' : 'GAMEWEEKS COMPARED', fmt(summary.evaluated_gameweeks, 0), `${fmt(summary.count, 0)} matched forecasts · ${summary.archived_gameweeks} archived GWs`),
        );
        $('trend-context').textContent = verified
            ? 'Pre-deadline forecasts with final scores. Lower is better.'
            : 'Archived runs against final scores. Late and undated runs are retrospective. Lower is better.';
        renderTrend(report.gameweeks, verified);
    }
    function renderSquad(squad) {
        badge('squad-badge', squad?.eligible ? 'Verified selection' : 'Unverified selection', squad?.eligible ? 'good' : 'warn');
        const target = $('squad'); target.replaceChildren();
        if (!squad?.count) { target.append(node('p', 'No squad was saved for this forecast.', 'empty')); return; }
        const numbers = node('div', null, 'squad-numbers');
        for (const [label, value] of [['Predicted points', squad.expected_points], ['Actual points', squad.actual_points], ['Players matched', `${squad.matched} / ${squad.count}`]]) {
            const item = node('div');
            item.append(node('strong', typeof value === 'string' ? value : fmt(value, 1)), node('span', label)); numbers.append(item);
        }
        target.append(numbers);
        if (squad.captain) {
            const captain = node('div', null, 'captain');
            captain.append(node('strong', `C · ${squad.captain.name}`), node('p', `${fmt(squad.captain.expected_points)} predicted → ${fmt(squad.captain.actual_points)} actual points (before doubling).`));
            target.append(captain);
        }
        target.append(node('p', squad.note, 'fine'));
        if (!squad.eligible) target.append(node('p', 'This selection is shown for reference. Its pre-deadline timing or match to the forecast could not be verified.', 'fine'));
    }
    function filteredPlayers() {
        const search = $('search').value.trim().toLocaleLowerCase(), position = $('position').value, scope = $('scope').value, sort = $('sort').value;
        return (report?.selected?.players || []).filter(p =>
            (!search || `${p.name} ${p.team}`.toLocaleLowerCase().includes(search)) &&
            (!position || p.position === position) &&
            (scope !== 'squad' || p.in_squad) && (scope !== 'matched' || p.actual_points != null) && (scope !== 'missing' || p.actual_points == null)
        ).sort((a, b) => {
            if (sort === 'name') return a.name.localeCompare(b.name);
            const value = p => p[sort] == null ? -Infinity : sort === 'error' ? Math.abs(p.error) : p[sort];
            return value(b) - value(a) || a.name.localeCompare(b.name);
        });
    }
    function renderPlayers() {
        const players = filteredPlayers();
        $('player-count').textContent = `${players.length} of ${report?.selected?.prediction_count || 0} players · ${human(report?.selected?.forecast_state)} forecast · ${human(report?.selected?.result_state)} scores`;
        $('players').replaceChildren(...players.map(p => {
            const row = node('tr'), name = node('td');
            name.append(node('strong', p.name), node('small', p.team));
            row.append(name, node('td', p.position), node('td', fmt(p.expected_points), 'numeric'), node('td', fmt(p.actual_points, 0), 'numeric'), node('td', p.error == null ? '—' : `${p.error > 0 ? '+' : ''}${fmt(p.error)}`, p.error > 0 ? 'over' : 'under'), node('td', fmt(p.minutes, 0)), node('td', p.role ? human(p.role) : p.in_squad ? 'Squad' : '—'));
            return row;
        }));
        if (!players.length) { const row = node('tr'), cell = node('td', 'No players match this view.', 'empty'); cell.colSpan = 7; row.append(cell); $('players').append(row); }
        $('export').disabled = !players.length;
    }
    function renderSystem(data) {
        const system = data.system, enrichment = system.enrichment, archive = system.archive;
        badge('official-badge', `Official FPL · ${data.official_status}`, data.official_status === 'available' ? 'good' : 'warn');
        const feature = system.feature_coverage;
        const cards = [
            ['Durable archive', archive.enabled ? human(archive.last_result.status) : 'Disabled', `Latest saved inference: ${timestamp(system.last_inference_at)}. ${archive.last_result.error || archive.root_path}`],
            ['Historical enrichment', `${system.enrichment_enabled ? 'Enabled' : 'Disabled'} · last run: ${human(enrichment.status)}`, `Matched rows: ${fmt(enrichment.matched_rows, 0)} / ${fmt(enrichment.eligible_rows, 0)}. Match rate: ${enrichment.match_ratio == null ? '—' : fmt(enrichment.match_ratio * 100, 1) + '%'}. Missing: ${(enrichment.missing_features || []).join(', ') || 'not recorded'}.`],
            ['Inference inputs', feature ? `${feature.populated.length} / ${feature.total} features populated` : 'Feature coverage not recorded', feature ? `Entirely missing: ${feature.entirely_missing.join(', ') || 'none'}. Model spread: ${fmt(system.mean_model_spread)} pts.` : 'Feature coverage will appear after the next successful model inference.'],
        ];
        $('pipeline').replaceChildren(...cards.map(([title, value, note]) => {
            const article = node('article'); article.append(node('h3', title), node('strong', value), node('p', note)); return article;
        }));
        $('models').replaceChildren(...system.models.map(model => {
            const row = node('tr');
            [human(model.name), model.version || '—', model.loaded ? 'Loaded' : 'Not loaded', model.error || human(model.last_inference), fmt(model.weight, 1), model.last_trained || '—'].forEach(v => row.append(node('td', v)));
            return row;
        }));
        $('model-context').textContent = `Loaded status reflects this instance. Run diagnostics reflect the latest archive in the selected season: ${system.last_inference_gameweek ? `GW${system.last_inference_gameweek}` : 'no run recorded'} · ${human(system.strategy)} · ${timestamp(system.last_inference_at)}.`;
        const runtime = data.runtime;
        $('runtime-scope').textContent = runtime.scope;
        $('runtime').replaceChildren(...[
            ['Uptime', `${fmt(runtime.uptime_seconds / 3600, 1)} h`], ['Requests', fmt(runtime.requests, 0)], ['Server errors', fmt(runtime.server_errors, 0)], ['P95 response', runtime.p95_latency_ms == null ? '—' : `${fmt(runtime.p95_latency_ms, 0)} ms`],
        ].map(([label, value]) => { const item = node('div'); item.append(node('strong', value), node('span', label)); return item; }));
    }
    const svgNS = 'http://www.w3.org/2000/svg';
    function svgNode(tag, attrs = {}, text) {
        const element = document.createElementNS(svgNS, tag);
        Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
        if (text != null) element.textContent = text;
        return element;
    }
    function chart(id, label) {
        const svg = svgNode('svg', { viewBox: '0 0 540 230', role: 'img', 'aria-label': label });
        $(id).replaceChildren(svg); return svg;
    }
    function emptyChart(id, text) { $(id).replaceChildren(node('p', text, 'empty')); }
    function renderTrend(weeks, verified) {
        const points = weeks.filter(w => (!verified || w.eligible) && w.result_state === 'final' && w.metrics.mae != null);
        if (!points.length) { emptyChart('trend', verified ? 'No finalized pre-deadline forecasts yet. Choose All archived runs to review existing comparisons.' : 'No archived runs have matched final scores yet. Live comparisons remain available in the gameweek review.'); return; }
        const svg = chart('trend', `${verified ? 'Verified forecast' : 'Archived run comparison'} mean absolute error by finalized gameweek. Each point can select that gameweek.`);
        const max = Math.max(1, ...points.map(w => w.metrics.mae)) * 1.2;
        const minGW = points[0].gameweek, maxGW = points.at(-1).gameweek;
        const x = gw => minGW === maxGW ? 290 : 45 + (gw - minGW) / (maxGW - minGW) * 465;
        const y = v => 190 - v / max * 170;
        for (let i = 0; i <= 4; i++) {
            const value = max * i / 4;
            svg.append(svgNode('line', { x1: 45, x2: 510, y1: y(value), y2: y(value), class: 'grid-line' }), svgNode('text', { x: 32, y: y(value) + 4, 'text-anchor': 'end' }, fmt(value, 1)));
        }
        svg.append(svgNode('polyline', { points: points.map(w => `${x(w.gameweek)},${y(w.metrics.mae)}`).join(' '), class: 'series' }));
        const labelStep = Math.max(1, Math.ceil(points.length / 10));
        points.forEach((w, index) => {
            const label = `GW ${w.gameweek}: ${fmt(w.metrics.mae)} points MAE, ${w.metrics.count} matched players, ${w.eligible ? 'verified pre-deadline forecast' : 'retrospective comparison'}`;
            const dot = svgNode('circle', { cx: x(w.gameweek), cy: y(w.metrics.mae), r: 5, class: 'point', tabindex: 0, role: 'button', 'aria-label': label });
            dot.append(svgNode('title', {}, label));
            const selectWeek = () => { $('gameweek').value = w.gameweek; refresh(); };
            dot.addEventListener('click', selectWeek);
            dot.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectWeek(); } });
            svg.append(dot);
            if (index % labelStep === 0) svg.append(svgNode('text', { x: x(w.gameweek), y: 216, 'text-anchor': 'middle' }, `GW${w.gameweek}`));
        });
    }
    function renderScatter(players) {
        const points = players.filter(p => p.actual_points != null);
        if (!points.length) { emptyChart('scatter', 'No matched official scores yet. Players will appear here when results are available.'); return; }
        const svg = chart('scatter', 'Player forecasts on the horizontal axis against actual FPL points on the vertical axis. The dashed diagonal represents a perfect forecast.');
        const min = Math.min(0, ...points.flatMap(p => [p.actual_points, p.expected_points]));
        const max = Math.max(2, ...points.flatMap(p => [p.actual_points, p.expected_points])) + 1;
        const x = value => 45 + (value - min) / (max - min) * 465;
        const y = value => 185 - (value - min) / (max - min) * 160;
        for (let i = 0; i <= 4; i++) {
            const value = min + (max - min) * i / 4;
            svg.append(svgNode('line', { x1: 45, x2: 510, y1: y(value), y2: y(value), class: 'grid-line' }), svgNode('text', { x: 32, y: y(value) + 4, 'text-anchor': 'end' }, fmt(value, 0)), svgNode('text', { x: x(value), y: 204, 'text-anchor': 'middle' }, fmt(value, 0)));
        }
        svg.append(svgNode('line', { x1: x(min), y1: y(min), x2: x(max), y2: y(max), class: 'reference' }), svgNode('text', { x: 280, y: 226, 'text-anchor': 'middle' }, 'Predicted points →'), svgNode('text', { x: 45, y: 13 }, 'Actual points ↑'));
        points.forEach(p => {
            const dot = svgNode('circle', { cx: x(p.expected_points), cy: y(p.actual_points), r: p.in_squad ? 4.5 : 3, class: 'scatter-point' });
            dot.append(svgNode('title', {}, `${p.name}: ${fmt(p.expected_points)} predicted, ${fmt(p.actual_points, 0)} actual`)); svg.append(dot);
        });
    }
    function exportCSV() {
        if (!report) return;
        const week = report.selected;
        const header = ['season', 'gameweek', 'forecast_status', 'result_status', 'forecast_saved_at', 'scores_observed_at', 'player_id', 'player', 'club', 'position', 'predicted', 'actual', 'error', 'minutes', 'in_squad', 'role'];
        const rows = filteredPlayers().map(p => [report.season, week.gameweek, week.forecast_state, week.result_state, week.captured_at_utc, week.actuals_at_utc, p.id, p.name, p.team, p.position, p.expected_points, p.actual_points, p.error, p.minutes, p.in_squad, p.role]);
        const escape = value => {
            let text = value == null ? '' : String(value);
            if (typeof value === 'string' && /^[\s]*[=+\-@\t\r\n]/.test(text)) text = `'${text}`;
            return `"${text.replaceAll('"', '""')}"`;
        };
        const csv = [header, ...rows].map(row => row.map(escape).join(',')).join('\r\n');
        const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8;' }));
        const link = node('a'); link.href = url; link.download = `openfpl-${report.season}-gw${week.gameweek}.csv`; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    $('login-form').addEventListener('submit', event => { event.preventDefault(); ownerKey = $('owner-key').value.trim(); $('owner-key').value = ''; refresh(true); });
    $('lock').addEventListener('click', lock);
    $('refresh').addEventListener('click', () => refresh());
    $('gameweek').addEventListener('change', () => refresh());
    $('season').addEventListener('change', () => { $('gameweek').replaceChildren(); refresh(); });
    $('evaluation-mode').addEventListener('change', renderSeason);
    ['search', 'position', 'scope', 'sort'].forEach(id => $(id).addEventListener('input', renderPlayers));
    $('export').addEventListener('click', exportCSV);
    setInterval(() => { if (ownerKey && !document.hidden && !$('refresh').disabled) refresh(); }, 60000);
    window.addEventListener('pagehide', lock);
})();

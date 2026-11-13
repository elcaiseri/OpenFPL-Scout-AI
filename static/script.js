/** OpenFPL Scout AI Team — official-data prediction dashboard. */

const CONFIG = {
    defaultGameweek: 1,
    cacheExpiry: 5 * 60 * 1000,
    requestRetries: 2,
    retryDelayMs: 500,
    requestTimeoutMs: 30000,
    retryStatuses: [429, 500, 502, 503, 504],
    // Squad and team-rating reads can run full model inference. They wait as
    // long as the server takes, and only a gateway or cold-start failure
    // (502/503) is retried: after a server error (500) or timeout (504) a
    // retry would rerun the same slow inference.
    inferenceRequest: { timeoutMs: 0, retryStatuses: [502, 503] },
    endpoints: {
        scout: '/api/scout',
        syncResults: '/api/scout/sync-results',
        refresh: gameweek => `/api/scout/refresh?gameweek=${gameweek}`,
        teamRating: (entryId, gameweek) => `/api/scout/team-rating?entry_id=${entryId}&gameweek=${gameweek}`,
        events: '/api/fpl/gameweeks',
        fixtures: gameweek => `/api/fpl/fixtures?gameweek=${gameweek}`
    }
};

// FPL's own three-letter codes by official and app club name, so a squad shown
// before its fixtures load never labels both Manchester clubs "MAN". Codes
// learned from fixtures win; a club missing here falls back to its letters.
const CLUB_CODES = new Map(Object.entries({
    arsenal: 'ARS', 'aston villa': 'AVL', bournemouth: 'BOU', brentford: 'BRE',
    brighton: 'BHA', burnley: 'BUR', chelsea: 'CHE', 'crystal palace': 'CRY',
    everton: 'EVE', fulham: 'FUL', ipswich: 'IPS', leeds: 'LEE', leicester: 'LEI',
    liverpool: 'LIV', luton: 'LUT', 'man city': 'MCI', 'manchester city': 'MCI',
    'man utd': 'MUN', 'manchester united': 'MUN', newcastle: 'NEW',
    'newcastle united': 'NEW', "nott'm forest": 'NFO', 'nottingham forest': 'NFO',
    'sheffield utd': 'SHU', southampton: 'SOU', spurs: 'TOT', tottenham: 'TOT',
    sunderland: 'SUN', 'west ham': 'WHU', wolves: 'WOL', 'wolverhampton wanderers': 'WOL'
}));

const appState = {
    currentGameweek: CONFIG.defaultGameweek,
    // The Gameweek the app opens on; only its squad is saved for next visit.
    openingGameweek: null,
    events: [],
    visibleEvents: [],
    currentData: null,
    isLoading: false,
    // Identifies the newest Gameweek load; an older one may still be finishing.
    loadToken: 0,
    isRatingLoading: false,
    activeView: 'pitch',
    squadMode: 'scout',
    ratingEntryId: null,
    ratingData: null,
    dashboardCache: new Map(),
    ratingCache: new Map(),
    eventsCache: null,
    eventsTimestamp: 0,
    resultsSyncPromise: null,
    // Refresh requests in flight, one per Gameweek.
    refreshPromises: new Map(),
    teamCodes: new Map(),
    pitchWidth: 0,
    // Players on the pitch and in the list, by their cards' data-player key.
    shownPlayers: new Map(),
    // The List view's order: a column and a direction.
    tableSort: { key: 'xpts', direction: 'desc' },
    tableRows: [],
    countdownTimer: null
};

const utils = {
    escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    },

    number(value, fallback = null) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    },

    money(value) {
        const number = this.number(value);
        return number === null ? '—' : `£${number.toFixed(1)}m`;
    },

    percentage(value) {
        const number = this.number(value);
        return number === null ? '—' : `${number.toFixed(1)}%`;
    },

    positionCode(player) {
        const value = player.position || player.element_type;
        const positions = {
            1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD',
            Goalkeeper: 'GK', Defender: 'DEF', Midfielder: 'MID', Forward: 'FWD'
        };
        return positions[value] || '—';
    },

    // Official three-letter club codes (LIV, MCI…) keep fixtures legible on a
    // phone-width pitch; an unmapped club falls back to its first letters.
    teamCode(name) {
        const label = String(name ?? '').trim();
        if (!label) return '—';
        const key = label.toLowerCase();
        return appState.teamCodes.get(key) || CLUB_CODES.get(key)
            || label.replace(/[^a-z]/gi, '').slice(0, 3).toUpperCase() || '—';
    },

    // Double gameweeks join opponents as "Arsenal / Chelsea", and mark each
    // venue when they differ: "Arsenal (H) / Chelsea (A)".
    opponentCode(label) {
        if (!label || label === 'TBC') return 'TBC';
        return String(label).split(' / ').map(part => {
            const match = part.match(/^(.*?)\s*\((H|A)\)$/);
            return match ? `${this.teamCode(match[1])} (${match[2]})` : this.teamCode(part);
        }).join(' / ');
    },

    // The points a squad can actually score, as the team rating's AI benchmark
    // counts them (src/team_rating.py): the highest-projected legal XI — one
    // goalkeeper, 3–5 defenders, 2–5 midfielders, 1–3 forwards — plus the
    // armband, which doubles its top projection. Null without a legal XI.
    bestLineupPoints(players) {
        const byPosition = { GK: [], DEF: [], MID: [], FWD: [] };
        players.forEach(player => {
            byPosition[this.positionCode(player)]?.push(Math.max(0, this.number(player.expected_points, 0)));
        });
        Object.values(byPosition).forEach(points => points.sort((a, b) => b - a));
        let best = null;
        for (let defenders = 3; defenders <= 5; defenders += 1) {
            for (let midfielders = 2; midfielders <= 5; midfielders += 1) {
                const forwards = 10 - defenders - midfielders;
                if (forwards < 1 || forwards > 3) continue;
                const counts = { GK: 1, DEF: defenders, MID: midfielders, FWD: forwards };
                if (Object.entries(counts).some(([code, count]) => byPosition[code].length < count)) continue;
                const lineup = Object.entries(counts).flatMap(([code, count]) => byPosition[code].slice(0, count));
                const base = lineup.reduce((sum, points) => sum + points, 0);
                if (best === null || base > best.base) best = { base, captain: Math.max(...lineup) };
            }
        }
        return best && best.base + best.captain;
    },

    // A published FPL pick carries its slot: 1–11 start, 12–15 are the bench
    // in substitution order. Scout picks carry no slot.
    isBench(player) {
        return this.number(player.pick_position, 0) > 11;
    },

    // A double gameweek with a home and an away match has no single venue;
    // its opponent label then names each fixture's venue.
    venueCode(player) {
        if (player.was_home === true) return 'H';
        if (player.was_home === false) return 'A';
        return '';
    },

    statusInfo(status, canSelect = true) {
        const states = {
            a: ['Available', 'available'],
            d: ['Doubtful', 'doubtful'],
            i: ['Injured', 'unavailable'],
            s: ['Suspended', 'unavailable'],
            u: ['Unavailable', 'unavailable'],
            n: ['Unavailable', 'unavailable']
        };
        if (!canSelect) return { label: 'Unavailable', className: 'unavailable' };
        const [label, className] = states[String(status || 'a').toLowerCase()] || states.a;
        return { label, className };
    },

    formatDate(value, options = {}) {
        if (!value) return 'TBC';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return 'TBC';
        return new Intl.DateTimeFormat('en-GB', options).format(date);
    },

    deadline(value) {
        return this.formatDate(value, {
            weekday: 'short', day: 'numeric', month: 'short',
            hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
        });
    },

    kickoff(value) {
        return this.formatDate(value, {
            weekday: 'short', hour: '2-digit', minute: '2-digit'
        });
    },

    // The time deadline decisions use; tests move it.
    now() {
        return Date.now();
    },

    // Whether a Gameweek's official deadline is known and has passed.
    deadlinePassed(gameweek) {
        const event = appState.events.find(item => Number(item.id) === Number(gameweek));
        const deadline = new Date(event?.deadline_time).getTime();
        return Number.isFinite(deadline) && this.now() >= deadline;
    },

    countdown(value) {
        if (!value) return 'Deadline TBC';
        const distance = new Date(value).getTime() - this.now();
        if (!Number.isFinite(distance)) return 'Deadline TBC';
        if (distance <= 0) return 'Deadline passed';

        const totalMinutes = Math.floor(distance / 60000);
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        if (days > 0) return `${days}d ${hours}h ${minutes}m`;
        if (hours > 0) return `${hours}h ${minutes}m`;
        return `${minutes}m`;
    },

    debounce(callback, wait) {
        let timer;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => callback(...args), wait);
        };
    },

    async fetchJson(url, {
        method = 'GET',
        timeoutMs = CONFIG.requestTimeoutMs,
        retryStatuses = CONFIG.retryStatuses
    } = {}) {
        const retries = method === 'GET' ? CONFIG.requestRetries : 0;
        for (let attempt = 0; attempt <= retries; attempt++) {
            const controller = new AbortController();
            const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
            let response;
            try {
                response = await fetch(url, {
                    method,
                    signal: controller.signal,
                    headers: { Accept: 'application/json' }
                });
                let payload;
                try {
                    payload = await response.json();
                } catch (error) {
                    if (error instanceof TypeError || error.name === 'AbortError') throw error;
                    throw new Error(`Invalid response from ${url}`);
                }
                if (!response.ok) {
                    throw new Error(payload.detail || `Request failed (${response.status})`);
                }
                return payload;
            } catch (error) {
                const temporary = error instanceof TypeError || error.name === 'AbortError'
                    || (response && retryStatuses.includes(response.status));
                if (!temporary || attempt === retries) {
                    if (error.name === 'AbortError') {
                        throw new Error('The FPL request timed out. Please try again.');
                    }
                    throw error;
                }
            } finally {
                clearTimeout(timer);
            }
            await new Promise(resolve => setTimeout(resolve, CONFIG.retryDelayMs * 2 ** attempt));
        }
    },

    eventLabel(event) {
        if (!event) return 'Awaiting Gameweek';
        if (event.finished) return 'Full-time';
        if (event.is_current) return 'Live';
        if (event.is_next) return 'Next deadline';
        if (new Date(event.deadline_time).getTime() < Date.now()) return 'Updating';
        return 'Scheduled';
    },

    clearCache() {
        appState.dashboardCache.clear();
        appState.ratingCache.clear();
        appState.eventsCache = null;
        appState.eventsTimestamp = 0;
    }
};

const dom = new Proxy({}, {
    get(_target, property) {
        const ids = {
            pitch: 'pitch', gameweekInfo: 'gameweek-info', gameweekSelect: 'gameweek-select',
            gameweekWindow: 'gameweek-window',
            refreshButton: 'refresh-button', sourceState: 'source-state',
            deadlineCountdown: 'deadline-countdown', deadlineDate: 'deadline-date',
            eventState: 'event-state', pulseDeadline: 'pulse-deadline',
            pulseDeadlineSub: 'pulse-deadline-sub', fixtureCount: 'fixture-count',
            dataState: 'data-state', dataStateSub: 'data-state-sub', lastSync: 'last-sync',
            signalState: 'signal-state', totalPoints: 'total-points', captainName: 'captain-name',
            modelVersion: 'model-version',
            captainPoints: 'captain-points', squadValue: 'squad-value',
            differentialCount: 'differential-count', fixtureRail: 'fixture-rail',
            squadSubtitle: 'squad-subtitle', formationChip: 'formation-chip',
            squadTools: 'squad-tools', squadDisplay: 'squad-display',
            pitchView: 'pitch-view', tableView: 'table-view', tableBody: 'squad-table-body',
            teamRatingPanel: 'team-rating-panel', teamRatingForm: 'team-rating-form',
            teamIdInput: 'team-id-input', rateTeamButton: 'rate-team-button',
            teamIdSuggestions: 'team-id-suggestions',
            teamRatingError: 'team-rating-error', teamRatingResult: 'team-rating-result',
            ratingScoreRing: 'rating-score-ring', ratingScore: 'rating-score',
            ratingGrade: 'rating-grade', ratingTeamName: 'rating-team-name',
            ratingManagerName: 'rating-manager-name', ratingPicksLabel: 'rating-picks-label',
            ratingProjection: 'rating-projection', ratingBenchmark: 'rating-benchmark',
            ratingGap: 'rating-gap', ratingStrengths: 'rating-strengths',
            ratingRisks: 'rating-risks', ratingTransfers: 'rating-transfers',
            ratingTransferList: 'rating-transfer-list', ratingBank: 'rating-bank',
            ratingClubLimit: 'rating-club-limit',
            shareRating: 'share-rating',
            credits: 'credits', playerDialog: 'player-dialog', dialogClose: 'dialog-close',
            dialogPositionBadge: 'dialog-position-badge', dialogPlayerName: 'dialog-player-name',
            dialogTeam: 'dialog-team', dialogPoints: 'dialog-points',
            dialogFixture: 'dialog-fixture', dialogVenue: 'dialog-venue',
            dialogPrice: 'dialog-price', dialogOwnership: 'dialog-ownership',
            dialogTotalPoints: 'dialog-total-points', dialogRole: 'dialog-role',
            dialogNews: 'dialog-news'
        };
        return document.getElementById(ids[property] || property);
    }
});

const dataLoader = {
    // The season's gameweeks are small and decide which squad to ask for, so
    // start-up waits for them alone. Squad players arrive with their current
    // official details (price, ownership, status, news), so no player list
    // is downloaded.
    async loadEvents(force = false) {
        const fresh = appState.eventsCache
            && Date.now() - appState.eventsTimestamp < CONFIG.cacheExpiry;
        if (fresh && !force) return appState.eventsCache;
        const payload = await utils.fetchJson(CONFIG.endpoints.events);
        appState.eventsCache = payload.results || [];
        appState.eventsTimestamp = Date.now();
        return appState.eventsCache;
    },

    // A failed refresh keeps the gameweeks already loaded, marked unavailable
    // so the dashboard built from them is not cached.
    async loadSeason(force = false) {
        try {
            return { events: await this.loadEvents(force), available: true };
        } catch (error) {
            console.warn('Supporting FPL data unavailable:', error);
            return { events: appState.events, available: false };
        }
    },

    async loadDashboard(gameweek, force = false, onScoutReady = null) {
        if (appState.dashboardCache.has(gameweek) && !force) {
            return appState.dashboardCache.get(gameweek);
        }

        let fixturesAvailable = true;
        const scoutRequest = utils.fetchJson(
            `${CONFIG.endpoints.scout}?gameweek=${gameweek}`, CONFIG.inferenceRequest
        );
        const detailsRequest = Promise.all([
            this.loadSeason(force),
            utils.fetchJson(CONFIG.endpoints.fixtures(gameweek)).catch(error => {
                console.warn('Fixtures unavailable:', error);
                fixturesAvailable = false;
                return { results: [] };
            })
        ]);
        const scout = await scoutRequest;
        if (onScoutReady) {
            // The squad already carries names, positions, prices and status.
            // Show it while the fixtures catch up.
            onScoutReady({
                ...scout,
                event: appState.events.find(event => Number(event.id) === Number(gameweek)) || null,
                events: appState.events,
                fixtures: [],
                fixturesAvailable: false,
                fixturesLoading: true,
                syncedAt: new Date()
            });
        }
        const [season, fixturePayload] = await detailsRequest;
        const fixtures = fixturePayload.results || [];
        this.learnTeamCodes(fixtures, scout.scout_team || []);

        const players = (scout.scout_team || []).map(player => {
            const fixture = this.playerFixture(player, fixtures);
            return {
                ...player,
                position: player.position || player.element_type,
                fixture_id: fixture?.id || null,
                difficulty: fixture?.difficulty || null,
                kickoff_time: fixture?.kickoff_time || null
            };
        });

        const dashboard = {
            ...scout,
            scout_team: players,
            event: season.events.find(event => Number(event.id) === Number(gameweek)) || null,
            events: season.events,
            fixtures,
            fixturesAvailable,
            syncedAt: new Date()
        };
        // A stale squad is not kept: the next view of its Gameweek asks again
        // and gets the refreshed forecast.
        if (season.available && fixturesAvailable && !scout.refresh_due) {
            appState.dashboardCache.set(gameweek, dashboard);
        }
        return dashboard;
    },

    // The server answered with an older forecast so the page did not wait
    // (refresh_due). The new prediction runs in this request of its own,
    // which is when request-billed Cloud Run gives the server CPU.
    refreshPredictions(gameweek) {
        const key = Number(gameweek);
        if (appState.refreshPromises.has(key)) return appState.refreshPromises.get(key);
        const request = utils.fetchJson(CONFIG.endpoints.refresh(key), {
            method: 'POST', timeoutMs: 0
        }).catch(error => {
            console.warn('Squad refresh unavailable:', error);
        }).finally(() => {
            appState.refreshPromises.delete(key);
        });
        appState.refreshPromises.set(key, request);
        return request;
    },

    syncResults() {
        if (appState.resultsSyncPromise) return appState.resultsSyncPromise;
        appState.resultsSyncPromise = utils.fetchJson(CONFIG.endpoints.syncResults, {
            method: 'POST', timeoutMs: 0
        }).catch(error => {
            console.warn('Official results sync unavailable:', error);
        }).finally(() => {
            appState.resultsSyncPromise = null;
        });
        return appState.resultsSyncPromise;
    },

    async loadTeamRating(entryId, gameweek, force = false) {
        const key = `${entryId}:${gameweek}`;
        const saved = appState.ratingCache.get(key);
        // Saved before the deadline with transfers open, a rating is out of
        // date once it passes: the server now answers with them closed.
        const expired = saved?.transfers_open && utils.deadlinePassed(gameweek);
        if (saved && !force && !expired) return saved;
        const rating = await utils.fetchJson(
            CONFIG.endpoints.teamRating(entryId, gameweek), CONFIG.inferenceRequest
        );
        appState.ratingCache.set(key, rating);
        return rating;
    },

    // Fixtures carry each club's official short name under the official club
    // name; players carry the app's canonical club name, so both are mapped.
    learnTeamCodes(fixtures, players) {
        const codeById = new Map();
        fixtures.forEach(fixture => [fixture.home_team, fixture.away_team].forEach(team => {
            if (!team?.short_name) return;
            codeById.set(Number(team.id), team.short_name);
            if (team.name) appState.teamCodes.set(String(team.name).toLowerCase(), team.short_name);
        }));
        players.forEach(player => {
            const code = codeById.get(Number(player.team_id));
            if (code && player.team_name) appState.teamCodes.set(String(player.team_name).toLowerCase(), code);
        });
    },

    playerFixture(player, fixtures) {
        const teamId = Number(player.team_id);
        const fixture = fixtures.find(item =>
            Number(item.home_team?.id) === teamId || Number(item.away_team?.id) === teamId
        );
        if (!fixture) return null;
        const home = Number(fixture.home_team?.id) === teamId;
        return {
            id: fixture.id,
            kickoff_time: fixture.kickoff_time,
            difficulty: home ? fixture.home_team?.difficulty : fixture.away_team?.difficulty
        };
    }
};

const playerRenderer = {
    format(player) {
        return {
            ...player,
            expected_points: utils.number(player.expected_points, 0),
            web_name: player.web_name || 'Unknown',
            team_name: player.team_name || 'Unknown',
            opponent_team_name: player.opponent_team_name || 'TBC',
            was_home: typeof player.was_home === 'boolean' ? player.was_home : null,
            role: player.role || '',
            position_code: utils.positionCode(player),
            status_info: utils.statusInfo(player.status, player.can_select !== false)
        };
    },

    // Cards and rows carry the player's FPL id; the player itself stays in
    // memory, instead of ~2KB of JSON per card in the page. A card and its
    // list row share one entry, so sorting the list adds none.
    dataAttribute(player) {
        const key = player.id !== null && player.id !== undefined
            ? String(player.id)
            : `name:${player.web_name}|${player.team_name}`;
        appState.shownPlayers.set(key, this.format(player));
        return utils.escapeHtml(key);
    },

    roleBadge(player) {
        if (player.role === 'captain') return '<span class="role-badge captain" aria-label="Captain">C</span>';
        if (player.role === 'vice') return '<span class="role-badge vice" aria-label="Vice captain">VC</span>';
        return '';
    },

    card(rawPlayer) {
        const player = this.format(rawPlayer);
        const name = utils.escapeHtml(player.web_name);
        const team = utils.escapeHtml(player.team_name);
        const opponent = utils.escapeHtml(player.opponent_team_name);
        const teamCode = utils.escapeHtml(utils.teamCode(player.team_name));
        const opponentCode = utils.escapeHtml(utils.opponentCode(player.opponent_team_name));
        const venue = utils.venueCode(player);
        const roleClass = player.role ? ` ${utils.escapeHtml(player.role)}` : '';
        const availability = player.status_info.className !== 'available'
            ? `<span class="card-alert ${player.status_info.className}" title="${player.status_info.label}">!</span>`
            : '';

        return `
            <button class="player-card${roleClass}" type="button"
                data-player="${this.dataAttribute(player)}"
                aria-label="${name}, ${player.expected_points.toFixed(2)} expected points (xPts)">
                ${this.roleBadge(player)}${availability}
                <div class="card-position">${player.position_code}</div>
                <div class="player-name">${name}</div>
                <div class="team-name"><span class="label-full">${team}</span><span class="label-code">${teamCode}</span></div>
                <div class="fixture">
                    ${venue ? `<b>${venue}</b>` : ''}
                    <span class="label-full">${opponent}</span><span class="label-code">${opponentCode}</span>
                </div>
                <div class="card-bottom">
                    <span class="card-price">${utils.money(player.price)}</span>
                    <span class="expected-points">${player.expected_points.toFixed(2)}</span>
                </div>
            </button>`;
    },

    tableRow(rawPlayer, index) {
        const player = this.format(rawPlayer);
        const status = player.status_info;
        const venue = utils.venueCode(player);
        const role = player.role
            ? `<span class="table-role ${player.role}">${player.role === 'captain' ? 'C' : 'VC'}</span>`
            : '';
        const bench = utils.isBench(player) ? '<span class="table-role bench">SUB</span>' : '';
        // On a phone the list drops to player, fixture and xPts, so position,
        // club code and any availability flag ride under the name instead.
        const flag = status.className !== 'available'
            ? ` · <b class="table-flag ${status.className}">${status.label}</b>`
            : '';
        const teamCode = utils.escapeHtml(utils.teamCode(player.team_name));
        return `
            <tr class="squad-row" tabindex="0" data-player="${this.dataAttribute(player)}">
                <td>
                    <div class="table-player">
                        <span class="table-rank">${String(index + 1).padStart(2, '0')}</span>
                        <span><strong>${utils.escapeHtml(player.web_name)} ${role}${bench}</strong><small><span class="label-full">${utils.escapeHtml(player.team_name)}</span><span class="label-code">${player.position_code} · ${teamCode}${flag}</span></small></span>
                    </div>
                </td>
                <td><span class="position-pill">${player.position_code}</span></td>
                <td>${venue ? `<strong>${venue}</strong> · ` : ''}<span class="label-full">${utils.escapeHtml(player.opponent_team_name)}</span><span class="label-code">${utils.escapeHtml(utils.opponentCode(player.opponent_team_name))}</span></td>
                <td>${utils.money(player.price)}</td>
                <td>${utils.percentage(player.selected_by_percent)}</td>
                <td><span class="status-pill ${status.className}"><i></i>${status.label}</span></td>
                <td><strong class="table-xpts">${player.expected_points.toFixed(2)}</strong></td>
            </tr>`;
    }
};

const dashboardRenderer = {
    render(data) {
        this.header(data);
        this.pulse(data);
        this.statistics(data.scout_team);
        this.fixtures(data.fixtures, data.fixturesAvailable, data.fixturesLoading);
        this.squad(data.scout_team);
        dom.signalState.textContent = 'Projections ready';
        dom.sourceState.textContent = 'Official data';
        if (data.credits) {
            // The footer's author link already credits the author, so show only
            // the app and data-source parts of the API credits string.
            const parts = data.credits.split(' | ');
            dom.credits.textContent = (parts.length > 2 ? parts.slice(0, -1) : parts).join(' · ');
        }
    },

    header(data) {
        const event = data.event;
        dom.gameweekInfo.textContent = `Gameweek ${data.gameweek}`;
        dom.modelVersion.textContent = data.version ? `GW ${data.gameweek} · Model v${data.version}` : '';
        dom.modelVersion.hidden = !data.version;
        dom.deadlineDate.textContent = event?.deadline_time
            ? utils.deadline(event.deadline_time)
            : 'Official deadline TBC';
        dom.eventState.textContent = utils.eventLabel(event);
        this.startCountdown(event?.deadline_time);
    },

    startCountdown(deadline) {
        clearInterval(appState.countdownTimer);
        const update = () => {
            dom.deadlineCountdown.textContent = utils.countdown(deadline);
            // Suggestions on screen close once their deadline passes.
            const rating = appState.ratingData;
            if (rating && !dom.ratingTransfers.hidden && utils.deadlinePassed(rating.gameweek)) {
                dom.ratingTransfers.hidden = true;
            }
        };
        update();
        appState.countdownTimer = setInterval(update, 30000);
    },

    pulse(data) {
        const event = data.event;
        dom.pulseDeadline.textContent = event?.deadline_time
            ? utils.formatDate(event.deadline_time, { day: '2-digit', month: 'short' })
            : 'TBC';
        dom.pulseDeadlineSub.textContent = event?.deadline_time
            ? utils.formatDate(event.deadline_time, { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })
            : 'Official deadline time';
        dom.fixtureCount.textContent = data.fixturesAvailable === false ? '—' : String(data.fixtures.length);
        dom.dataState.textContent = event?.data_checked ? 'Checked' : utils.eventLabel(event);
        dom.dataStateSub.textContent = event?.finished
            ? 'Official scores finalised'
            : event?.data_checked ? 'Official data checked' : 'Live changes possible';
        dom.lastSync.textContent = utils.formatDate(data.syncedAt, {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    },

    statistics(players) {
        const total = utils.bestLineupPoints(players)
            ?? players.reduce((sum, player) => sum + utils.number(player.expected_points, 0), 0);
        const value = players.reduce((sum, player) => sum + utils.number(player.price, 0), 0);
        const captain = players.find(player => player.role === 'captain');
        const differentialCount = players.filter(player => {
            const ownership = utils.number(player.selected_by_percent);
            return ownership !== null && ownership < 10;
        }).length;

        dom.totalPoints.textContent = total.toFixed(2);
        dom.captainName.textContent = captain?.web_name || '—';
        dom.captainPoints.textContent = captain
            ? `${utils.number(captain.expected_points, 0).toFixed(2)} xPts`
            : '— xPts';
        dom.squadValue.textContent = value ? `£${value.toFixed(1)}m` : '—';
        dom.differentialCount.textContent = String(differentialCount);
    },

    fixtures(fixtures, available = true, loading = false) {
        if (loading) {
            dom.fixtureRail.innerHTML = '<div class="rail-loading">Loading Premier League fixtures…</div>';
            return;
        }
        if (!available) {
            dom.fixtureRail.innerHTML = '<div class="rail-loading">Fixtures temporarily unavailable. Squad projections are ready.</div>';
            return;
        }
        if (!fixtures.length) {
            dom.fixtureRail.innerHTML = '<div class="rail-loading">No Premier League fixtures have been published for this Gameweek.</div>';
            return;
        }
        dom.fixtureRail.innerHTML = fixtures.map(fixture => {
            const home = fixture.home_team || {};
            const away = fixture.away_team || {};
            const hasScore = fixture.started || fixture.finished;
            return `
                <article class="fixture-card">
                    <div class="fixture-card-top">
                        <span>${utils.kickoff(fixture.kickoff_time)}</span>
                        <span>${fixture.finished ? 'FT' : fixture.started ? `${fixture.minutes || 0}′` : `GW${fixture.gameweek}`}</span>
                    </div>
                    <div class="fixture-team">
                        <span><strong>${utils.escapeHtml(home.short_name || home.name)}</strong><small>${utils.escapeHtml(home.name)}</small></span>
                        <span class="fdr fdr-${home.difficulty || 0}">FDR ${home.difficulty || '—'}</span>
                    </div>
                    <div class="fixture-score">${hasScore ? `${home.score ?? 0} — ${away.score ?? 0}` : 'v'}</div>
                    <div class="fixture-team">
                        <span><strong>${utils.escapeHtml(away.short_name || away.name)}</strong><small>${utils.escapeHtml(away.name)}</small></span>
                        <span class="fdr fdr-${away.difficulty || 0}">FDR ${away.difficulty || '—'}</span>
                    </div>
                </article>`;
        }).join('');
    },

    squad(players, options = {}) {
        if (!Array.isArray(players) || !players.length) {
            throw new Error('The AI engine returned no squad players');
        }
        // A manager's published squad has a starting XI and an ordered bench;
        // the scout squad has neither, so all 15 picks take the pitch.
        const hasLineup = players.every(player => utils.number(player.pick_position, 0) >= 1);
        const starters = hasLineup ? players.filter(player => !utils.isBench(player)) : players;
        const bench = hasLineup
            ? players.filter(player => utils.isBench(player))
                .sort((a, b) => utils.number(a.pick_position, 0) - utils.number(b.pick_position, 0))
            : [];
        appState.shownPlayers = new Map();
        const positionOrder = ['Goalkeeper', 'Defender', 'Midfielder', 'Forward'];
        const grouped = Object.fromEntries(positionOrder.map(position => [position, []]));
        starters.forEach(player => {
            const position = player.position || player.element_type;
            if (grouped[position]) grouped[position].push(player);
        });
        Object.values(grouped).forEach(group => group.sort(
            (a, b) => utils.number(b.expected_points, 0) - utils.number(a.expected_points, 0)
        ));

        const lines = positionOrder.map(position => {
            const group = grouped[position];
            if (!group.length) return '';
            const positionClass = position.toLowerCase();
            return `
                <div class="position-label position-${positionClass}">${position}</div>
                <div class="formation-line formation-${positionClass}">${group.map(player => playerRenderer.card(player)).join('')}</div>`;
        }).join('');
        const benchLine = bench.length ? `
            <div class="pitch-bench">
                <div class="position-label position-bench">Bench</div>
                <div class="formation-line formation-bench">${bench.map(player => playerRenderer.card(player)).join('')}</div>
            </div>` : '';
        dom.pitch.innerHTML = lines + benchLine;
        dom.pitch.classList.remove('fade-in');
        requestAnimationFrame(() => dom.pitch.classList.add('fade-in'));
        this.fitNames();

        // Each row keeps its xPts rank, whichever column the list is sorted by.
        appState.tableRows = [...players]
            .sort((a, b) => utils.number(b.expected_points, 0) - utils.number(a.expected_points, 0))
            .map((player, index) => ({ player, rank: index }));
        this.table();
        const counts = positionOrder.map(position => grouped[position].length);
        if (hasLineup) {
            dom.formationChip.textContent = counts.slice(1).join('-');
            dom.formationChip.title = 'Starting formation: defenders-midfielders-forwards';
        } else {
            dom.formationChip.textContent = counts.join(' • ');
            dom.formationChip.title = 'Squad by position: goalkeepers • defenders • midfielders • forwards';
        }
        dom.squadSubtitle.textContent = options.subtitle
            || `${players.length} picks · tap a player for xPts, price and ownership`;
    },

    table() {
        const { key, direction } = appState.tableSort;
        const value = tableSorting.columns[key];
        const sign = direction === 'asc' ? 1 : -1;
        const rows = [...appState.tableRows].sort((a, b) => {
            const left = value(a.player);
            const right = value(b.player);
            // Missing values sit last in either direction; ties keep xPts order.
            if (left === null || right === null) {
                return (left === null) - (right === null) || a.rank - b.rank;
            }
            const order = typeof left === 'string' ? left.localeCompare(right) : left - right;
            return sign * order || a.rank - b.rank;
        });
        dom.tableBody.innerHTML = rows.map(({ player, rank }) =>
            playerRenderer.tableRow(player, rank)
        ).join('');
        document.querySelectorAll('[data-sort]').forEach(button => {
            const active = button.dataset.sort === key;
            button.closest('th')?.setAttribute('aria-sort',
                active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none');
        });
    },

    // Long names (Alexander-Arnold, Van de Ven) first wrap at a space or
    // hyphen, then shrink to fit their card, before falling back to an
    // ellipsis. A Range measures the text, since scrollWidth rounds to whole
    // pixels and misses centred text that overflows to the left.
    fitNames() {
        const range = document.createRange();
        const textWidth = name => {
            range.selectNodeContents(name);
            return range.getBoundingClientRect().width;
        };
        dom.pitch.querySelectorAll('.player-name').forEach(name => {
            name.classList.remove('wrapped');
            name.style.removeProperty('--name-fit');
            const room = name.clientWidth;
            if (!room) return;
            let ratio = room / textWidth(name);
            if (ratio >= 1) return;
            if (ratio < 0.86 && /[\s-]/.test(name.textContent.trim())) {
                name.classList.add('wrapped');
                ratio = room / textWidth(name);
            }
            if (ratio < 1) name.style.setProperty('--name-fit', Math.max(0.72, ratio * 0.97).toFixed(3));
        });
    },

    // Faint cards hold the squad's 2-5-5-3 shape while it loads, so the page
    // does not jump when the real cards arrive.
    loading(message = 'Projecting the Gameweek…') {
        dom.modelVersion.hidden = true;
        const placeholders = [2, 5, 5, 3].map(count => `
            <div class="formation-line placeholder-line" aria-hidden="true">${
                '<span class="player-card placeholder"></span>'.repeat(count)
            }</div>`).join('');
        dom.pitch.innerHTML = `
            <div class="loading over-placeholders" role="status" aria-live="polite">
                <span class="loading-ball" aria-hidden="true"></span>
                ${utils.escapeHtml(message)}
            </div>${placeholders}`;
        dom.fixtureRail.innerHTML = '<div class="rail-loading">Syncing Premier League fixtures…</div>';
        dom.signalState.textContent = 'Computing';
        dom.sourceState.textContent = 'Syncing';
    },

    error(message) {
        dom.modelVersion.hidden = true;
        dom.pitch.innerHTML = `
            <div class="error" role="alert">
                <strong>Scout temporarily unavailable</strong>
                <span>${utils.escapeHtml(message)}</span>
                <small>Official FPL may be updating. Try again in a moment.</small>
                <button class="retry-button" type="button" data-retry>Try again</button>
            </div>`;
        dom.fixtureRail.innerHTML = '<div class="rail-loading error-inline">Fixture sync paused.</div>';
        dom.signalState.textContent = 'Retry needed';
        dom.sourceState.textContent = 'Sync paused';
        // Nothing loaded, so nothing on screen may still describe a squad:
        // clear the heading, the list and the summary, and show the pitch,
        // where the error and its retry live.
        dom.formationChip.textContent = '—';
        dom.formationChip.removeAttribute('title');
        dom.squadSubtitle.textContent = 'The AI squad could not load for this Gameweek';
        dom.tableBody.innerHTML = '';
        dom.totalPoints.textContent = '—';
        dom.captainName.textContent = '—';
        dom.captainPoints.textContent = '— xPts';
        dom.squadValue.textContent = '—';
        dom.differentialCount.textContent = '—';
        interactions.switchView('pitch');
    }
};

// The List view's sortable columns. Names and positions start A–Z and GK–FWD;
// numbers start highest first.
const tableSorting = {
    columns: {
        player: player => String(player.web_name || '').toLocaleLowerCase('en-GB'),
        position: player => ({ GK: 1, DEF: 2, MID: 3, FWD: 4 })[utils.positionCode(player)] ?? null,
        price: player => tableSorting.known(player.price),
        ownership: player => tableSorting.known(player.selected_by_percent),
        xpts: player => utils.number(player.expected_points, 0)
    },
    ascendingFirst: new Set(['player', 'position']),

    // Number(null) is 0, so a missing price must not sort as £0.0m.
    known(value) {
        return value === null || value === undefined || value === '' ? null : utils.number(value);
    },

    sortBy(key) {
        if (!this.columns[key]) return;
        const current = appState.tableSort;
        const direction = current.key === key
            ? (current.direction === 'asc' ? 'desc' : 'asc')
            : (this.ascendingFirst.has(key) ? 'asc' : 'desc');
        appState.tableSort = { key, direction };
        dashboardRenderer.table();
    }
};

const gameweekManager = {
    // Open on the current Gameweek while its matches are still in play. Once
    // FPL marks it finished, open on the next deadline, as the API's default
    // squad does (OfficialFPLClient.next_gameweek), so a break between
    // Gameweeks never opens on last week's squad. After the season's last
    // deadline, the current Gameweek. A finished current Gameweek stays one
    // step back in the planning window.
    openingGameweek(events) {
        return events.find(event => event.is_current && !event.finished)
            || events.find(event => event.is_next)
            || events.find(event => event.is_current)
            || events.find(event => !event.finished)
            || events.at(-1);
    },

    planningWindow(events) {
        const anchor = events.find(event => event.is_current)
            || events.find(event => event.is_next)
            || events.find(event => !event.finished)
            || events.at(-1);
        if (!anchor) return [];
        const anchorIndex = events.findIndex(event => Number(event.id) === Number(anchor.id));
        return events.slice(Math.max(anchorIndex, 0), Math.max(anchorIndex, 0) + 3);
    },

    populate(events) {
        const visibleEvents = this.planningWindow(events);
        appState.visibleEvents = visibleEvents;
        dom.gameweekSelect.innerHTML = '';
        visibleEvents.forEach(event => {
            const option = document.createElement('option');
            option.value = event.id;
            const state = event.finished ? 'FT' : event.is_current ? 'Live' : event.is_next ? 'Next' : 'Plan';
            option.textContent = `GW ${event.id} · ${state}`;
            option.selected = Number(event.id) === Number(appState.currentGameweek);
            dom.gameweekSelect.appendChild(option);
        });
        dom.gameweekWindow.innerHTML = visibleEvents.map(event => {
            const active = Number(event.id) === Number(appState.currentGameweek);
            const state = event.finished ? 'FT'
                : event.is_current ? 'Live' : event.is_next ? 'Next' : 'Plan';
            return `
                <button class="gameweek-option${active ? ' active' : ''}" type="button"
                    data-gameweek="${Number(event.id)}" aria-pressed="${active}">
                    <span>GW</span><strong>${Number(event.id)}</strong><small>${state}</small>
                </button>`;
        }).join('');
    },

    setActive(gameweek) {
        dom.gameweekSelect.value = String(gameweek);
        document.querySelectorAll('.gameweek-option').forEach(button => {
            const active = Number(button.dataset.gameweek) === Number(gameweek);
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });
    },

    setLocked(locked) {
        appState.isLoading = locked;
        dom.gameweekSelect.disabled = locked;
        document.querySelectorAll('.gameweek-option').forEach(button => { button.disabled = locked; });
    },

    // keepSaved leaves a saved squad of this Gameweek on screen, instead of
    // the loading state, until the current one replaces it.
    async load(gameweek, force = false, { keepSaved = false } = {}) {
        if (appState.isLoading) return;
        const token = ++appState.loadToken;
        const isCurrent = () => token === appState.loadToken;
        this.setLocked(true);
        if (!keepSaved) dashboardRenderer.loading(`Running local AI for Gameweek ${gameweek}…`);
        try {
            let resultsSyncStarted = false;
            let refreshStarted = false;
            const publish = data => {
                // A newer load owns the screen; this one only fills the cache.
                if (!isCurrent()) return;
                appState.currentData = data;
                appState.currentGameweek = Number(gameweek);
                this.setActive(gameweek);
                dashboardRenderer.render(data);
                shareLink.write();
                // Keep collection in its own request, after showing the squad.
                if (data.frozen && !resultsSyncStarted) {
                    resultsSyncStarted = true;
                    dataLoader.syncResults();
                }
                if (data.refresh_due && !refreshStarted) {
                    refreshStarted = true;
                    dataLoader.refreshPredictions(gameweek);
                }
                // The squad is on screen. Player metadata and fixtures may
                // still be loading, but the Gameweek can already be changed
                // or refreshed.
                this.setLocked(false);
            };
            const data = await dataLoader.loadDashboard(gameweek, force, publish);
            if (!isCurrent()) return;
            publish(data);
            if (Number(gameweek) === appState.openingGameweek) savedSquad.save(data);
            if (appState.squadMode === 'rating' && appState.ratingEntryId) {
                await teamRating.rate(appState.ratingEntryId, force);
            }
        } catch (error) {
            if (!isCurrent()) return;
            console.error('Dashboard load failed:', error);
            // The requested Gameweek stays selected though its squad failed,
            // so Try again and Refresh reload it, and the Gameweek shown
            // before can be picked again.
            appState.currentGameweek = Number(gameweek);
            appState.currentData = null;
            this.setActive(gameweek);
            dashboardRenderer.header({
                gameweek,
                event: appState.events.find(event => Number(event.id) === Number(gameweek)) || null
            });
            dashboardRenderer.error(error.message);
        } finally {
            if (isCurrent()) this.setLocked(false);
        }
    }
};

const teamRating = {
    // focus moves to the team ID field; a shared link opens without it, so a
    // phone does not raise its keyboard over the result.
    setMode(mode, { focus = true } = {}) {
        appState.squadMode = mode;
        const ratingMode = mode === 'rating';
        dom.teamRatingPanel.hidden = !ratingMode;
        document.querySelectorAll('.squad-mode-button').forEach(button => {
            const active = button.dataset.squadMode === mode;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });

        if (!ratingMode) {
            dom.teamRatingError.hidden = true;
            dom.squadDisplay.hidden = false;
            dom.squadTools.hidden = false;
            if (appState.currentData?.scout_team) {
                dashboardRenderer.squad(appState.currentData.scout_team, {
                    subtitle: '15 AI picks ranked by xPts for the selected Gameweek'
                });
            }
            shareLink.write();
            return;
        }

        if (appState.ratingData
            && Number(appState.ratingData.gameweek) === Number(appState.currentGameweek)) {
            this.render(appState.ratingData);
        } else {
            dom.teamRatingResult.hidden = true;
            dom.squadDisplay.hidden = true;
            dom.squadTools.hidden = true;
        }
        if (focus) requestAnimationFrame(() => dom.teamIdInput.focus());
    },

    loading() {
        appState.isRatingLoading = true;
        dom.rateTeamButton.disabled = true;
        dom.refreshButton.disabled = true;
        dom.gameweekSelect.disabled = true;
        document.querySelectorAll('.gameweek-option').forEach(button => { button.disabled = true; });
        dom.rateTeamButton.textContent = 'Scoring…';
        dom.teamRatingError.hidden = true;
        dom.teamRatingResult.hidden = true;
        dom.squadDisplay.hidden = true;
        dom.squadTools.hidden = true;
    },

    render(data) {
        appState.ratingData = data;
        const score = utils.number(data.rating, 0);
        dom.ratingScore.textContent = String(Math.round(score));
        dom.ratingScoreRing.style.setProperty('--rating-score', `${Math.max(0, Math.min(100, score)) * 3.6}deg`);
        dom.ratingGrade.textContent = data.grade || '—';
        dom.ratingTeamName.textContent = data.team_name || `Team ${data.entry_id}`;
        dom.ratingManagerName.textContent = data.manager_name || 'FPL manager';
        dom.ratingPicksLabel.textContent = Number(data.picks_gameweek) === Number(data.gameweek)
            ? `Published Gameweek ${data.picks_gameweek} squad`
            : `Gameweek ${data.picks_gameweek} squad · scored for Gameweek ${data.gameweek}`;
        dom.ratingProjection.textContent = `${utils.number(data.projected_points, 0).toFixed(2)} xPts`;
        dom.ratingBenchmark.textContent = `${utils.number(data.ai_projected_points, 0).toFixed(2)} xPts`;
        dom.ratingGap.textContent = `${utils.number(data.projected_gap, 0).toFixed(2)} xPts`;
        dom.ratingStrengths.innerHTML = (data.strengths || [])
            .map(item => `<li>${utils.escapeHtml(item)}</li>`).join('');
        dom.ratingRisks.innerHTML = (data.risks || [])
            .map(item => `<li>${utils.escapeHtml(item)}</li>`).join('');
        this.transfers(data);
        // Also when a rating already loaded is shown again (AI squad → Rate
        // your team): the address bar names the team on screen.
        shareLink.write();
        dom.teamRatingResult.hidden = false;
        dom.squadDisplay.hidden = false;
        dom.squadTools.hidden = false;
        dashboardRenderer.squad(data.squad || [], {
            subtitle: `${data.team_name || 'Your team'} · AI score ${Math.round(score)}/100`
        });
    },

    // Suggestions exist only before the deadline and with a published bank.
    transfers(data) {
        // Number(null) is 0, so a missing bank must not read as £0.0m.
        const bank = data.bank === null || data.bank === undefined ? null : utils.number(data.bank);
        // transfers_open is the server's answer when the rating loaded; the
        // deadline is checked again here, as a page can stay open past it.
        dom.ratingTransfers.hidden = !data.transfers_open || bank === null
            || utils.deadlinePassed(data.gameweek);
        if (dom.ratingTransfers.hidden) return;
        dom.ratingBank.textContent = `${utils.money(bank)} in the bank at the last deadline`;
        // A real-life transfer can leave more than three players from a club;
        // FPL then only accepts a transfer that sells one of them.
        const overLimit = Object.entries(data.clubs_over_limit || {});
        dom.ratingClubLimit.hidden = !overLimit.length;
        dom.ratingClubLimit.textContent = overLimit.length
            ? `Your squad has ${overLimit.map(([club, count]) => `${count} ${club}`).join(' and ')} players. `
                + 'FPL requires your next transfer to sell one of them, so only those sales are suggested.'
            : '';
        const transfers = Array.isArray(data.transfers) ? data.transfers : [];
        if (!transfers.length) {
            dom.ratingTransferList.innerHTML = overLimit.length
                ? '<li class="transfer-empty">No affordable sale of one of those players raises this squad\'s projection.</li>'
                : '<li class="transfer-empty">No affordable single transfer raises this squad\'s projection.</li>';
            return;
        }
        const player = (item, direction) => `
            <span class="transfer-player ${direction}">
                <small>${direction === 'out' ? 'Out' : 'In'}</small>
                <strong>${utils.escapeHtml(item.web_name)}</strong>
                <span>${utils.escapeHtml(utils.teamCode(item.team_name))} · ${utils.money(item.price)} · ${utils.number(item.expected_points, 0).toFixed(2)} xPts</span>
            </span>`;
        dom.ratingTransferList.innerHTML = transfers.map(item => `
            <li>
                ${player(item.out || {}, 'out')}
                <span class="transfer-arrow" aria-hidden="true">→</span>
                ${player(item.in || {}, 'in')}
                <strong class="transfer-gain">+${utils.number(item.gain, 0).toFixed(2)} xPts</strong>
            </li>`).join('');
    },

    error(message) {
        dom.teamRatingError.textContent = message;
        dom.teamRatingError.hidden = false;
        dom.teamRatingResult.hidden = true;
        dom.squadDisplay.hidden = true;
        dom.squadTools.hidden = true;
    },

    async rate(entryId, force = false) {
        if (appState.isRatingLoading) return;
        appState.ratingEntryId = Number(entryId);
        this.loading();
        try {
            const data = await dataLoader.loadTeamRating(
                appState.ratingEntryId, appState.currentGameweek, force
            );
            this.render(data);
            recentTeams.remember(data);
            shareLink.write();
        } catch (error) {
            console.error('Team rating failed:', error);
            this.error(error.message);
        } finally {
            appState.isRatingLoading = false;
            dom.rateTeamButton.disabled = false;
            dom.rateTeamButton.textContent = 'Rate my team';
            if (!appState.isLoading) {
                dom.refreshButton.disabled = false;
                dom.gameweekSelect.disabled = false;
                document.querySelectorAll('.gameweek-option').forEach(button => { button.disabled = false; });
            }
        }
    }
};

// The Gameweek and rated team live in the address bar (?gw=12&team=123456), so
// a view can be bookmarked or shared. The opening Gameweek is left out, so a
// plain link always opens the newest one. replaceState keeps Back for leaving.
const shareLink = {
    read() {
        try {
            const params = new URLSearchParams(window.location.search);
            const id = name => {
                const value = params.get(name) || '';
                return /^\d{1,9}$/.test(value) && Number(value) > 0 ? Number(value) : null;
            };
            return { gameweek: id('gw'), team: id('team') };
        } catch (error) {
            return { gameweek: null, team: null };
        }
    },

    write() {
        try {
            const url = new URL(window.location.href);
            const gameweek = Number(appState.currentGameweek);
            if (appState.openingGameweek && gameweek !== appState.openingGameweek) {
                url.searchParams.set('gw', String(gameweek));
            } else {
                url.searchParams.delete('gw');
            }
            const rated = appState.squadMode === 'rating' && appState.ratingData
                && Number(appState.ratingData.entry_id) === Number(appState.ratingEntryId);
            if (rated) url.searchParams.set('team', String(appState.ratingEntryId));
            else url.searchParams.delete('team');
            if (url.href !== window.location.href) window.history.replaceState(null, '', url);
        } catch (error) {
            // No address bar to update (an embedded or sandboxed page).
        }
    },

    // The link to a rating as shown: its team and, unless it is the opening
    // one, its Gameweek, whatever the address bar says.
    ratingUrl(rating) {
        const url = new URL(window.location.href);
        url.searchParams.set('team', String(Number(rating.entry_id)));
        const gameweek = Number(rating.gameweek);
        if (appState.openingGameweek && gameweek !== appState.openingGameweek) {
            url.searchParams.set('gw', String(gameweek));
        } else {
            url.searchParams.delete('gw');
        }
        return url.href;
    },

    // The phone's share sheet where there is one, otherwise the clipboard.
    async share(button, text, url = window.location.href) {
        const label = button.textContent;
        try {
            if (navigator.share) {
                await navigator.share({ title: document.title, text, url });
                return;
            }
            await navigator.clipboard.writeText(url);
            button.textContent = 'Link copied';
        } catch (error) {
            if (error?.name === 'AbortError') return;
            button.textContent = 'Copy the address bar link';
        }
        setTimeout(() => { button.textContent = label; }, 2000);
    }
};

// The opening Gameweek's last complete squad, kept on this device so a return
// visit shows it at once, marked as refreshing, while the current squad loads
// (a cold server can take many seconds). Storage can be blocked or full.
const savedSquad = {
    storageKey: 'openfpl.lastSquad',
    schema: 1,
    maxAgeMs: 12 * 60 * 60 * 1000,

    read() {
        try {
            const saved = JSON.parse(localStorage.getItem(this.storageKey) || 'null');
            const fresh = saved?.schema === this.schema
                && Date.now() - Number(saved.savedAt) < this.maxAgeMs;
            const data = saved?.data;
            if (!fresh || !Number.isInteger(data?.gameweek)) return null;
            if (!Array.isArray(data.scout_team) || !data.scout_team.length) return null;
            return data;
        } catch (error) {
            return null;
        }
    },

    // Only a complete squad: its fixtures and season data loaded.
    save(data) {
        if (data.fixturesLoading || data.fixturesAvailable === false) return;
        const { events, ...squad } = data;
        try {
            localStorage.setItem(this.storageKey, JSON.stringify({
                schema: this.schema, savedAt: Date.now(), data: squad
            }));
        } catch (error) {
            // Private browsing or blocked storage: the next visit loads as usual.
        }
    }
};

// Team IDs this browser has rated, newest first, offered as the ID field's
// suggestions. Only a successful rating is remembered, so a mistyped ID never
// is. Kept on this device alone; storage can be blocked or full.
const recentTeams = {
    storageKey: 'openfpl.recentTeams',
    limit: 5,

    read() {
        try {
            const saved = JSON.parse(localStorage.getItem(this.storageKey) || '[]');
            if (!Array.isArray(saved)) return [];
            return saved
                .filter(item => Number.isInteger(item?.id) && item.id > 0)
                .slice(0, this.limit);
        } catch (error) {
            return [];
        }
    },

    remember(rating) {
        const id = Number(rating?.entry_id);
        if (!Number.isInteger(id) || id < 1) return;
        const teams = [
            { id, team: String(rating.team_name || '') },
            ...this.read().filter(item => item.id !== id)
        ].slice(0, this.limit);
        try {
            localStorage.setItem(this.storageKey, JSON.stringify(teams));
        } catch (error) {
            // Private browsing or blocked storage: suggest it for this visit only.
        }
        this.render(teams);
    },

    render(teams = this.read()) {
        dom.teamIdSuggestions.innerHTML = teams.map(item => {
            const label = utils.escapeHtml(item.team || `FPL team ${item.id}`);
            return `<option value="${item.id}" label="${label}"></option>`;
        }).join('');
    }
};

const interactions = {
    showPlayer(target) {
        let player;
        try {
            player = appState.shownPlayers.get(target.dataset.player);
            if (!player) throw new Error(`No player for key ${target.dataset.player}`);
        } catch (error) {
            console.error('Could not open player briefing:', error);
            return;
        }
        const status = utils.statusInfo(player.status, player.can_select !== false);
        const role = player.role === 'captain' ? 'Captain'
            : player.role === 'vice' ? 'Vice captain'
            : utils.isBench(player) ? 'Bench' : 'Squad';

        dom.dialogPositionBadge.textContent = utils.positionCode(player);
        dom.dialogPlayerName.textContent = player.web_name;
        dom.dialogTeam.textContent = `${player.team_name} · ${status.label}`;
        dom.dialogPoints.textContent = utils.number(player.expected_points, 0).toFixed(2);
        const venue = utils.venueCode(player);
        dom.dialogFixture.textContent = venue
            ? `${venue} · ${player.opponent_team_name}`
            : player.opponent_team_name;
        dom.dialogVenue.textContent = venue === 'H' ? 'Home'
            : venue === 'A' ? 'Away'
            : player.fixture_count > 1 ? 'Home and away' : '—';
        dom.dialogPrice.textContent = utils.money(player.price);
        dom.dialogOwnership.textContent = utils.percentage(player.selected_by_percent);
        dom.dialogTotalPoints.textContent = player.total_points ?? '—';
        dom.dialogRole.textContent = role;
        dom.dialogNews.hidden = !player.news;
        dom.dialogNews.textContent = player.news || '';
        if (typeof dom.playerDialog.showModal === 'function') dom.playerDialog.showModal();
    },

    switchView(view) {
        appState.activeView = view;
        const pitchActive = view === 'pitch';
        dom.pitchView.hidden = !pitchActive;
        dom.tableView.hidden = pitchActive;
        dom.pitchView.classList.toggle('active', pitchActive);
        dom.tableView.classList.toggle('active', !pitchActive);
        document.querySelectorAll('.view-button').forEach(button => {
            const active = button.dataset.view === view;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });
    },

    // A hero button opens the squad section in its mode: the AI squad, or the
    // team-ID form (which takes focus). Already in that mode, nothing reloads.
    openSquadMode(mode) {
        if (appState.squadMode !== mode) teamRating.setMode(mode);
    },

    // Retry from the error panel. If the season list itself never loaded there
    // is no Gameweek to reload, so the app starts over.
    retry() {
        if (appState.isLoading || appState.isRatingLoading) return;
        if (!appState.events.length) return app.start();
        return this.refresh();
    },

    async refresh() {
        if (appState.isLoading || appState.isRatingLoading) return;
        dom.refreshButton.classList.add('refreshing');
        dom.refreshButton.disabled = true;
        appState.dashboardCache.delete(appState.currentGameweek);
        appState.eventsCache = null;
        try {
            await gameweekManager.load(appState.currentGameweek, true);
        } finally {
            dom.refreshButton.classList.remove('refreshing');
            dom.refreshButton.disabled = false;
        }
    },

    init() {
        if ('ResizeObserver' in window) {
            new ResizeObserver(([entry]) => {
                const width = Math.round(entry.contentRect.width);
                if (width === appState.pitchWidth) return;
                appState.pitchWidth = width;
                dashboardRenderer.fitNames();
            }).observe(dom.pitch);
        }
        recentTeams.render();
        dom.gameweekSelect.addEventListener('change', utils.debounce(event => {
            const gameweek = Number(event.target.value);
            if (gameweek && gameweek !== appState.currentGameweek) gameweekManager.load(gameweek);
        }, 180));
        dom.gameweekWindow.addEventListener('click', event => {
            const button = event.target.closest('.gameweek-option');
            if (!button || button.disabled) return;
            const gameweek = Number(button.dataset.gameweek);
            if (gameweek && gameweek !== appState.currentGameweek) gameweekManager.load(gameweek);
        });
        dom.refreshButton.addEventListener('click', () => this.refresh());
        document.querySelectorAll('.view-button').forEach(button => {
            button.addEventListener('click', () => this.switchView(button.dataset.view));
        });
        document.querySelectorAll('[data-sort]').forEach(button => {
            button.addEventListener('click', () => tableSorting.sortBy(button.dataset.sort));
        });
        document.querySelectorAll('.squad-mode-button').forEach(button => {
            button.addEventListener('click', () => teamRating.setMode(button.dataset.squadMode));
        });
        // The hero's two buttons pick the squad mode, then follow their #squad link.
        document.querySelectorAll('[data-squad-mode-link]').forEach(link => {
            link.addEventListener('click', () => this.openSquadMode(link.dataset.squadModeLink));
        });
        dom.shareRating.addEventListener('click', () => {
            const rating = appState.ratingData;
            if (!rating) return;
            shareLink.share(dom.shareRating,
                `${rating.team_name || 'My FPL team'} scores ${Math.round(utils.number(rating.rating, 0))}/100 `
                + `for Gameweek ${rating.gameweek} on OpenFPL Scout AI`,
                shareLink.ratingUrl(rating));
        });
        dom.teamRatingForm.addEventListener('submit', event => {
            event.preventDefault();
            const rawEntryId = dom.teamIdInput.value.trim();
            if (!/^\d+$/.test(rawEntryId) || Number(rawEntryId) < 1) {
                teamRating.error('Enter a valid numeric FPL team ID.');
                dom.teamIdInput.focus();
                return;
            }
            teamRating.rate(Number(rawEntryId));
        });
        document.addEventListener('click', event => {
            const target = event.target.closest('[data-player]');
            if (target) this.showPlayer(target);
        });
        dom.pitch.addEventListener('click', event => {
            if (event.target.closest('[data-retry]')) this.retry();
        });
        document.addEventListener('keydown', event => {
            const target = event.target.closest('.squad-row');
            if (target && (event.key === 'Enter' || event.key === ' ')) {
                event.preventDefault();
                this.showPlayer(target);
            }
            if (event.key === 'Escape' && dom.playerDialog.open) dom.playerDialog.close();
        });
        dom.dialogClose.addEventListener('click', () => dom.playerDialog.close());
        dom.playerDialog.addEventListener('click', event => {
            if (event.target === dom.playerDialog) dom.playerDialog.close();
        });
    }
};

const app = {
    init() {
        interactions.init();
        this.start();
    },

    async start() {
        const link = shareLink.read();
        const saved = savedSquad.read();
        if (saved) this.showSaved(saved);
        else dashboardRenderer.loading('Checking the official FPL season…');
        try {
            appState.events = await dataLoader.loadEvents();
            if (!appState.events.length) throw new Error('No official gameweeks are published yet');
            const active = gameweekManager.openingGameweek(appState.events);
            appState.openingGameweek = Number(active.id);
            // A shared Gameweek opens if the planner still offers it.
            const offered = gameweekManager.planningWindow(appState.events)
                .some(event => Number(event.id) === link.gameweek);
            appState.currentGameweek = offered ? link.gameweek : Number(active.id);
            gameweekManager.populate(appState.events);
            if (link.team) {
                // A shared rating: score that team once the squad loads.
                appState.ratingEntryId = link.team;
                dom.teamIdInput.value = String(link.team);
                teamRating.setMode('rating', { focus: false });
                document.getElementById('squad')?.scrollIntoView?.();
            }
            // A saved squad of another Gameweek gives way to the loading state.
            await gameweekManager.load(appState.currentGameweek, false, {
                keepSaved: Number(saved?.gameweek) === appState.currentGameweek
            });
        } catch (error) {
            console.error('Application initialization failed:', error);
            dashboardRenderer.error(error.message);
        }
    },

    showSaved(data) {
        appState.currentGameweek = Number(data.gameweek);
        appState.currentData = data;
        dashboardRenderer.render(data);
        dom.signalState.textContent = 'Refreshing';
        dom.sourceState.textContent = 'Saved squad';
    }
};

document.addEventListener('DOMContentLoaded', () => app.init());

window.FPLScoutApp = {
    app,
    appState,
    CONFIG,
    utils,
    dataLoader,
    dashboardRenderer,
    gameweekManager,
    teamRating,
    recentTeams,
    savedSquad,
    shareLink,
    tableSorting,
    interactions,
    clearCache: utils.clearCache
};

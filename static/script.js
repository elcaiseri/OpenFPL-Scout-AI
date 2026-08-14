/** OpenFPL Scout — official-data prediction dashboard. */

const CONFIG = {
    defaultGameweek: 1,
    cacheExpiry: 5 * 60 * 1000,
    endpoints: {
        scout: '/api/scout',
        events: '/api/fpl/gameweeks',
        players: '/api/fpl/players?limit=1000',
        eventStatus: '/api/fpl/gameweeks/status',
        fixtures: gameweek => `/api/fpl/fixtures?gameweek=${gameweek}`
    }
};

const appState = {
    currentGameweek: CONFIG.defaultGameweek,
    events: [],
    visibleEvents: [],
    currentData: null,
    isLoading: false,
    activeView: 'pitch',
    dashboardCache: new Map(),
    referenceCache: null,
    referenceTimestamp: 0,
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

    countdown(value) {
        if (!value) return 'Deadline TBC';
        const distance = new Date(value).getTime() - Date.now();
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

    async fetchJson(url) {
        const response = await fetch(url, { headers: { Accept: 'application/json' } });
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            throw new Error(`Invalid response from ${url}`);
        }
        if (!response.ok) {
            throw new Error(payload.detail || `Request failed (${response.status})`);
        }
        return payload;
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
        appState.referenceCache = null;
        appState.referenceTimestamp = 0;
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
            captainPoints: 'captain-points', squadValue: 'squad-value',
            differentialCount: 'differential-count', fixtureRail: 'fixture-rail',
            squadSubtitle: 'squad-subtitle', formationChip: 'formation-chip',
            pitchView: 'pitch-view', tableView: 'table-view', tableBody: 'squad-table-body',
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
    async loadReferenceData(force = false) {
        const fresh = appState.referenceCache
            && Date.now() - appState.referenceTimestamp < CONFIG.cacheExpiry;
        if (fresh && !force) return appState.referenceCache;

        const [eventsPayload, playersPayload, statusPayload] = await Promise.all([
            utils.fetchJson(CONFIG.endpoints.events),
            utils.fetchJson(CONFIG.endpoints.players),
            utils.fetchJson(CONFIG.endpoints.eventStatus)
        ]);
        const reference = {
            events: eventsPayload.results || [],
            players: playersPayload.results || [],
            eventStatus: statusPayload.data || {}
        };
        appState.referenceCache = reference;
        appState.referenceTimestamp = Date.now();
        return reference;
    },

    async loadDashboard(gameweek, force = false) {
        if (appState.dashboardCache.has(gameweek) && !force) {
            return appState.dashboardCache.get(gameweek);
        }

        const [scout, reference, fixturePayload] = await Promise.all([
            utils.fetchJson(`${CONFIG.endpoints.scout}?gameweek=${gameweek}`),
            this.loadReferenceData(force),
            utils.fetchJson(CONFIG.endpoints.fixtures(gameweek))
        ]);

        const byId = new Map(reference.players.map(player => [Number(player.id), player]));
        const byIdentity = new Map(reference.players.map(player => [
            `${player.web_name}|${player.team_name}`.toLowerCase(), player
        ]));

        const players = (scout.scout_team || []).map(player => {
            const identity = `${player.web_name}|${player.team_name}`.toLowerCase();
            const official = byId.get(Number(player.id)) || byIdentity.get(identity) || {};
            const fixture = this.playerFixture(official, fixturePayload.results || []);
            return {
                ...official,
                ...player,
                position: official.position || player.element_type,
                official_element_type: official.element_type,
                fixture_id: fixture?.id || null,
                difficulty: fixture?.difficulty || null,
                kickoff_time: fixture?.kickoff_time || null
            };
        });

        const dashboard = {
            ...scout,
            scout_team: players,
            event: reference.events.find(event => Number(event.id) === Number(gameweek)) || null,
            events: reference.events,
            eventStatus: reference.eventStatus,
            fixtures: fixturePayload.results || [],
            syncedAt: new Date()
        };
        appState.dashboardCache.set(gameweek, dashboard);
        return dashboard;
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
            was_home: Boolean(player.was_home),
            role: player.role || '',
            position_code: utils.positionCode(player),
            status_info: utils.statusInfo(player.status, player.can_select !== false)
        };
    },

    dataAttribute(player) {
        return utils.escapeHtml(JSON.stringify(this.format(player)));
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
        const roleClass = player.role ? ` ${utils.escapeHtml(player.role)}` : '';
        const availability = player.status_info.className !== 'available'
            ? `<span class="card-alert ${player.status_info.className}" title="${player.status_info.label}">!</span>`
            : '';

        return `
            <button class="player-card${roleClass}" type="button"
                data-player="${this.dataAttribute(player)}"
                aria-label="${name}, ${player.expected_points.toFixed(2)} expected points">
                ${this.roleBadge(player)}${availability}
                <div class="card-position">${player.position_code}</div>
                <div class="player-name">${name}</div>
                <div class="team-name">${team}</div>
                <div class="fixture">
                    <b>${player.was_home ? 'H' : 'A'}</b>
                    <span>${opponent}</span>
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
        const role = player.role
            ? `<span class="table-role ${player.role}">${player.role === 'captain' ? 'C' : 'VC'}</span>`
            : '';
        return `
            <tr class="squad-row" tabindex="0" data-player="${this.dataAttribute(player)}">
                <td>
                    <div class="table-player">
                        <span class="table-rank">${String(index + 1).padStart(2, '0')}</span>
                        <span><strong>${utils.escapeHtml(player.web_name)} ${role}</strong><small>${utils.escapeHtml(player.team_name)}</small></span>
                    </div>
                </td>
                <td><span class="position-pill">${player.position_code}</span></td>
                <td><strong>${player.was_home ? 'H' : 'A'}</strong> · ${utils.escapeHtml(player.opponent_team_name)}</td>
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
        this.fixtures(data.fixtures);
        this.squad(data.scout_team);
        dom.signalState.textContent = 'Verdict ready';
        dom.sourceState.textContent = 'Official data';
        if (data.credits) dom.credits.textContent = data.credits;
    },

    header(data) {
        const event = data.event;
        dom.gameweekInfo.textContent = `Gameweek ${data.gameweek}`;
        dom.deadlineDate.textContent = event?.deadline_time
            ? utils.deadline(event.deadline_time)
            : 'Official deadline TBC';
        dom.eventState.textContent = utils.eventLabel(event);
        this.startCountdown(event?.deadline_time);
    },

    startCountdown(deadline) {
        clearInterval(appState.countdownTimer);
        const update = () => { dom.deadlineCountdown.textContent = utils.countdown(deadline); };
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
        dom.fixtureCount.textContent = String(data.fixtures.length);
        dom.dataState.textContent = event?.data_checked ? 'Checked' : utils.eventLabel(event);
        dom.dataStateSub.textContent = event?.finished
            ? 'Official scores finalised'
            : event?.data_checked ? 'Official data checked' : 'Live changes possible';
        dom.lastSync.textContent = utils.formatDate(data.syncedAt, {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    },

    statistics(players) {
        const total = players.reduce((sum, player) => sum + utils.number(player.expected_points, 0), 0);
        const value = players.reduce((sum, player) => sum + utils.number(player.price, 0), 0);
        const captain = players.find(player => player.role === 'captain');
        const differentialCount = players.filter(player => {
            const ownership = utils.number(player.selected_by_percent);
            return ownership !== null && ownership < 10;
        }).length;

        dom.totalPoints.textContent = total.toFixed(2);
        dom.captainName.textContent = captain?.web_name || '—';
        dom.captainPoints.textContent = captain
            ? `${utils.number(captain.expected_points, 0).toFixed(2)} xP`
            : '— xP';
        dom.squadValue.textContent = value ? `£${value.toFixed(1)}m` : '—';
        dom.differentialCount.textContent = String(differentialCount);
    },

    fixtures(fixtures) {
        if (!fixtures.length) {
            dom.fixtureRail.innerHTML = '<div class="rail-loading">No official fixtures have been published for this Gameweek.</div>';
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

    squad(players) {
        if (!Array.isArray(players) || !players.length) {
            throw new Error('The AI engine returned no squad players');
        }
        const positionOrder = ['Goalkeeper', 'Defender', 'Midfielder', 'Forward'];
        const grouped = Object.fromEntries(positionOrder.map(position => [position, []]));
        players.forEach(player => {
            const position = player.position || player.element_type;
            if (grouped[position]) grouped[position].push(player);
        });
        Object.values(grouped).forEach(group => group.sort(
            (a, b) => utils.number(b.expected_points, 0) - utils.number(a.expected_points, 0)
        ));

        dom.pitch.innerHTML = positionOrder.map(position => {
            const group = grouped[position];
            if (!group.length) return '';
            const positionClass = position.toLowerCase();
            return `
                <div class="position-label position-${positionClass}">${position}</div>
                <div class="formation-line formation-${positionClass}">${group.map(player => playerRenderer.card(player)).join('')}</div>`;
        }).join('');
        dom.pitch.classList.remove('fade-in');
        requestAnimationFrame(() => dom.pitch.classList.add('fade-in'));

        const ranked = [...players].sort(
            (a, b) => utils.number(b.expected_points, 0) - utils.number(a.expected_points, 0)
        );
        dom.tableBody.innerHTML = ranked.map((player, index) =>
            playerRenderer.tableRow(player, index)
        ).join('');
        dom.formationChip.textContent = positionOrder.map(position => grouped[position].length).join(' • ');
        dom.squadSubtitle.textContent = `${players.length} official players · select any pick for the full briefing`;
    },

    loading(message = 'Running local AI inference…') {
        dom.pitch.innerHTML = `
            <div class="loading" role="status" aria-live="polite">
                <span class="loading-ball" aria-hidden="true"></span>
                ${utils.escapeHtml(message)}
            </div>`;
        dom.fixtureRail.innerHTML = '<div class="rail-loading">Syncing official fixtures…</div>';
        dom.signalState.textContent = 'Computing';
        dom.sourceState.textContent = 'Syncing';
    },

    error(message) {
        dom.pitch.innerHTML = `
            <div class="error" role="alert">
                <strong>Scout temporarily unavailable</strong>
                <span>${utils.escapeHtml(message)}</span>
                <small>Official FPL may be updating. Please try again shortly.</small>
            </div>`;
        dom.fixtureRail.innerHTML = '<div class="rail-loading error-inline">Fixture sync paused.</div>';
        dom.signalState.textContent = 'Retry needed';
        dom.sourceState.textContent = 'Sync paused';
    }
};

const gameweekManager = {
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

    async load(gameweek, force = false) {
        if (appState.isLoading) return;
        appState.isLoading = true;
        dom.gameweekSelect.disabled = true;
        document.querySelectorAll('.gameweek-option').forEach(button => { button.disabled = true; });
        dashboardRenderer.loading(`Running local AI for Gameweek ${gameweek}…`);
        try {
            const data = await dataLoader.loadDashboard(gameweek, force);
            appState.currentData = data;
            appState.currentGameweek = Number(gameweek);
            this.setActive(gameweek);
            dashboardRenderer.render(data);
        } catch (error) {
            console.error('Dashboard load failed:', error);
            dashboardRenderer.error(error.message);
        } finally {
            appState.isLoading = false;
            dom.gameweekSelect.disabled = false;
            document.querySelectorAll('.gameweek-option').forEach(button => { button.disabled = false; });
        }
    }
};

const interactions = {
    showPlayer(target) {
        let player;
        try {
            player = JSON.parse(target.dataset.player);
        } catch (error) {
            console.error('Could not parse player briefing:', error);
            return;
        }
        const status = utils.statusInfo(player.status, player.can_select !== false);
        const role = player.role === 'captain' ? 'Captain'
            : player.role === 'vice' ? 'Vice captain' : 'Squad';

        dom.dialogPositionBadge.textContent = utils.positionCode(player);
        dom.dialogPlayerName.textContent = player.web_name;
        dom.dialogTeam.textContent = `${player.team_name} · ${status.label}`;
        dom.dialogPoints.textContent = utils.number(player.expected_points, 0).toFixed(2);
        dom.dialogFixture.textContent = `${player.was_home ? 'H' : 'A'} · ${player.opponent_team_name}`;
        dom.dialogVenue.textContent = player.was_home ? 'Home' : 'Away';
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

    async refresh() {
        if (appState.isLoading) return;
        dom.refreshButton.classList.add('refreshing');
        dom.refreshButton.disabled = true;
        appState.dashboardCache.delete(appState.currentGameweek);
        appState.referenceCache = null;
        try {
            await gameweekManager.load(appState.currentGameweek, true);
        } finally {
            dom.refreshButton.classList.remove('refreshing');
            dom.refreshButton.disabled = false;
        }
    },

    init() {
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
        document.addEventListener('click', event => {
            const target = event.target.closest('[data-player]');
            if (target) this.showPlayer(target);
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
    async init() {
        interactions.init();
        dashboardRenderer.loading('Checking the official FPL season…');
        try {
            const reference = await dataLoader.loadReferenceData();
            appState.events = reference.events;
            if (!appState.events.length) throw new Error('No official gameweeks are published yet');
            const active = appState.events.find(event => event.is_current)
                || appState.events.find(event => event.is_next)
                || appState.events[0];
            appState.currentGameweek = Number(active.id);
            gameweekManager.populate(appState.events);
            await gameweekManager.load(appState.currentGameweek);
        } catch (error) {
            console.error('Application initialization failed:', error);
            dashboardRenderer.error(error.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => app.init());

window.FPLScoutApp = {
    app,
    appState,
    CONFIG,
    utils,
    dataLoader,
    gameweekManager,
    clearCache: utils.clearCache
};

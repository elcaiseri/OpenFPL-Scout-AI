/* OpenFPL Control Room — the owner's private dashboard.
 *
 * Plain JavaScript, no libraries: like static/script.js, every module is an
 * object literal and the whole app is exposed on window.OpenFPLAdmin for the
 * Node tests. Charts are hand-built SVG (see static/report/charts.js).
 *
 * Every data string from the API (player names, news, logs) is inserted with
 * textContent through h(); nothing from the network reaches innerHTML.
 */
"use strict";

const CONFIG = {
    storageKeys: {
        token: "openfpl.admin.token",
        tab: "openfpl.admin.tab",
        entry: "openfpl.admin.entry",
        recentTeams: "openfpl.recentTeams",
    },
    timeoutMs: 60000,
    slowTimeoutMs: 180000,
    retries: 2,
    retryDelayMs: 500,
    retryStatuses: [429, 500, 502, 503, 504],
    pageSize: 100,
    healthPollMs: 60000,
    logPollMs: 10000,
    // Categorical series, dark steps validated on the chart surface (#160c1e):
    // adjacent CVD ΔE ≥ 8.4, normal-vision ΔE ≥ 19.8, all ≥ 3:1 contrast.
    series: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
    muted: "#6d6178",
    positive: "#3987e5",
    negative: "#e66767",
    highlight: "#c8ff32",
    sequential: ["#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4"],
    positions: { 1: "GK", 2: "DEF", 3: "MID", 4: "FWD" },
};

class ApiError extends Error {
    constructor(message, status, payload) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.payload = payload;
    }
}

/* ------------------------------------------------------------------ */
/* Storage and authentication                                          */
/* ------------------------------------------------------------------ */

const storage = {
    area(kind) {
        try {
            return globalThis[kind] || null;
        } catch (error) {
            return null;
        }
    },
    get(kind, key) {
        try {
            const area = this.area(kind);
            return area ? area.getItem(key) : null;
        } catch (error) {
            return null;
        }
    },
    set(kind, key, value) {
        try {
            const area = this.area(kind);
            if (!area) return false;
            area.setItem(key, value);
            return true;
        } catch (error) {
            return false;
        }
    },
    remove(kind, key) {
        try {
            const area = this.area(kind);
            if (area) area.removeItem(key);
        } catch (error) {
            /* storage unavailable */
        }
    },
};

const auth = {
    token: null,
    load() {
        const key = CONFIG.storageKeys.token;
        this.token = storage.get("sessionStorage", key) || storage.get("localStorage", key) || null;
        return this.token;
    },
    save(token, remember) {
        const key = CONFIG.storageKeys.token;
        this.token = String(token || "").trim() || null;
        if (!this.token) return;
        storage.set("sessionStorage", key, this.token);
        if (remember) storage.set("localStorage", key, this.token);
        else storage.remove("localStorage", key);
    },
    clear() {
        const key = CONFIG.storageKeys.token;
        this.token = null;
        storage.remove("sessionStorage", key);
        storage.remove("localStorage", key);
    },
    headers() {
        return this.token ? { Authorization: `Bearer ${this.token}` } : {};
    },
};

/* ------------------------------------------------------------------ */
/* Utilities                                                           */
/* ------------------------------------------------------------------ */

const utils = {
    escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (character) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        })[character]);
    },
    isNumber(value) {
        return typeof value === "number" && Number.isFinite(value);
    },
    fmt(value, digits = 1) {
        const number = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
        if (!utils.isNumber(number)) return "–";
        return number.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    },
    fmtInt(value) {
        return utils.isNumber(value) ? Math.round(value).toLocaleString("en-GB") : "–";
    },
    fmtCompact(value) {
        if (!utils.isNumber(value)) return "–";
        const abs = Math.abs(value);
        if (abs >= 1e6) return `${utils.fmt(value / 1e6, 1)}M`;
        if (abs >= 1e4) return `${utils.fmt(value / 1e3, 1)}K`;
        return utils.fmtInt(value);
    },
    fmtPct(fraction, digits = 0) {
        return utils.isNumber(fraction) ? `${utils.fmt(fraction * 100, digits)}%` : "–";
    },
    fmtSigned(value, digits = 2) {
        if (!utils.isNumber(value)) return "–";
        return `${value > 0 ? "+" : value < 0 ? "−" : "±"}${utils.fmt(Math.abs(value), digits)}`;
    },
    fmtBytes(bytes) {
        if (!utils.isNumber(bytes)) return "–";
        const units = ["B", "KB", "MB", "GB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${utils.fmt(value, unit ? 1 : 0)} ${units[unit]}`;
    },
    fmtDuration(seconds) {
        if (!utils.isNumber(seconds)) return "–";
        const total = Math.max(0, Math.round(seconds));
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        if (days) return `${days}d ${hours}h`;
        if (hours) return `${hours}h ${minutes}m`;
        if (minutes) return `${minutes}m ${total % 60}s`;
        return `${total}s`;
    },
    fmtMs(ms) {
        if (!utils.isNumber(ms)) return "–";
        return ms >= 1000 ? `${utils.fmt(ms / 1000, 2)} s` : `${utils.fmt(ms, 0)} ms`;
    },
    timeAgo(iso, now = Date.now()) {
        if (!iso) return "–";
        const time = Date.parse(iso);
        if (!Number.isFinite(time)) return "–";
        const seconds = (now - time) / 1000;
        if (seconds < 0) return `in ${utils.fmtDuration(-seconds)}`;
        if (seconds < 5) return "just now";
        return `${utils.fmtDuration(seconds)} ago`;
    },
    dateTime(iso) {
        if (!iso) return "–";
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return String(iso);
        return date.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    },
    qs(params) {
        if (!params) return "";
        const parts = [];
        Object.entries(params).forEach(([key, value]) => {
            if (value === undefined || value === null || value === "") return;
            parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
        });
        return parts.length ? `?${parts.join("&")}` : "";
    },
    sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    },
    debounce(fn, ms) {
        let timer = null;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), ms);
        };
    },
    positionName(value) {
        if (typeof value === "string") {
            const map = { Goalkeeper: "GK", Defender: "DEF", Midfielder: "MID", Forward: "FWD" };
            return map[value] || value;
        }
        return CONFIG.positions[value] || "?";
    },
    sum(values) {
        return values.reduce((total, value) => total + (utils.isNumber(value) ? value : 0), 0);
    },
    mean(values) {
        const numbers = values.filter(utils.isNumber);
        return numbers.length ? utils.sum(numbers) / numbers.length : null;
    },
    download(filename, text, type = "text/plain") {
        const blob = new Blob([text], { type });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    csv(rows, columns) {
        const escape = (value) => {
            const text = value === null || value === undefined ? "" : String(value);
            return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
        };
        return [columns.join(","), ...rows.map((row) => columns.map((column) => escape(row[column])).join(","))].join("\n");
    },
    detail(payload) {
        if (!payload) return null;
        if (typeof payload === "string") return payload.slice(0, 300);
        const detail = payload.detail;
        if (typeof detail === "string") return detail;
        if (Array.isArray(detail)) return detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
        return null;
    },
};

/* DOM builder: strings become text nodes, never HTML. */
function h(tag, props, ...children) {
    const element = document.createElement(tag);
    if (props) {
        Object.entries(props).forEach(([key, value]) => {
            if (value === undefined || value === null || value === false) return;
            if (key === "class") element.className = value;
            else if (key === "text") element.textContent = value;
            else if (key === "style" && typeof value === "object") Object.assign(element.style, value);
            else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2).toLowerCase(), value);
            else if (key === "dataset") Object.assign(element.dataset, value);
            else if (value === true) element.setAttribute(key, "");
            else element.setAttribute(key, String(value));
        });
    }
    appendChildren(element, children);
    return element;
}

/* Like Element.append, but skips null/false and flattens arrays. */
function add(element, ...children) {
    return appendChildren(element, children);
}

function appendChildren(element, children) {
    children.flat(Infinity).forEach((child) => {
        if (child === null || child === undefined || child === false) return;
        element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return element;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
    const element = document.createElementNS(SVG_NS, tag);
    if (attrs) {
        Object.entries(attrs).forEach(([key, value]) => {
            if (value === undefined || value === null) return;
            if (key === "text") element.textContent = value;
            else if (key === "style" && typeof value === "object") Object.assign(element.style, value);
            else element.setAttribute(key, String(value));
        });
    }
    children.flat(Infinity).forEach((child) => child && element.append(child));
    return element;
}

function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
}

const ICONS = {
    good: "M5 10.5l3 3 7-7",
    warning: "M10 3l8 14H2L10 3zm0 5v4m0 2.5v.5",
    serious: "M10 2a8 8 0 100 16 8 8 0 000-16zm0 4v5m0 3v.5",
    critical: "M5 5l10 10M15 5L5 15",
    neutral: "M4 10h12",
};

function statusBadge(kind, label) {
    const icon = s("svg", { viewBox: "0 0 20 20", "aria-hidden": "true" },
        s("path", { d: ICONS[kind] || ICONS.neutral, fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
    return h("span", { class: `status ${kind}` }, icon, label);
}

function chip(text, kind) {
    return h("span", { class: `chip${kind ? ` ${kind}` : ""}` }, text);
}

/* ------------------------------------------------------------------ */
/* API client                                                          */
/* ------------------------------------------------------------------ */

const api = {
    onUnauthorized: () => {},
    now() {
        return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
    },
    async request(path, options = {}) {
        const {
            method = "GET",
            query,
            body,
            timeoutMs = CONFIG.timeoutMs,
            retries,
            useAuth = true,
        } = options;
        const url = `${path}${utils.qs(query)}`;
        const attempts = method === "GET" ? (retries ?? CONFIG.retries) : 0;
        for (let attempt = 0; ; attempt += 1) {
            const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
            const timer = controller && timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
            const started = api.now();
            try {
                const headers = { Accept: "application/json", ...(useAuth ? auth.headers() : {}) };
                if (body !== undefined) headers["Content-Type"] = "application/json";
                const response = await fetch(url, {
                    method,
                    headers,
                    body: body !== undefined ? JSON.stringify(body) : undefined,
                    signal: controller ? controller.signal : undefined,
                    cache: "no-store",
                });
                const text = await response.text();
                const ms = api.now() - started;
                let data = null;
                if (text) {
                    try {
                        data = JSON.parse(text);
                    } catch (error) {
                        data = text;
                    }
                }
                if (response.status === 401 && useAuth) {
                    api.onUnauthorized();
                    throw new ApiError("Your token was rejected. Sign in again.", 401, data);
                }
                if (!response.ok) {
                    if (attempt < attempts && CONFIG.retryStatuses.includes(response.status)) {
                        await utils.sleep(CONFIG.retryDelayMs * 2 ** attempt);
                        continue;
                    }
                    throw new ApiError(utils.detail(data) || `Request failed (${response.status})`, response.status, data);
                }
                return { data, status: response.status, ms, bytes: text.length, url };
            } catch (error) {
                if (error instanceof ApiError) throw error;
                const retryable = error && (error.name === "AbortError" || error instanceof TypeError);
                if (retryable && attempt < attempts) {
                    await utils.sleep(CONFIG.retryDelayMs * 2 ** attempt);
                    continue;
                }
                throw new ApiError(
                    error && error.name === "AbortError" ? "The request timed out." : (error && error.message) || "Network error",
                    0,
                );
            } finally {
                if (timer) clearTimeout(timer);
            }
        }
    },
    async get(path, query, options = {}) {
        return (await api.request(path, { ...options, query })).data;
    },
    async post(path, body, query, options = {}) {
        return (await api.request(path, { ...options, method: "POST", body, query })).data;
    },
};

/* ------------------------------------------------------------------ */
/* Tooltip, toasts, dialogs                                            */
/* ------------------------------------------------------------------ */

const tooltip = {
    node() {
        return document.getElementById("tooltip");
    },
    show(event, title, rows) {
        const node = tooltip.node();
        if (!node) return;
        clear(node);
        if (title) node.append(h("div", { class: "t-title" }, title));
        (rows || []).forEach((row) => {
            node.append(h("div", { class: "t-row" },
                h("span", null, row.color ? h("i", { class: "t-key", style: { background: row.color } }) : null, row.label),
                h("strong", null, row.value)));
        });
        node.hidden = false;
        tooltip.move(event);
    },
    move(event) {
        const node = tooltip.node();
        if (!node || node.hidden || !event) return;
        const x = event.clientX ?? 0;
        const y = event.clientY ?? 0;
        const width = node.offsetWidth || 200;
        const height = node.offsetHeight || 60;
        const left = Math.min(window.innerWidth - width - 8, x + 14);
        const top = y + height + 20 > window.innerHeight ? y - height - 12 : y + 14;
        node.style.left = `${Math.max(8, left)}px`;
        node.style.top = `${Math.max(8, top)}px`;
    },
    hide() {
        const node = tooltip.node();
        if (node) node.hidden = true;
    },
};

const toast = {
    show(message, kind = "info", ms = 5000) {
        const area = document.getElementById("toasts");
        if (!area) return;
        const node = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
        area.append(node);
        setTimeout(() => node.remove(), ms);
    },
};

const dialogs = {
    confirm(title, message, okLabel = "Confirm") {
        const dialog = document.getElementById("confirm-dialog");
        if (!dialog || typeof dialog.showModal !== "function") {
            return Promise.resolve(window.confirm(`${title}\n\n${message}`));
        }
        document.getElementById("confirm-title").textContent = title;
        document.getElementById("confirm-message").textContent = message;
        document.getElementById("confirm-ok").textContent = okLabel;
        dialog.returnValue = "";
        dialog.showModal();
        return new Promise((resolve) => {
            dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
        });
    },
    modal(title, content) {
        const dialog = document.getElementById("modal");
        document.getElementById("modal-title").textContent = title;
        const body = clear(document.getElementById("modal-body"));
        appendChildren(body, [content]);
        if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
        return body;
    },
};

/* ------------------------------------------------------------------ */
/* Charts (hand-built SVG)                                             */
/* ------------------------------------------------------------------ */

const charts = {
    width(container, fallback = 640) {
        const width = container && container.clientWidth;
        return Math.max(280, Math.round(width || fallback));
    },
    /* Never upscale: a chart drawn before its container was laid out keeps
       its drawn size instead of stretching its text across a wide card. */
    mount(container, svg) {
        const viewBox = String(svg.getAttribute("viewBox") || "").split(" ");
        if (viewBox.length === 4) svg.style.maxWidth = `${viewBox[2]}px`;
        container.append(h("div", { class: "chart" }, svg));
    },
    niceTicks(min, max, count = 5) {
        if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
        if (min === max) {
            max = min + 1;
        }
        const span = max - min;
        const step0 = span / Math.max(1, count);
        const magnitude = 10 ** Math.floor(Math.log10(step0));
        const residual = step0 / magnitude;
        const step = (residual > 5 ? 10 : residual > 2 ? 5 : residual > 1 ? 2 : 1) * magnitude;
        const start = Math.floor(min / step);
        const end = Math.ceil(max / step);
        const ticks = [];
        for (let index = start; index <= end; index += 1) {
            ticks.push(Number((index * step).toPrecision(12)));
        }
        return ticks;
    },
    legend(container, items, kind = "rect") {
        if (!items || items.length < 2) return;
        container.append(h("div", { class: "legend" },
            items.map((item) => h("span", null, h("i", { class: kind === "line" ? "line" : "", style: { background: item.color } }), item.label))));
    },
    empty(container, message = "No data yet.") {
        container.append(h("p", { class: "empty" }, message));
    },
    tickLabel(value) {
        if (!utils.isNumber(value)) return "";
        const abs = Math.abs(value);
        if (abs >= 10000) return utils.fmtCompact(value);
        if (Number.isInteger(value)) return value.toLocaleString("en-GB");
        return utils.fmt(value, abs < 1 ? 2 : 1);
    },

    /* Horizontal bars: magnitude, or diverging around zero. */
    hbars(container, items, options = {}) {
        const {
            format = (value) => utils.fmt(value, 2),
            diverging = false,
            barHeight = 18,
            gap = 8,
            labelWidth = 150,
            onClick = null,
            color = CONFIG.series[0],
        } = options;
        if (!items.length) return charts.empty(container);
        const width = charts.width(container);
        const valueSpace = 64;
        const height = items.length * (barHeight + gap) + 8;
        const values = items.flatMap((item) => [item.value, ...(item.range || [])]).filter(utils.isNumber);
        const maxValue = Math.max(0, ...values);
        const minValue = diverging ? Math.min(0, ...values) : 0;
        // Room for value labels beside negative bars as well as positive ones.
        const plotLeft = Math.min(labelWidth, width * 0.38) + (minValue < 0 ? valueSpace : 0);
        const plotWidth = Math.max(60, width - plotLeft - (maxValue > 0 || !diverging ? valueSpace : 12));
        const span = maxValue - minValue || 1;
        const x = (value) => plotLeft + ((value - minValue) / span) * plotWidth;
        const zero = x(0);
        const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.ariaLabel || "Bar chart" });
        if (diverging) svg.append(s("line", { class: "axis", x1: zero, x2: zero, y1: 0, y2: height }));
        items.forEach((item, index) => {
            const y = index * (barHeight + gap) + 4;
            const value = utils.isNumber(item.value) ? item.value : 0;
            const fill = item.color || (diverging ? (value >= 0 ? CONFIG.positive : CONFIG.negative) : color);
            const x0 = Math.min(zero, x(value));
            const w = Math.max(1, Math.abs(x(value) - zero));
            const r = Math.min(4, w / 2, barHeight / 2);
            const positive = value >= 0;
            // Rounded data end, square at the baseline.
            const path = positive
                ? `M${x0},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${barHeight - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}z`
                : `M${x0 + w},${y}h-${w - r}a${r},${r} 0 0 0 -${r},${r}v${barHeight - 2 * r}a${r},${r} 0 0 0 ${r},${r}h${w - r}z`;
            const group = s("g", { class: "mark" });
            const labelRight = plotLeft - (minValue < 0 ? valueSpace : 0) - 8;
            group.append(s("text", { class: "label", x: labelRight, y: y + barHeight / 2 + 4, "text-anchor": "end", text: charts.truncate(item.label, Math.floor(labelRight / 6.6)) }));
            group.append(s("path", { d: path, fill, opacity: item.dim ? 0.45 : 1 }));
            if (item.range && item.range.every(utils.isNumber)) {
                group.append(s("line", { x1: x(item.range[0]), x2: x(item.range[1]), y1: y + barHeight / 2, y2: y + barHeight / 2, stroke: "#f3f0f7", "stroke-width": 2, "stroke-linecap": "round" }));
            }
            if (item.marker) {
                group.append(s("circle", { cx: x0 + w + 2, cy: y + barHeight / 2, r: 4, fill: CONFIG.highlight, stroke: "#160c1e", "stroke-width": 2 }));
            }
            const labelX = positive ? Math.max(x0 + w, item.range ? x(Math.max(...item.range)) : 0) + 6 : x0 - 6;
            group.append(s("text", { class: "value", x: labelX, y: y + barHeight / 2 + 4, "text-anchor": positive ? "start" : "end", text: format(item.value) }));
            const hit = s("rect", { class: "hit", x: 0, y: y - gap / 2, width, height: barHeight + gap });
            hit.addEventListener("pointermove", (event) => tooltip.show(event, item.label, item.tooltip || [{ label: options.valueLabel || "Value", value: format(item.value), color: fill }]));
            hit.addEventListener("pointerleave", tooltip.hide);
            if (onClick) hit.addEventListener("click", () => onClick(item));
            group.append(hit);
            svg.append(group);
        });
        charts.mount(container, svg);
    },

    truncate(text, max) {
        const value = String(text ?? "");
        return value.length > max ? `${value.slice(0, Math.max(1, max - 1))}…` : value;
    },

    /* Vertical columns (histograms, per-gameweek counts). */
    columns(container, items, options = {}) {
        const { format = (value) => utils.fmtInt(value), height = 200, color = CONFIG.series[0], xLabel = "" } = options;
        if (!items.length) return charts.empty(container);
        const width = charts.width(container);
        const margin = { top: 16, right: 8, bottom: xLabel ? 40 : 28, left: 40 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const maxValue = Math.max(...items.map((item) => item.value).filter(utils.isNumber), 0) || 1;
        const ticks = charts.niceTicks(0, maxValue, 4);
        const top = ticks[ticks.length - 1] || maxValue;
        const y = (value) => margin.top + plotHeight - (value / top) * plotHeight;
        const band = plotWidth / items.length;
        const barWidth = Math.min(24, Math.max(2, band - 2));
        const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.ariaLabel || "Column chart" });
        ticks.forEach((tick) => {
            svg.append(s("line", { class: "gridline", x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick) }));
            svg.append(s("text", { x: margin.left - 6, y: y(tick) + 4, "text-anchor": "end", text: charts.tickLabel(tick) }));
        });
        const every = Math.ceil(items.length / Math.max(1, Math.floor(plotWidth / 34)));
        items.forEach((item, index) => {
            const value = utils.isNumber(item.value) ? item.value : 0;
            const cx = margin.left + band * index + band / 2;
            const x0 = cx - barWidth / 2;
            const y0 = y(value);
            const barH = Math.max(0, margin.top + plotHeight - y0);
            const r = Math.min(4, barWidth / 2, barH);
            const path = barH > 0
                ? `M${x0},${y0 + barH}v-${barH - r}a${r},${r} 0 0 1 ${r},-${r}h${barWidth - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${barH - r}z`
                : "";
            if (path) svg.append(s("path", { class: "mark", d: path, fill: item.color || color }));
            if (index % every === 0) svg.append(s("text", { x: cx, y: height - margin.bottom + 14, "text-anchor": "middle", text: item.label }));
            const hit = s("rect", { class: "hit", x: margin.left + band * index, y: margin.top, width: band, height: plotHeight });
            hit.addEventListener("pointermove", (event) => tooltip.show(event, item.title || item.label, [{ label: options.valueLabel || "Value", value: format(item.value), color: item.color || color }]));
            hit.addEventListener("pointerleave", tooltip.hide);
            svg.append(hit);
        });
        svg.append(s("line", { class: "axis", x1: margin.left, x2: width - margin.right, y1: margin.top + plotHeight, y2: margin.top + plotHeight }));
        if (xLabel) svg.append(s("text", { x: margin.left + plotWidth / 2, y: height - 6, "text-anchor": "middle", text: xLabel }));
        charts.mount(container, svg);
    },

    /* Lines over a numeric x (gameweeks), crosshair tooltip with every series. */
    lines(container, series, options = {}) {
        const {
            height = 240,
            format = (value) => utils.fmt(value, 2),
            xFormat = (value) => `GW${value}`,
            invert = false,
            references = [],
            yMin = null,
        } = options;
        const usable = series.filter((item) => item.points.some((point) => utils.isNumber(point.y)));
        if (!usable.length) return charts.empty(container, options.emptyMessage);
        charts.legend(container, usable.map((item) => ({ label: item.name, color: item.color })), "line");
        const width = charts.width(container);
        const margin = { top: 14, right: 16, bottom: 28, left: 52 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const xs = [...new Set(usable.flatMap((item) => item.points.map((point) => point.x)))].sort((a, b) => a - b);
        const ys = usable.flatMap((item) => item.points.map((point) => point.y)).concat(references.map((ref) => ref.y)).filter(utils.isNumber);
        let low = yMin !== null ? yMin : Math.min(...ys);
        let high = Math.max(...ys);
        if (low === high) {
            low -= 1;
            high += 1;
        }
        const ticks = charts.niceTicks(low, high, 4);
        low = Math.min(low, ticks[0]);
        high = Math.max(high, ticks[ticks.length - 1]);
        const xMin = xs[0];
        const xMax = xs[xs.length - 1];
        const x = (value) => margin.left + (xMax === xMin ? plotWidth / 2 : ((value - xMin) / (xMax - xMin)) * plotWidth);
        const y = (value) => {
            const ratio = (value - low) / (high - low || 1);
            return margin.top + (invert ? ratio : 1 - ratio) * plotHeight;
        };
        const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.ariaLabel || "Line chart" });
        ticks.forEach((tick) => {
            svg.append(s("line", { class: "gridline", x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick) }));
            svg.append(s("text", { x: margin.left - 6, y: y(tick) + 4, "text-anchor": "end", text: charts.tickLabel(tick) }));
        });
        const every = Math.ceil(xs.length / Math.max(1, Math.floor(plotWidth / 44)));
        xs.forEach((value, index) => {
            if (index % every === 0 || index === xs.length - 1) {
                svg.append(s("text", { x: x(value), y: height - 8, "text-anchor": "middle", text: xFormat(value) }));
            }
        });
        references.forEach((ref) => {
            svg.append(s("line", { x1: margin.left, x2: width - margin.right, y1: y(ref.y), y2: y(ref.y), stroke: "#91869d", "stroke-width": 1, "stroke-dasharray": "4 4" }));
            svg.append(s("text", { class: "label", x: width - margin.right, y: y(ref.y) - 4, "text-anchor": "end", text: ref.label }));
        });
        usable.forEach((item) => {
            const points = item.points.filter((point) => utils.isNumber(point.y));
            const d = points.map((point, index) => `${index ? "L" : "M"}${x(point.x)},${y(point.y)}`).join("");
            svg.append(s("path", { class: "mark", d, fill: "none", stroke: item.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": item.dashed ? "5 4" : null }));
            const last = points[points.length - 1];
            if (last) svg.append(s("circle", { cx: x(last.x), cy: y(last.y), r: 4, fill: item.color, stroke: "#160c1e", "stroke-width": 2 }));
        });
        const crosshair = s("line", { class: "crosshair", y1: margin.top, y2: margin.top + plotHeight, visibility: "hidden" });
        svg.append(crosshair);
        const overlay = s("rect", { class: "hit", x: margin.left, y: margin.top, width: plotWidth, height: plotHeight });
        overlay.addEventListener("pointermove", (event) => {
            const box = svg.getBoundingClientRect();
            const px = ((event.clientX - box.left) / box.width) * width;
            const nearest = xs.reduce((best, value) => (Math.abs(x(value) - px) < Math.abs(x(best) - px) ? value : best), xs[0]);
            crosshair.setAttribute("x1", x(nearest));
            crosshair.setAttribute("x2", x(nearest));
            crosshair.setAttribute("visibility", "visible");
            tooltip.show(event, xFormat(nearest), usable.map((item) => {
                const point = item.points.find((candidate) => candidate.x === nearest);
                return { label: item.name, value: point && utils.isNumber(point.y) ? format(point.y) : "–", color: item.color };
            }));
        });
        overlay.addEventListener("pointerleave", () => {
            crosshair.setAttribute("visibility", "hidden");
            tooltip.hide();
        });
        svg.append(overlay);
        charts.mount(container, svg);
    },

    /* Scatter with nearest-point hover (no 1px hunting). */
    scatter(container, points, options = {}) {
        const {
            height = 300,
            xLabel = "",
            yLabel = "",
            formatX = (value) => utils.fmt(value, 1),
            formatY = (value) => utils.fmt(value, 2),
            onClick = null,
            legend = null,
        } = options;
        const usable = points.filter((point) => utils.isNumber(point.x) && utils.isNumber(point.y));
        if (!usable.length) return charts.empty(container);
        if (legend) charts.legend(container, legend);
        const width = charts.width(container);
        const margin = { top: 12, right: 16, bottom: 40, left: 48 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const xTicks = charts.niceTicks(Math.min(...usable.map((p) => p.x)), Math.max(...usable.map((p) => p.x)), 5);
        const yTicks = charts.niceTicks(Math.min(0, ...usable.map((p) => p.y)), Math.max(...usable.map((p) => p.y)), 4);
        const [x0, x1] = [xTicks[0], xTicks[xTicks.length - 1]];
        const [y0, y1] = [yTicks[0], yTicks[yTicks.length - 1]];
        const x = (value) => margin.left + ((value - x0) / (x1 - x0 || 1)) * plotWidth;
        const y = (value) => margin.top + plotHeight - ((value - y0) / (y1 - y0 || 1)) * plotHeight;
        const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": options.ariaLabel || "Scatter plot" });
        yTicks.forEach((tick) => {
            svg.append(s("line", { class: "gridline", x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick) }));
            svg.append(s("text", { x: margin.left - 6, y: y(tick) + 4, "text-anchor": "end", text: charts.tickLabel(tick) }));
        });
        xTicks.forEach((tick) => svg.append(s("text", { x: x(tick), y: margin.top + plotHeight + 16, "text-anchor": "middle", text: charts.tickLabel(tick) })));
        svg.append(s("line", { class: "axis", x1: margin.left, x2: width - margin.right, y1: margin.top + plotHeight, y2: margin.top + plotHeight }));
        if (xLabel) svg.append(s("text", { x: margin.left + plotWidth / 2, y: height - 4, "text-anchor": "middle", text: xLabel }));
        if (yLabel) svg.append(s("text", { x: 12, y: margin.top + plotHeight / 2, "text-anchor": "middle", transform: `rotate(-90 12 ${margin.top + plotHeight / 2})`, text: yLabel }));
        // Background points first so highlighted ones sit on top.
        const ordered = [...usable].sort((a, b) => Number(Boolean(a.highlight)) - Number(Boolean(b.highlight)));
        ordered.forEach((point) => {
            svg.append(s("circle", { class: "mark", cx: x(point.x), cy: y(point.y), r: point.highlight ? 5 : 4, fill: point.color || (point.highlight ? CONFIG.series[0] : CONFIG.muted), stroke: "#160c1e", "stroke-width": 2 }));
        });
        usable.filter((point) => point.label && point.annotate).forEach((point) => {
            svg.append(s("text", { class: "label", x: x(point.x) + 7, y: y(point.y) - 6, text: point.label }));
        });
        const ring = s("circle", { r: 8, fill: "none", stroke: CONFIG.highlight, "stroke-width": 2, visibility: "hidden" });
        svg.append(ring);
        const overlay = s("rect", { class: "hit", x: margin.left, y: margin.top, width: plotWidth, height: plotHeight });
        let current = null;
        overlay.addEventListener("pointermove", (event) => {
            const box = svg.getBoundingClientRect();
            const px = ((event.clientX - box.left) / box.width) * width;
            const py = ((event.clientY - box.top) / box.height) * height;
            let best = null;
            let bestDistance = Infinity;
            usable.forEach((point) => {
                const distance = (x(point.x) - px) ** 2 + (y(point.y) - py) ** 2;
                if (distance < bestDistance) {
                    best = point;
                    bestDistance = distance;
                }
            });
            if (!best || bestDistance > 900) {
                current = null;
                ring.setAttribute("visibility", "hidden");
                tooltip.hide();
                return;
            }
            current = best;
            ring.setAttribute("cx", x(best.x));
            ring.setAttribute("cy", y(best.y));
            ring.setAttribute("visibility", "visible");
            tooltip.show(event, best.label, [
                { label: yLabel || "y", value: formatY(best.y) },
                { label: xLabel || "x", value: formatX(best.x) },
                ...(best.extra || []),
            ]);
        });
        overlay.addEventListener("pointerleave", () => {
            ring.setAttribute("visibility", "hidden");
            tooltip.hide();
        });
        if (onClick) {
            overlay.style.cursor = "pointer";
            overlay.addEventListener("click", () => current && onClick(current));
        }
        svg.append(overlay);
        charts.mount(container, svg);
    },

    /* Square heatmap on a single-hue ramp (e.g. model correlations). */
    heatmap(container, labels, matrix, options = {}) {
        const { min = 0, max = 1, format = (value) => utils.fmt(value, 2) } = options;
        if (!labels.length) return charts.empty(container);
        const width = Math.min(charts.width(container), 520);
        const left = 90;
        const top = 24;
        const cell = Math.max(28, Math.min(80, (width - left) / labels.length));
        const size = left + cell * labels.length;
        const svg = s("svg", { viewBox: `0 0 ${size} ${top + cell * labels.length}`, role: "img", "aria-label": options.ariaLabel || "Heatmap", style: { maxWidth: `${size}px` } });
        labels.forEach((label, index) => {
            svg.append(s("text", { class: "label", x: left + cell * index + cell / 2, y: top - 8, "text-anchor": "middle", text: charts.truncate(label, Math.floor(cell / 7)) }));
            svg.append(s("text", { class: "label", x: left - 6, y: top + cell * index + cell / 2 + 4, "text-anchor": "end", text: charts.truncate(label, 13) }));
        });
        labels.forEach((rowLabel, row) => {
            labels.forEach((columnLabel, column) => {
                const value = matrix[row][column];
                const ratio = utils.isNumber(value) ? Math.max(0, Math.min(1, (value - min) / (max - min || 1))) : null;
                const color = ratio === null ? "#2a2133" : CONFIG.sequential[Math.round(ratio * (CONFIG.sequential.length - 1))];
                const rect = s("rect", { class: "mark", x: left + cell * column + 1, y: top + cell * row + 1, width: cell - 2, height: cell - 2, rx: 4, fill: color });
                rect.addEventListener("pointermove", (event) => tooltip.show(event, `${rowLabel} × ${columnLabel}`, [{ label: options.valueLabel || "Correlation", value: format(value) }]));
                rect.addEventListener("pointerleave", tooltip.hide);
                svg.append(rect);
                const light = ratio !== null && ratio > 0.62;
                svg.append(s("text", { x: left + cell * column + cell / 2, y: top + cell * row + cell / 2 + 4, "text-anchor": "middle", style: { fill: light ? "#0b0b0b" : "#ffffff", pointerEvents: "none" }, text: format(value) }));
            });
        });
        charts.mount(container, svg);
    },

    /* Floating columns from a start value to a projection. */
    waterfall(container, steps, options = {}) {
        if (!steps.length) return charts.empty(container);
        const width = charts.width(container);
        const height = options.height || 230;
        const margin = { top: 20, right: 8, bottom: 46, left: 40 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        let running = 0;
        const bars = steps.map((step) => {
            if (step.kind === "start" || step.kind === "end") {
                running = step.kind === "start" ? step.value : running;
                return { ...step, from: 0, to: step.kind === "end" ? step.value : step.value };
            }
            const from = running;
            running += step.value;
            return { ...step, from, to: running };
        });
        const values = bars.flatMap((bar) => [bar.from, bar.to]);
        const ticks = charts.niceTicks(Math.min(0, ...values), Math.max(...values), 4);
        const low = ticks[0];
        const high = ticks[ticks.length - 1];
        const y = (value) => margin.top + plotHeight - ((value - low) / (high - low || 1)) * plotHeight;
        const band = plotWidth / bars.length;
        const barWidth = Math.min(36, band - 6);
        const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Projection waterfall" });
        ticks.forEach((tick) => {
            svg.append(s("line", { class: "gridline", x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick) }));
            svg.append(s("text", { x: margin.left - 6, y: y(tick) + 4, "text-anchor": "end", text: charts.tickLabel(tick) }));
        });
        bars.forEach((bar, index) => {
            const cx = margin.left + band * index + band / 2;
            const top = Math.min(y(bar.from), y(bar.to));
            const barH = Math.max(1.5, Math.abs(y(bar.from) - y(bar.to)));
            const total = bar.kind === "start" || bar.kind === "end";
            const fill = total ? (bar.kind === "end" ? CONFIG.highlight : "#8d82a0") : bar.value >= 0 ? CONFIG.positive : CONFIG.negative;
            svg.append(s("rect", { class: "mark", x: cx - barWidth / 2, y: top, width: barWidth, height: barH, rx: 3, fill }));
            svg.append(s("text", { class: "value", x: cx, y: top - 5, "text-anchor": "middle", text: total ? utils.fmt(bar.value, 2) : utils.fmtSigned(bar.value, 2) }));
            const words = String(bar.step).split(" ");
            words.slice(0, 2).forEach((word, line) => {
                svg.append(s("text", { class: "label", x: cx, y: height - margin.bottom + 14 + line * 12, "text-anchor": "middle", text: charts.truncate(word, Math.max(4, Math.floor(band / 6.5))) }));
            });
            const hit = s("rect", { class: "hit", x: cx - band / 2, y: margin.top, width: band, height: plotHeight });
            hit.addEventListener("pointermove", (event) => tooltip.show(event, bar.step, [{ label: total ? "Points" : "Change", value: total ? utils.fmt(bar.value, 2) : utils.fmtSigned(bar.value, 2), color: fill }]));
            hit.addEventListener("pointerleave", tooltip.hide);
            svg.append(hit);
        });
        charts.mount(container, svg);
    },
};

/* ------------------------------------------------------------------ */
/* Shared UI building blocks                                           */
/* ------------------------------------------------------------------ */

const ui = {
    card(title, subtitle, options = {}) {
        const actions = h("div", { class: "btn-row" });
        const body = h("div", { class: "card-body" });
        const root = h("section", { class: `card ${options.span ? `span-${options.span}` : "span-12"}` },
            h("div", { class: "card-head" },
                h("div", null, h("h2", null, title), subtitle ? h("p", null, subtitle) : null),
                actions),
            body);
        return { root, body, actions };
    },

    /* Load a card's content, showing the error (with a retry) if it fails. */
    async fill(card, loader) {
        card.root.classList.add("loading");
        try {
            const result = await loader(card.body, card);
            return result;
        } catch (error) {
            clear(card.body);
            card.body.append(ui.errorBox(error, () => ui.fill(card, loader)));
            return null;
        } finally {
            card.root.classList.remove("loading");
        }
    },

    errorBox(error, retry) {
        const message = (error && error.message) || String(error);
        return h("div", { class: "stack" },
            statusBadge(error && error.status === 404 ? "neutral" : "critical", error && error.status === 404 ? "Not available" : "Could not load"),
            h("p", { class: "muted small" }, message),
            retry ? h("div", null, h("button", { class: "btn small", type: "button", onClick: retry }, "Try again")) : null);
    },

    tiles(items) {
        return h("div", { class: "tiles" }, items.filter(Boolean).map((item) => h("div", { class: "tile" },
            h("span", { class: "label" }, item.label),
            h("span", { class: "value" }, item.value),
            item.detail ? h("span", { class: "detail" }, item.detail) : null,
            item.badge || null)));
    },

    kv(entries) {
        return h("dl", { class: "kv" }, entries.filter(Boolean).flatMap(([key, value]) => [h("dt", null, key), h("dd", null, value ?? "–")]));
    },

    /* Sortable table. columns: {key, label, value(row), render(row), num, sort} */
    table(rows, columns, options = {}) {
        const wrap = h("div", { class: `table-wrap${options.tall ? " tall" : ""}` });
        const foot = h("div", { class: "table-foot" });
        let sortKey = options.sortKey || null;
        let sortDir = options.sortDir || "desc";
        let limit = options.pageSize || rows.length;
        const container = h("div", null, wrap, options.footer === false ? null : foot);

        const valueOf = (column, row) => (column.value ? column.value(row) : row[column.key]);
        const render = () => {
            let ordered = [...rows];
            const column = columns.find((item) => item.key === sortKey);
            if (column) {
                ordered.sort((a, b) => {
                    const left = valueOf(column, a);
                    const right = valueOf(column, b);
                    const missingLeft = left === null || left === undefined || left === "";
                    const missingRight = right === null || right === undefined || right === "";
                    if (missingLeft || missingRight) return missingLeft === missingRight ? 0 : missingLeft ? 1 : -1;
                    const result = typeof left === "number" && typeof right === "number" ? left - right : String(left).localeCompare(String(right));
                    return sortDir === "asc" ? result : -result;
                });
            }
            const visible = ordered.slice(0, limit);
            const thead = h("thead", null, h("tr", null, columns.map((item) => {
                const sortable = item.sort !== false;
                const th = h("th", {
                    class: `${sortable ? "sortable" : ""}${item.num ? " num" : ""}`,
                    scope: "col",
                    title: item.title || null,
                    "aria-sort": item.key === sortKey ? (sortDir === "asc" ? "ascending" : "descending") : null,
                }, item.label);
                if (sortable) {
                    th.addEventListener("click", () => {
                        if (sortKey === item.key) sortDir = sortDir === "asc" ? "desc" : "asc";
                        else {
                            sortKey = item.key;
                            sortDir = item.num ? "desc" : "asc";
                        }
                        render();
                    });
                }
                return th;
            })));
            const tbody = h("tbody", null, visible.map((row) => {
                const tr = h("tr", { class: [options.onRowClick ? "clickable" : "", options.rowClass ? options.rowClass(row) : ""].join(" ").trim() || null },
                    columns.map((item) => h("td", { class: item.num ? "num" : null }, item.render ? item.render(row) : formatCell(valueOf(item, row), item))));
                if (options.onRowClick) {
                    tr.tabIndex = 0;
                    tr.addEventListener("click", () => options.onRowClick(row));
                    tr.addEventListener("keydown", (event) => {
                        if (event.key === "Enter") options.onRowClick(row);
                    });
                }
                return tr;
            }));
            clear(wrap).append(h("table", null, thead, tbody));
            clear(foot);
            foot.append(h("span", null, `${Math.min(limit, ordered.length)} of ${ordered.length} rows`));
            if (ordered.length > limit) {
                foot.append(h("button", { class: "btn small", type: "button", onClick: () => { limit += options.pageSize || 100; render(); } }, "Show more"));
            }
            if (!rows.length) wrap.append(h("p", { class: "empty", style: { padding: "12px" } }, options.empty || "Nothing to show."));
        };
        render();
        container.update = (nextRows) => {
            rows = nextRows;
            limit = options.pageSize || nextRows.length;
            render();
        };
        return container;
    },

    json(value) {
        const root = h("div", { class: "json" });
        const build = (data, key, depth) => {
            const label = key !== null ? [h("span", { class: "k" }, JSON.stringify(key)), ": "] : [];
            if (data === null || typeof data !== "object") {
                const kind = data === null ? "z" : typeof data === "string" ? "s" : typeof data === "number" ? "n" : "b";
                return h("div", null, label, h("span", { class: kind }, JSON.stringify(data)));
            }
            const entries = Array.isArray(data) ? data.map((item, index) => [index, item]) : Object.entries(data);
            const details = h("details", depth < 2 ? { open: true } : null,
                h("summary", null, label, Array.isArray(data) ? `[${entries.length}]` : `{${entries.length}}`));
            entries.slice(0, 500).forEach(([childKey, child]) => details.append(build(child, Array.isArray(data) ? null : childKey, depth + 1)));
            if (entries.length > 500) details.append(h("div", { class: "z" }, `… ${entries.length - 500} more`));
            return details;
        };
        root.append(build(value, null, 0));
        return root;
    },

    /* Render an array of flat objects as a table (official passthrough data). */
    autoTable(items, options = {}) {
        if (!Array.isArray(items) || !items.length) return h("p", { class: "empty" }, options.empty || "Nothing published yet.");
        const keys = options.keys || Object.keys(items[0]).filter((key) => items.some((item) => item[key] !== null && typeof item[key] !== "object")).slice(0, options.maxColumns || 8);
        return ui.table(items, keys.map((key) => ({ key, label: key.replace(/_/g, " "), num: typeof items[0][key] === "number" })), { pageSize: options.pageSize || 25 });
    },

    /* Split a squad into pitch rows (GK, DEF, MID, FWD starters) and bench, keeping order. */
    pitchRows(players, startingIds) {
        const starting = new Set((startingIds || []).map(Number));
        const rows = { GK: [], DEF: [], MID: [], FWD: [] };
        const bench = [];
        (players || []).forEach((player) => {
            const position = utils.positionName(player.element_type ?? player.position);
            if (starting.has(Number(player.id)) && rows[position]) rows[position].push(player);
            else bench.push(player);
        });
        return { rows: ["GK", "DEF", "MID", "FWD"].map((position) => rows[position]), bench };
    },

    /* A pitch of player buttons. options: startingIds, value(player), unit, title(player), onClick(player), selectedId. */
    pitch(players, options = {}) {
        const { rows, bench } = ui.pitchRows(players, options.startingIds);
        const value = options.value || ((player) => player.expected_points);
        const unit = options.unit || "xP";
        const card = (player) => {
            const factor = player.availability_factor;
            const doubtful = utils.isNumber(factor) && factor < 1;
            const selected = options.selectedId !== undefined && options.selectedId === player.id;
            return h("button", {
                class: `pitch-player${player.new ? " is-new" : ""}${selected ? " is-selected" : ""}${player.banned ? " is-banned" : ""}`,
                type: "button",
                title: options.title ? options.title(player) : player.news || "",
                "aria-pressed": options.selectedId !== undefined ? String(selected) : null,
                onClick: () => (options.onClick ? options.onClick(player) : drawer.open(player.id, player.web_name)),
            },
            player.role ? h("span", { class: `badge ${player.role === "vice" ? "vice" : ""}` }, player.role === "captain" ? "C" : "V") : null,
            player.new ? h("span", { class: "badge new" }, "IN") : null,
            h("strong", null, player.web_name || `#${player.id}`),
            h("span", { class: "small muted" }, player.team_name || ""),
            h("span", { class: "xp" }, player.pending ? "…" : `${utils.fmt(value(player), unit === "pts" ? 0 : 2)} ${unit}`),
            doubtful ? h("span", { class: `small ${factor > 0 ? "warn" : "out"}` }, factor > 0 ? `${utils.fmtPct(factor)} fit` : "out") : null,
            player.projection_missing ? h("span", { class: "small out" }, "no projection") : null,
            player.locked ? h("span", { class: "small tag-lock" }, "locked") : null,
            player.banned ? h("span", { class: "small out" }, "banned") : null,
            player.overridden ? h("span", { class: "small tag-own" }, "your view") : null,
            utils.isNumber(player.actual_points) ? h("span", { class: "small" }, `${utils.fmtInt(player.actual_points)} pts`) : null);
        };
        return h("div", { class: "pitch" },
            rows.map((row) => h("div", { class: "pitch-row" }, row.map(card))),
            h("div", { class: "bench" }, bench.map(card)));
    },

    fdr(value, label) {
        const level = Math.max(1, Math.min(5, Math.round(Number(value) || 3)));
        return h("span", { class: `fdr fdr-${level}`, title: `Fixture difficulty ${value ?? "unknown"}` }, label);
    },

    fixtures(fixtures) {
        if (!fixtures || !fixtures.length) return h("span", { class: "muted" }, "Blank");
        return h("span", { class: "row", style: { gap: "4px" } }, fixtures.map((fixture) => {
            const text = `${fixture.opponent ?? "?"} (${fixture.home ? "H" : "A"})`;
            return fixture.difficulty ? ui.fdr(fixture.difficulty, text) : h("span", { class: "chip" }, text);
        }));
    },

    availability(row) {
        const factor = row.availability_factor;
        if (!utils.isNumber(factor)) return h("span", { class: "muted" }, "–");
        const kind = factor >= 1 ? "good" : factor >= 0.5 ? "warning" : factor > 0 ? "serious" : "critical";
        return h("span", { title: row.news || null }, statusBadge(kind, utils.fmtPct(factor)));
    },

    timeline(events) {
        if (!events || !events.length) return h("p", { class: "empty" }, "No predictions or admin actions recorded since this instance started.");
        return h("ul", { class: "timeline" }, events.map((event) => {
            const failed = event.status === "failed";
            const item = h("li", { class: event.type === "admin" ? "admin" : failed ? "failed" : null });
            item.append(h("div", { class: "when" }, `${utils.dateTime(event.at)} · ${utils.timeAgo(event.at)}`));
            if (event.type === "admin") {
                const details = Object.entries(event).filter(([key]) => !["type", "at", "action"].includes(key)).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join(", ");
                item.append(h("div", null, h("strong", null, `Admin · ${event.action}`), details ? h("span", { class: "muted" }, ` — ${details}`) : null));
                return item;
            }
            item.append(h("div", null,
                h("strong", null, `GW${event.gameweek} prediction`), " ",
                failed ? statusBadge("critical", "failed") : statusBadge("good", "ok"), " ",
                h("span", { class: "muted" }, `trigger: ${event.trigger} · ${utils.fmtMs(event.duration_ms)}`)));
            if (failed) {
                item.append(h("div", { class: "error-text small" }, event.error));
                return item;
            }
            item.append(h("div", { class: "small secondary" },
                `${(event.successful_models || []).length} models ok`,
                Object.keys(event.failed_models || {}).length ? ` · ${Object.keys(event.failed_models).length} failed` : "",
                (event.disabled_models || []).length ? ` · disabled: ${event.disabled_models.join(", ")}` : "",
                ` · spread ${utils.fmt(event.mean_model_spread, 3)} · enrichment ${event.enrichment_status || "–"} · archive ${event.archive_status || "–"}`));
            const changes = event.changes;
            if (changes) {
                const moves = [];
                (changes.squad_in || []).forEach((player) => moves.push(`IN ${player.web_name} (${player.team_name}) — ${player.reason}`));
                (changes.squad_out || []).forEach((player) => moves.push(`OUT ${player.web_name} (${player.team_name}) — ${player.reason}`));
                if (changes.captain_changed && changes.captain_after) moves.push(`Captain → ${changes.captain_after.web_name} (was ${changes.captain_before ? changes.captain_before.web_name : "–"})`);
                if (!moves.length) moves.push(`Squad unchanged; projected total ${utils.fmt(changes.projected_total_before, 2)} → ${utils.fmt(changes.projected_total_after, 2)}`);
                (changes.top_movers || []).slice(0, 3).forEach((player) => moves.push(`${player.web_name}: ${utils.fmtSigned(player.delta, 2)} (${player.reason})`));
                item.append(h("ul", { class: "moves" }, moves.map((move) => h("li", null, move))));
            }
            return item;
        }));
    },

    /* Player search against the public /api/fpl/players route. */
    playerSearch(onPick, placeholder = "Search players…") {
        const input = h("input", { type: "search", placeholder, "aria-label": placeholder, autocomplete: "off" });
        const results = h("div", { class: "chips", style: { marginTop: "6px" } });
        const run = utils.debounce(async () => {
            const term = input.value.trim();
            clear(results);
            if (term.length < 2) return;
            try {
                const data = await api.get("/api/fpl/players", { search: term, limit: 8, order_by: "total_points", descending: true });
                (data.results || []).forEach((player) => {
                    results.append(h("button", {
                        class: "btn small", type: "button",
                        onClick: () => { onPick(player); input.value = ""; clear(results); },
                    }, `${player.web_name} · ${player.team_name} · ${utils.positionName(player.element_type)} · £${utils.fmt(player.price, 1)}`));
                });
                if (!(data.results || []).length) results.append(h("span", { class: "muted small" }, "No match"));
            } catch (error) {
                results.append(h("span", { class: "error-text small" }, error.message));
            }
        }, 250);
        input.addEventListener("input", run);
        return h("div", null, input, results);
    },
};

function formatCell(value, column) {
    if (value === null || value === undefined || value === "") return "–";
    if (typeof value === "number") return column.digits !== undefined ? utils.fmt(value, column.digits) : Number.isInteger(value) ? utils.fmtInt(value) : utils.fmt(value, 2);
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
}

/* ------------------------------------------------------------------ */
/* App state and shell                                                 */
/* ------------------------------------------------------------------ */

const state = {
    tab: "command",
    gameweek: null,
    events: [],
    eventsById: {},
    overview: null,
    renderToken: 0,
    cache: {},
    timers: [],
};

const cache = {
    async get(key, loader, ttlMs = 60000) {
        const entry = state.cache[key];
        if (entry && Date.now() - entry.at < ttlMs) return entry.value;
        const value = await loader();
        state.cache[key] = { value, at: Date.now() };
        return value;
    },
    drop(prefix) {
        Object.keys(state.cache).forEach((key) => {
            if (!prefix || key.startsWith(prefix)) delete state.cache[key];
        });
    },
};

const data = {
    gameweekQuery() {
        return state.gameweek ? { gameweek: state.gameweek } : {};
    },
    overview() {
        return cache.get("overview", () => api.get("/api/admin/overview"), 15000);
    },
    predictions() {
        return cache.get(`predictions:${state.gameweek}`, () => api.get("/api/admin/predictions", data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs }), 60000);
    },
    models() {
        return cache.get(`models:${state.gameweek}`, () => api.get("/api/admin/models", data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs }), 60000);
    },
    teams() {
        return cache.get("teams", () => api.get("/api/fpl/teams"), 600000);
    },
    setPieces() {
        return cache.get("set-pieces", () => api.get("/api/fpl/set-piece-notes"), 600000);
    },
    regions() {
        return cache.get("regions", () => api.get("/api/fpl/regions"), 3600000);
    },
};

const app = {
    async start() {
        api.onUnauthorized = () => app.showLogin("Your token was rejected. Sign in again.");
        document.getElementById("login-form").addEventListener("submit", app.onLogin);
        document.getElementById("logout-button").addEventListener("click", () => {
            auth.clear();
            app.showLogin();
        });
        document.getElementById("refresh-button").addEventListener("click", () => {
            cache.drop();
            app.render();
        });
        document.getElementById("gw-select").addEventListener("change", (event) => {
            state.gameweek = Number(event.target.value) || null;
            app.updateDeadline();
            app.render();
        });
        document.querySelectorAll("[data-tab]").forEach((button) => {
            button.addEventListener("click", () => app.selectTab(button.dataset.tab));
        });
        drawer.bind();
        if (auth.load()) {
            await app.enter();
        } else {
            app.showLogin();
        }
    },

    showLogin(message) {
        state.timers.forEach((timer) => clearInterval(timer));
        state.timers = [];
        cache.drop();
        document.getElementById("app").hidden = true;
        drawer.close();
        document.getElementById("login").hidden = false;
        const error = document.getElementById("login-error");
        error.hidden = !message;
        error.textContent = message || "";
        document.getElementById("login-token").focus();
    },

    async onLogin(event) {
        event.preventDefault();
        const token = document.getElementById("login-token").value;
        const remember = document.getElementById("login-remember").checked;
        auth.save(token, remember);
        document.getElementById("login-token").value = "";
        await app.enter();
    },

    async enter() {
        try {
            state.overview = await api.get("/api/admin/overview");
            state.cache.overview = { value: state.overview, at: Date.now() };
        } catch (error) {
            if (error.status === 401 || error.status === 403) {
                auth.clear();
                app.showLogin("That token was not accepted.");
            } else if (error.status === 404) {
                app.showLogin("The dashboard is turned off on this server (OPENFPL_ADMIN_ENABLED=false).");
            } else {
                app.showLogin(`Could not reach the server: ${error.message}`);
            }
            return;
        }
        document.getElementById("login").hidden = true;
        document.getElementById("app").hidden = false;
        document.getElementById("version-label").textContent = `v${state.overview.version || "?"}${state.overview.environment && state.overview.environment.revision ? ` · ${state.overview.environment.revision}` : ""}`;
        await app.loadGameweeks();
        app.updateHealth(state.overview);
        const saved = storage.get("localStorage", CONFIG.storageKeys.tab);
        app.selectTab(views[saved] ? saved : "command");
        state.timers.push(setInterval(app.updateDeadline, 30000));
        state.timers.push(setInterval(async () => {
            try {
                cache.drop("overview");
                app.updateHealth(await data.overview());
            } catch (error) {
                /* the next poll retries */
            }
        }, CONFIG.healthPollMs));
    },

    async loadGameweeks() {
        const select = document.getElementById("gw-select");
        try {
            const [official, playable] = await Promise.all([
                api.get("/api/fpl/gameweeks"),
                api.get("/api/gameweeks").catch(() => null),
            ]);
            state.events = official.results || [];
            state.eventsById = Object.fromEntries(state.events.map((event) => [event.id, event]));
            const playableSet = new Set((playable && playable.gameweeks) || []);
            const open = (state.overview.gameweeks && state.overview.gameweeks.open_gameweek) || (playable && playable.latest) || 1;
            state.gameweek = state.gameweek || open;
            clear(select);
            state.events.forEach((event) => {
                const flags = [event.is_current ? "current" : "", event.is_next ? "next" : "", event.finished ? "finished" : ""].filter(Boolean).join(", ");
                select.append(h("option", { value: event.id, selected: event.id === state.gameweek ? true : null },
                    `GW${event.id}${flags ? ` · ${flags}` : ""}${playableSet.size && !playableSet.has(event.id) ? " (future)" : ""}`));
            });
        } catch (error) {
            state.gameweek = state.gameweek || 1;
            clear(select).append(h("option", { value: state.gameweek }, `GW${state.gameweek}`));
            toast.show(`Could not load gameweeks: ${error.message}`, "error");
        }
        app.updateDeadline();
    },

    updateDeadline() {
        const node = document.getElementById("deadline");
        const event = state.eventsById[state.gameweek];
        if (!node) return;
        if (!event || !event.deadline_time) {
            node.textContent = "";
            return;
        }
        const remaining = (Date.parse(event.deadline_time) - Date.now()) / 1000;
        node.textContent = remaining > 0
            ? `Deadline in ${utils.fmtDuration(remaining)} · forecast live`
            : `Deadline passed ${utils.dateTime(event.deadline_time)} · forecast frozen`;
    },

    healthOf(overview) {
        if (!overview) return { kind: "neutral", label: "Unknown" };
        const problems = [];
        const models = overview.models || {};
        if (models.enabled < models.minimum_successful) problems.push("critical");
        const last = overview.last_inference;
        if (last && Object.keys(last.failed_models || {}).length) problems.push("warning");
        const requests = overview.requests || {};
        if (requests.total_requests > 20 && requests.server_errors / requests.total_requests > 0.05) problems.push("warning");
        const upstream = overview.upstream || {};
        if (upstream.last_error_at && Date.now() - Date.parse(upstream.last_error_at) < 600000) problems.push("warning");
        if ((overview.archive && overview.archive.last_result && overview.archive.last_result.status) === "failed") problems.push("warning");
        if (problems.includes("critical")) return { kind: "critical", label: "Problem" };
        if (problems.length) return { kind: "warning", label: "Degraded" };
        return { kind: "good", label: "Healthy" };
    },

    updateHealth(overview) {
        state.overview = overview;
        const pill = document.getElementById("health-pill");
        const health = app.healthOf(overview);
        pill.className = `pill ${health.kind}`;
        pill.textContent = health.label;
        pill.title = `Updated ${utils.timeAgo(overview.generated_at)}`;
    },

    setGameweek(gameweek) {
        state.gameweek = Number(gameweek) || state.gameweek;
        const select = typeof document !== "undefined" && document.getElementById ? document.getElementById("gw-select") : null;
        if (select) select.value = String(state.gameweek);
        app.updateDeadline();
        app.render();
    },

    selectTab(tab) {
        state.tab = views[tab] ? tab : "command";
        storage.set("localStorage", CONFIG.storageKeys.tab, state.tab);
        document.querySelectorAll("[data-tab]").forEach((button) => {
            button.setAttribute("aria-selected", button.dataset.tab === state.tab ? "true" : "false");
        });
        app.render();
    },

    render() {
        state.renderToken += 1;
        const token = state.renderToken;
        const main = clear(document.getElementById("main"));
        tooltip.hide();
        const view = views[state.tab];
        const context = { token, alive: () => token === state.renderToken };
        Promise.resolve(view.render(main, context)).catch((error) => {
            main.append(ui.errorBox(error, () => app.render()));
        });
    },

    /* Run a live control with a confirmation, then refresh what it changed. */
    async action(title, message, run, options = {}) {
        if (options.confirm !== false && !(await dialogs.confirm(title, message, options.okLabel || "Run"))) return null;
        try {
            const result = await run();
            toast.show(options.success ? options.success(result) : `${title}: done`, "success");
            cache.drop();
            if (options.rerender !== false) app.render();
            return result;
        } catch (error) {
            toast.show(`${title} failed: ${error.message}`, "error", 8000);
            return null;
        }
    },
};

function sectionTitle(title, subtitle, actions) {
    return h("div", { class: "section-title" },
        h("div", null, h("h1", null, title), subtitle ? h("p", null, subtitle) : null),
        actions ? h("div", { class: "btn-row" }, actions) : null);
}

function forecastChips(meta) {
    if (!meta) return null;
    const items = [
        meta.frozen ? chip(`Frozen · captured ${utils.dateTime(meta.forecast_captured_at)}${meta.freeze_method ? ` (${meta.freeze_method})` : ""}`, "aqua") : chip("Live forecast", "lime"),
        chip(`Strategy: ${meta.strategy}`),
        chip(`Source: ${meta.source}`),
    ];
    if (meta.refresh_due) items.push(chip("Refresh due"));
    if ((meta.successful_models || []).length) items.push(chip(`${meta.successful_models.length} models ok`));
    const failed = Object.keys(meta.failed_models || {});
    if (failed.length) items.push(h("span", { title: JSON.stringify(meta.failed_models) }, statusBadge("serious", `${failed.length} failed: ${failed.join(", ")}`)));
    if ((meta.disabled_models || []).length) items.push(chip(`Disabled: ${meta.disabled_models.join(", ")}`));
    if (meta.enrichment && meta.enrichment.status) items.push(chip(`Enrichment: ${meta.enrichment.status}`));
    if (meta.inputs) items.push(chip(`Inputs: ${meta.inputs.origin}${meta.inputs.approximate ? " (rebuilt)" : ""}`));
    return h("div", { class: "chips" }, items);
}

/* ------------------------------------------------------------------ */
/* Player explanation drawer                                           */
/* ------------------------------------------------------------------ */

const drawer = {
    current: null,
    bind() {
        document.getElementById("drawer-close").addEventListener("click", drawer.close);
        document.getElementById("drawer-backdrop").addEventListener("click", drawer.close);
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !document.getElementById("drawer").hidden) drawer.close();
        });
    },
    close() {
        const node = document.getElementById("drawer");
        if (!node) return;
        node.hidden = true;
        document.getElementById("drawer-backdrop").hidden = true;
        tooltip.hide();
        drawer.current = null;
    },

    async open(playerId, label) {
        const node = document.getElementById("drawer");
        const body = clear(document.getElementById("drawer-body"));
        document.getElementById("drawer-title").textContent = label || `Player ${playerId}`;
        node.hidden = false;
        document.getElementById("drawer-backdrop").hidden = false;
        drawer.current = playerId;
        const alive = () => drawer.current === playerId;

        const summary = ui.card("Why this projection", `GW${state.gameweek} · re-run of the live ensemble on the model's inputs`);
        const contributions = ui.card("What moves his projection", "Each input set to a typical player's value in his position, one at a time");
        const inputs = ui.card("Inputs vs a typical player", "5-match averages the models saw");
        const models = ui.card("Model view", "Each model's projection and the fixture sensitivity");
        const history = ui.card("Season so far", "/api/fpl/players/{id}/history");
        const official = ui.card("Official status", "/api/fpl/players/{id} · set-piece notes");
        [summary, contributions, inputs, models, history, official].forEach((card) => body.append(card.root));

        const explainPromise = api.get(`/api/admin/players/${playerId}/explain`, data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs });
        ui.fill(summary, async (target) => {
            const explanation = await explainPromise;
            if (!alive()) return;
            document.getElementById("drawer-title").textContent = `${explanation.web_name} · ${explanation.team_name || ""} · ${explanation.position || ""}`;
            clear(target);
            target.append(h("div", { class: "headline" },
                h("span", { class: "big" }, utils.fmt(explanation.expected_points, 2)),
                h("span", { class: "secondary" }, "projected points"),
                explanation.served_points !== undefined && explanation.served_points !== null ? chip(`served ${utils.fmt(explanation.served_points, 2)}`) : null,
                explanation.role ? chip(explanation.role === "captain" ? "AI captain" : "AI vice-captain", "lime") : null,
                explanation.decision && explanation.decision.status ? chip(explanation.decision.status.replace(/-/g, " ")) : null));
            target.append(h("ol", { class: "reasons" }, (explanation.reasons || []).map((reason) => h("li", null, reason))));
            (explanation.notes || []).forEach((note) => target.append(h("p", { class: "notes" }, note)));
            const steps = (explanation.waterfall || []).filter((step) => step.kind !== "delta" || Math.abs(step.value) >= 0.005);
            if (steps.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 0" } }, "From a typical player to his projection"));
                charts.waterfall(target, steps);
            }
        });
        ui.fill(contributions, async (target) => {
            const explanation = await explainPromise;
            if (!alive()) return;
            clear(target);
            const groups = explanation.groups || [];
            if (groups.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "0 0 4px" } }, "By group"));
                charts.hbars(target, groups.map((group) => ({ label: group.group, value: group.contribution })), { diverging: true, format: (value) => utils.fmtSigned(value, 2), valueLabel: "Points" });
            }
            const top = (explanation.contributions || []).slice(0, 12);
            if (top.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Top inputs"));
                charts.hbars(target, top.map((item) => ({
                    label: item.label,
                    value: item.contribution,
                    tooltip: [
                        { label: "Change", value: utils.fmtSigned(item.contribution, 2) },
                        { label: "His value", value: utils.fmt(item.value, 2) },
                        { label: "Typical", value: utils.fmt(item.baseline, 2) },
                        { label: "Percentile", value: utils.isNumber(item.percentile) ? `${utils.fmt(item.percentile, 0)}th` : "–" },
                    ],
                })), { diverging: true, format: (value) => utils.fmtSigned(value, 2) });
            } else {
                charts.empty(target, "No model contributions (blank gameweek or GW1 cold start).");
            }
        });
        ui.fill(inputs, async (target) => {
            const explanation = await explainPromise;
            if (!alive()) return;
            clear(target);
            const rows = [...(explanation.contributions || [])].sort((a, b) => String(a.group).localeCompare(String(b.group)));
            target.append(ui.table(rows, [
                { key: "label", label: "Input" },
                { key: "group", label: "Group" },
                { key: "value", label: "His value", num: true, digits: 2 },
                { key: "baseline", label: "Typical", num: true, digits: 2 },
                { key: "percentile", label: "Percentile", num: true, render: (row) => utils.isNumber(row.percentile) ? h("div", { class: "row", style: { justifyContent: "flex-end" } }, h("div", { class: "meter", style: { width: "60px" } }, h("span", { style: { width: `${row.percentile}%` } })), `${utils.fmt(row.percentile, 0)}`) : "–" },
                { key: "contribution", label: "Effect", num: true, render: (row) => utils.fmtSigned(row.contribution, 2) },
            ], { sortKey: "contribution", footer: false }));
        });
        ui.fill(models, async (target) => {
            const explanation = await explainPromise;
            if (!alive()) return;
            clear(target);
            const names = Object.keys(explanation.models || {});
            if (names.length) {
                charts.hbars(target, names.map((name, index) => ({
                    label: `${name} (w ${utils.fmt(explanation.models[name].weight, 2)})`,
                    value: explanation.models[name].expected_points,
                    color: CONFIG.series[index % CONFIG.series.length],
                })), { format: (value) => utils.fmt(value, 2), valueLabel: "Projection" });
                target.append(h("p", { class: "small muted" }, `Model spread ${utils.fmt(explanation.model_spread, 2)} points.`));
            }
            const sensitivity = explanation.fixture_sensitivity || [];
            if (sensitivity.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, `Every possible fixture (his: #${explanation.fixture_rank} of ${explanation.fixture_options})`));
                const actual = (explanation.fixtures || [])[0] || {};
                charts.hbars(target, sensitivity.map((item) => ({
                    label: `${item.opponent} (${item.home ? "H" : "A"})`,
                    value: item.points,
                    color: item.opponent === actual.opponent && Boolean(item.home) === Boolean(actual.home) ? CONFIG.highlight : CONFIG.series[0],
                })), { barHeight: 12, gap: 4, format: (value) => utils.fmt(value, 2), valueLabel: "Match points" });
            }
        });
        ui.fill(history, async (target) => {
            const summaryData = await api.get(`/api/fpl/players/${playerId}/history`);
            if (!alive()) return;
            const payload = summaryData.data || {};
            const rows = payload.history || [];
            clear(target);
            const byGw = {};
            rows.forEach((row) => {
                const entry = byGw[row.gameweek] || { x: row.gameweek, points: 0, xg: 0, xa: 0, minutes: 0 };
                entry.points += row.total_points || 0;
                entry.xg += row.expected_goals || 0;
                entry.xa += row.expected_assists || 0;
                entry.minutes += row.minutes || 0;
                byGw[row.gameweek] = entry;
            });
            const points = Object.values(byGw).sort((a, b) => a.x - b.x);
            if (points.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "0 0 4px" } }, "Points per gameweek"));
                charts.columns(target, points.map((point) => ({ label: `${point.x}`, title: `GW${point.x}`, value: point.points })), { height: 160, valueLabel: "Points" });
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Expected goals and assists per gameweek"));
                charts.lines(target, [
                    { name: "xG", color: CONFIG.series[0], points: points.map((point) => ({ x: point.x, y: point.xg })) },
                    { name: "xA", color: CONFIG.series[1], points: points.map((point) => ({ x: point.x, y: point.xa })) },
                ], { height: 180, yMin: 0 });
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Minutes per gameweek"));
                charts.columns(target, points.map((point) => ({ label: `${point.x}`, title: `GW${point.x}`, value: point.minutes })), { height: 140, color: CONFIG.series[2], valueLabel: "Minutes" });
            } else {
                charts.empty(target, "No matches played this season yet.");
            }
            const upcoming = (payload.upcoming_fixtures || []).slice(0, 6);
            if (upcoming.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Next fixtures"));
                target.append(h("div", { class: "row" }, upcoming.map((fixture) => ui.fdr(fixture.difficulty, `GW${fixture.gameweek ?? "?"} ${fixture.opponent_team_name} (${fixture.was_home ? "H" : "A"})`))));
            }
            const past = payload.past_seasons || [];
            if (past.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Past seasons"));
                target.append(ui.table(past.slice(-6).reverse(), [
                    { key: "season_name", label: "Season" },
                    { key: "total_points", label: "Pts", num: true },
                    { key: "minutes", label: "Min", num: true },
                    { key: "goals", label: "G", num: true },
                    { key: "assists", label: "A", num: true },
                    { key: "end_price", label: "£ end", num: true, digits: 1 },
                ], { footer: false }));
            }
        });
        ui.fill(official, async (target) => {
            const [player, notes] = await Promise.all([
                api.get(`/api/fpl/players/${playerId}`),
                data.setPieces().catch(() => null),
            ]);
            if (!alive()) return;
            const record = player.data || {};
            clear(target);
            target.append(ui.kv([
                ["Name", `${record.first_name || ""} ${record.second_name || ""}`.trim() || record.web_name],
                ["Club", record.team_name],
                ["Position", record.position],
                ["Price", utils.isNumber(record.price) ? `£${utils.fmt(record.price, 1)}m` : "–"],
                ["Ownership", utils.isNumber(record.selected_by_percent) ? `${utils.fmt(record.selected_by_percent, 1)}%` : "–"],
                ["Status", record.status],
                ["Chance next round", utils.isNumber(record.chance_of_playing_next_round) ? `${record.chance_of_playing_next_round}%` : "–"],
                ["News", record.news || "None"],
                ["Season points", utils.fmtInt(record.total_points)],
                ["Season minutes · xG · xA", `${utils.fmtInt(record.minutes)} · ${utils.fmt(record.expected_goals, 2)} · ${utils.fmt(record.expected_assists, 2)}`],
            ]));
            const teamNotes = ((notes && notes.data && notes.data.teams) || []).find((team) => Number(team.id) === Number(record.team_id));
            const lines = teamNotes && (teamNotes.notes || []).map((note) => note.info_message).filter(Boolean);
            if (lines && lines.length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Club set-piece notes"));
                target.append(h("ul", { class: "reasons" }, lines.map((line) => h("li", null, line))));
            }
        });
    },
};

/* ------------------------------------------------------------------ */
/* Views                                                               */
/* ------------------------------------------------------------------ */

const views = {};

/* ---------- Command centre ---------- */
views.command = {
    async render(main, context) {
        main.append(sectionTitle("Command centre", `GW${state.gameweek} · everything at a glance, plus live controls`));
        const tilesHolder = h("div");
        main.append(tilesHolder);
        const grid = h("div", { class: "grid" });
        main.append(grid);

        const controls = ui.card("Live controls", "These change the running service until it restarts. Each one asks first.", { span: 5 });
        const snapshot = ui.card("AI squad snapshot", "GET /api/scout — exactly what the public page receives", { span: 7 });
        const log = ui.card("AI decision log", "Every prediction run, what changed in the squad and why", { span: 7 });
        const eventStatus = ui.card("Official processing", "GET /api/fpl/gameweeks/status", { span: 5 });
        const cacheCard = ui.card("Forecast cache", "Live forecasts held in memory per gameweek", { span: 5 });
        const problems = ui.card("Recent problems", "Warnings, errors and failed requests on this instance", { span: 7 });
        [controls, snapshot, log, eventStatus, cacheCard, problems].forEach((card) => grid.append(card.root));

        const overview = await data.overview();
        if (!context.alive()) return;
        app.updateHealth(overview);
        tilesHolder.append(views.command.tiles(overview));
        views.command.controls(controls.body);

        clear(log.body).append(ui.timeline(overview.timeline));
        ui.fill(cacheCard, async (target) => {
            clear(target).append(ui.table(overview.live_forecasts || [], [
                { key: "gameweek", label: "GW", num: true },
                { key: "fresh", label: "State", render: (row) => row.pending ? statusBadge("neutral", "predicting") : row.fresh ? statusBadge("good", "fresh") : statusBadge("warning", "stale") },
                { key: "age_seconds", label: "Age", num: true, render: (row) => utils.fmtDuration(row.age_seconds) },
                { key: "fresh_for_seconds", label: "Fresh for", num: true, render: (row) => utils.fmtDuration(row.fresh_for_seconds) },
                { key: "stale_for_seconds", label: "Servable for", num: true, render: (row) => utils.fmtDuration(row.stale_for_seconds) },
                { key: "restored", label: "Restored", render: (row) => (row.restored ? "yes" : "no") },
            ], { footer: false, empty: "No live forecast cached (closed gameweeks are served frozen from the archive)." }));
        });
        ui.fill(problems, async (target) => {
            clear(target);
            const records = (overview.logs && overview.logs.recent_problems) || [];
            const errors = (overview.requests && overview.requests.recent_errors) || [];
            if (!records.length && !errors.length) {
                target.append(statusBadge("good", "No warnings or failed requests recorded."));
                return;
            }
            records.forEach((record) => target.append(h("div", { class: "stack", style: { marginBottom: "8px" } },
                h("div", null, statusBadge(record.level === "WARNING" ? "warning" : "critical", record.level), " ", h("span", { class: "muted small" }, `${utils.timeAgo(record.time)} · ${record.logger}`)),
                h("div", { class: "small secondary mono" }, record.message))));
            if (errors.length) {
                target.append(ui.table(errors, [
                    { key: "time", label: "When", render: (row) => utils.timeAgo(row.time) },
                    { key: "method", label: "Method" },
                    { key: "path", label: "Path" },
                    { key: "status", label: "Status", num: true },
                    { key: "duration_ms", label: "Time", num: true, render: (row) => utils.fmtMs(row.duration_ms) },
                ], { footer: false }));
            }
        });
        ui.fill(eventStatus, async (target) => {
            const status = await api.get("/api/fpl/gameweeks/status");
            const payload = status.data || {};
            clear(target);
            target.append(ui.kv([["Leagues", payload.leagues || "–"]]));
            target.append(ui.table(payload.status || [], [
                { key: "date", label: "Date" },
                { key: "event", label: "GW", num: true },
                { key: "bonus_added", label: "Bonus added", render: (row) => (row.bonus_added ? statusBadge("good", "yes") : statusBadge("neutral", "not yet")) },
                { key: "points", label: "Points state", title: "Official code: l = live, r = results final", render: (row) => ({ l: "live", r: "final" })[row.points] || row.points || "–" },
            ], { footer: false, empty: "No processing status published." }));
        });
        ui.fill(snapshot, async (target) => {
            const squad = await api.get("/api/scout", data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs, useAuth: false });
            if (!context.alive()) return;
            clear(target);
            target.append(forecastChips({ ...squad, successful_models: [], failed_models: {} }));
            if (squad.refresh_due) {
                target.append(h("p", { class: "notes" }, "The public page received an older forecast and will ask for a refresh.",
                    " ", h("button", { class: "btn small", type: "button", onClick: () => views.command.refresh() }, "Refresh now")));
            }
            const team = squad.scout_team || [];
            const total = utils.sum(team.map((player) => player.expected_points));
            target.append(h("p", { class: "small muted" }, `${team.length} players · squad total ${utils.fmt(total, 2)} projected points · v${squad.version}`));
            target.append(ui.table(team, [
                { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.role ? " " : null, row.role ? chip(row.role === "captain" ? "C" : "V", "lime") : null, h("span", { class: "sub" }, `${row.team_name} · ${utils.positionName(row.element_type)}`)) },
                { key: "opponent_team_name", label: "Fixture", render: (row) => `${row.opponent_team_name ?? "–"}${row.was_home === true ? " (H)" : row.was_home === false ? " (A)" : ""}` },
                { key: "expected_points", label: "xP", num: true, digits: 2 },
                { key: "availability_factor", label: "Avail.", render: ui.availability },
                { key: "price", label: "£", num: true, digits: 1 },
            ], { onRowClick: (row) => drawer.open(row.id, row.web_name), footer: false, sortKey: "expected_points" }));
        });
    },

    tiles(overview) {
        const last = overview.last_inference || {};
        const process = overview.process || {};
        const models = overview.models || {};
        const requests = overview.requests || {};
        const upstream = overview.upstream || {};
        const enrichment = (overview.enrichment && overview.enrichment.last_result) || {};
        const archive = (overview.archive && overview.archive.last_result) || {};
        const health = app.healthOf(overview);
        const gameweeks = overview.gameweeks || {};
        const next = gameweeks.next || gameweeks.current || {};
        return ui.tiles([
            { label: "Service", value: health.label, badge: statusBadge(health.kind, health.kind === "good" ? "all checks pass" : "see below"), detail: `${overview.environment && overview.environment.production ? "production" : "development"} · v${overview.version}` },
            { label: "Uptime", value: utils.fmtDuration(process.uptime_seconds), detail: `pid ${process.pid} · Python ${process.python}` },
            { label: "Memory", value: utils.fmtBytes(process.rss_bytes), detail: `peak ${utils.fmtBytes(process.peak_rss_bytes)} · ${process.threads} threads` },
            { label: "Models", value: `${models.enabled}/${models.loaded} on`, detail: `${models.minimum_successful} must succeed${models.overrides_active ? " · overrides active" : ""}` },
            { label: "Last prediction", value: last.at ? utils.timeAgo(last.at) : "none yet", detail: last.at ? `GW${last.gameweek} · ${utils.fmtMs(last.duration_ms)} · spread ${utils.fmt(last.mean_model_spread, 2)}` : "Runs on the first squad request" },
            { label: "Next deadline", value: next.deadline_time ? utils.fmtDuration((Date.parse(next.deadline_time) - Date.now()) / 1000) : "–", detail: next.id ? `GW${next.id} · ${utils.dateTime(next.deadline_time)}` : "" },
            { label: "FPL Data enrichment", value: enrichment.status || "–", detail: overview.enrichment && overview.enrichment.enabled ? "enabled" : "disabled" },
            { label: "Archive", value: archive.status || "–", detail: overview.archive && overview.archive.enabled ? `…/${String(overview.archive.root_path || "").split("/").slice(-2).join("/")}` : "disabled" },
            { label: "API traffic", value: utils.fmtCompact(requests.total_requests), detail: `${utils.fmtInt(requests.server_errors)} server errors · ${utils.fmtInt(requests.client_errors)} rejected · p95 ${utils.fmtMs(requests.latency_ms && requests.latency_ms.p95)}` },
            { label: "Official FPL calls", value: utils.fmtCompact(upstream.requests), detail: `cache hits ${utils.fmtPct(upstream.cache_hit_rate)} · failures ${utils.fmtInt(upstream.failures)}` },
        ]);
    },

    controls(target) {
        const gw = state.gameweek;
        const frozen = (() => {
            const event = state.eventsById[gw];
            return event && event.deadline_time && Date.parse(event.deadline_time) < Date.now();
        })();
        const button = (label, description, handler, kind = "") => h("div", { class: "stack", style: { gap: "4px", marginBottom: "12px" } },
            h("div", null, h("button", { class: `btn ${kind}`, type: "button", onClick: handler }, label)),
            h("p", { class: "small muted", style: { margin: 0 } }, description));
        add(clear(target),
            button("Refresh forecast", `POST /api/scout/refresh — predicts GW${gw} again only if its cached forecast is stale (what the public page does).`, () => views.command.refresh()),
            button("Re-predict now", frozen ? `GW${gw} is frozen; its forecast cannot change.` : `POST /api/admin/predictions/recompute — drops GW${gw}'s cached forecast and predicts it now. The decision log shows what changed.`, () => app.action("Re-predict now", `Run the four-model ensemble for GW${gw} again now? The public squad updates immediately.`, () => api.post("/api/admin/predictions/recompute", undefined, { gameweek: gw }, { timeoutMs: CONFIG.slowTimeoutMs }), { success: (result) => `GW${result.gameweek} predicted again` }), "primary"),
            button("Sync results", "POST /api/scout/sync-results — collect official results into the archive (5-minute cooldown).", () => app.action("Sync results", "Collect official results now (respects the cooldown)?", () => api.post("/api/scout/sync-results"), { confirm: false, success: (result) => `Results sync: ${result.status}` })),
            button("Force results collection", "POST /api/admin/archive/collect?force=true — the same, skipping the cooldown.", () => app.action("Force results collection", "Collect official results now, skipping the cooldown?", () => api.post("/api/admin/archive/collect", undefined, { force: true }, { timeoutMs: CONFIG.slowTimeoutMs }), { success: (result) => `Collection: ${result.status}${result.files_updated ? ` · ${result.files_updated.length} files` : ""}` })),
            button("Clear caches", "POST /api/admin/cache/clear — empties the official FPL response cache, fixture cache and live forecasts. The next request refetches everything.", () => app.action("Clear caches", "Clear every cache? The next squad request refetches official data and predicts again.", () => api.post("/api/admin/cache/clear", { official: true, fixtures: true, forecasts: true }), { okLabel: "Clear", success: (result) => `Cleared ${result.official_entries_cleared ?? 0} official entries, ${result.forecasts_invalidated ?? 0} forecasts` }), "danger"),
        );
    },

    async refresh() {
        await app.action("Refresh forecast", "", () => api.post("/api/scout/refresh", undefined, data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs, useAuth: false }), { confirm: false, success: (result) => `Refresh: ${result.status}` });
    },
};

/* ---------- Predictions ---------- */
views.predictions = {
    filters: { search: "", position: "", team: "", availability: "", squadOnly: false, maxPrice: "", groups: { models: true, inputs: true, market: true } },

    async render(main, context) {
        main.append(sectionTitle("AI predictions", `Every player's GW${state.gameweek} projection, each model's view and the inputs behind it`));
        const metaHolder = h("div");
        main.append(metaHolder);
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const tableCard = ui.card("All players", "Click a player for the full explanation. Lime bar = in the AI squad.");
        const scatterCard = ui.card("Projection against price", "AI squad picks highlighted; click a dot to explain it", { span: 6 });
        const topCard = ui.card("Top 20 projections", "Bar = ensemble · white line = range across the models", { span: 6 });
        const checkCard = ui.card("Cross-checks and export", "Compare with the service's own endpoints", { span: 12 });
        [tableCard, scatterCard, topCard, checkCard].forEach((card) => grid.append(card.root));

        const payload = await ui.fill(tableCard, async () => data.predictions());
        if (!payload || !context.alive()) return;
        metaHolder.append(forecastChips(payload));
        if (payload.inputs_error) metaHolder.append(h("p", { class: "notes" }, `Model inputs unavailable: ${payload.inputs_error}`));
        views.predictions.table(tableCard.body, payload);
        views.predictions.charts(scatterCard.body, topCard.body, payload);
        views.predictions.checks(checkCard.body, payload);
    },

    filtered(payload) {
        const filters = views.predictions.filters;
        const needle = filters.search.trim().toLowerCase();
        return payload.players.filter((row) => {
            if (needle && !`${row.web_name} ${row.team_name}`.toLowerCase().includes(needle)) return false;
            if (filters.position && row.position !== filters.position) return false;
            if (filters.team && row.team_name !== filters.team) return false;
            if (filters.squadOnly && !row.in_squad) return false;
            if (filters.maxPrice && utils.isNumber(row.price) && row.price > Number(filters.maxPrice)) return false;
            if (filters.availability) {
                const factor = row.availability_factor;
                if (filters.availability === "available" && !(factor >= 1)) return false;
                if (filters.availability === "doubtful" && !(factor > 0 && factor < 1)) return false;
                if (filters.availability === "out" && !(factor === 0)) return false;
            }
            return true;
        });
    },

    columns(payload) {
        const groups = views.predictions.filters.groups;
        const labels = payload.feature_labels || {};
        const columns = [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.role ? " " : null, row.role ? chip(row.role === "captain" ? "C" : "V", "lime") : null, h("span", { class: "sub" }, `${row.team_name} · ${row.position}`)) },
            { key: "opponent", label: "Fixture", render: (row) => ui.fixtures(row.fixtures && row.fixtures.length ? row.fixtures : row.opponent ? [{ opponent: row.opponent, home: row.was_home }] : []) },
            { key: "expected_points", label: "xP", num: true, digits: 2, title: "Projected points (ensemble × availability)" },
            { key: "availability_factor", label: "Avail.", render: ui.availability, value: (row) => row.availability_factor },
        ];
        if (groups.models) {
            (payload.models || []).forEach((name, index) => columns.push({
                key: `model:${name}`, label: name, num: true, digits: 2,
                value: (row) => (row.models || {})[name] ?? null,
                render: (row) => h("span", null, h("i", { class: "model-key", style: { background: CONFIG.series[index % CONFIG.series.length] } }), utils.fmt((row.models || {})[name], 2)),
            }));
            columns.push({ key: "model_spread", label: "Spread", num: true, digits: 2, title: "Standard deviation across models" });
        }
        if (groups.inputs) {
            (payload.table_features || []).forEach((feature) => columns.push({
                key: `f:${feature}`, label: labels[feature] || feature, num: true, digits: 2,
                title: `5-match average of ${labels[feature] || feature}`,
                value: (row) => (row.features || {})[feature] ?? null,
            }));
        }
        if (groups.market) {
            columns.push(
                { key: "price", label: "£", num: true, digits: 1 },
                { key: "value", label: "xP/£", num: true, digits: 2 },
                { key: "selected_by_percent", label: "Own %", num: true, digits: 1 },
                { key: "total_points", label: "Season pts", num: true },
                { key: "news", label: "News", render: (row) => row.news ? h("span", { class: "small secondary" }, row.news) : "" },
            );
        }
        columns.push({ key: "decision", label: "AI decision", render: (row) => row.decision ? h("span", { title: row.decision_reason || "" }, row.decision.replace(/-/g, " ")) : "–" });
        return columns;
    },

    table(target, payload) {
        clear(target);
        const filters = views.predictions.filters;
        const teams = [...new Set(payload.players.map((row) => row.team_name).filter(Boolean))].sort();
        const holder = h("div");
        const rerender = () => {
            clear(holder).append(ui.table(views.predictions.filtered(payload), views.predictions.columns(payload), {
                sortKey: "expected_points", pageSize: CONFIG.pageSize, tall: true,
                onRowClick: (row) => drawer.open(row.id, row.web_name),
                rowClass: (row) => (row.in_squad ? "in-squad" : ""),
            }));
        };
        const select = (options, value, onChange, label) => {
            const element = h("select", { "aria-label": label }, options.map(([optionValue, text]) => h("option", { value: optionValue, selected: optionValue === value ? true : null }, text)));
            element.addEventListener("change", () => { onChange(element.value); rerender(); });
            return element;
        };
        const search = h("input", { type: "search", placeholder: "Search player or club", value: filters.search, "aria-label": "Search" });
        search.addEventListener("input", utils.debounce(() => { filters.search = search.value; rerender(); }, 200));
        const price = h("input", { type: "number", min: 3, max: 16, step: 0.5, placeholder: "Max £", value: filters.maxPrice, style: { width: "90px" }, "aria-label": "Maximum price" });
        price.addEventListener("input", utils.debounce(() => { filters.maxPrice = price.value; rerender(); }, 250));
        const toggle = (label, checked, onChange) => {
            const input = h("input", { type: "checkbox", checked: checked ? true : null });
            input.addEventListener("change", () => { onChange(input.checked); rerender(); });
            return h("label", { class: "check small" }, input, label);
        };
        target.append(h("div", { class: "filters" },
            search,
            select([["", "All positions"], ["GK", "GK"], ["DEF", "DEF"], ["MID", "MID"], ["FWD", "FWD"]], filters.position, (value) => { filters.position = value; }, "Position"),
            select([["", "All clubs"], ...teams.map((team) => [team, team])], filters.team, (value) => { filters.team = value; }, "Club"),
            select([["", "Any availability"], ["available", "Available"], ["doubtful", "Doubtful"], ["out", "Out"]], filters.availability, (value) => { filters.availability = value; }, "Availability"),
            price,
            toggle("AI squad only", filters.squadOnly, (value) => { filters.squadOnly = value; }),
            h("span", { class: "spacer" }),
            toggle("Models", filters.groups.models, (value) => { filters.groups.models = value; }),
            toggle("Inputs", filters.groups.inputs, (value) => { filters.groups.inputs = value; }),
            toggle("Market", filters.groups.market, (value) => { filters.groups.market = value; }),
        ), holder);
        rerender();
    },

    charts(scatterTarget, topTarget, payload) {
        clear(scatterTarget);
        charts.scatter(scatterTarget, payload.players.filter((row) => utils.isNumber(row.price)).map((row) => ({
            x: row.price, y: row.expected_points, label: `${row.web_name} (${row.team_name})`, id: row.id,
            highlight: row.in_squad, annotate: row.role === "captain",
            extra: [{ label: "Position", value: row.position }],
        })), {
            xLabel: "Price (£m)", yLabel: "Projected points",
            legend: [{ label: "AI squad", color: CONFIG.series[0] }, { label: "Other players", color: CONFIG.muted }],
            onClick: (point) => drawer.open(point.id, point.label),
        });
        clear(topTarget);
        const top = payload.players.slice(0, 20);
        charts.hbars(topTarget, top.map((row) => {
            const values = Object.values(row.models || {}).filter(utils.isNumber);
            return {
                label: `${row.web_name} · ${row.position}`,
                value: row.expected_points,
                range: values.length > 1 ? [Math.min(...values), Math.max(...values)] : null,
                color: row.in_squad ? CONFIG.series[0] : CONFIG.muted,
                id: row.id,
                tooltip: [
                    { label: "Ensemble", value: utils.fmt(row.expected_points, 2) },
                    ...Object.entries(row.models || {}).map(([name, value], index) => ({ label: name, value: utils.fmt(value, 2), color: CONFIG.series[index % CONFIG.series.length] })),
                ],
            };
        }), { onClick: (item) => drawer.open(item.id, item.label), format: (value) => utils.fmt(value, 2) });
        topTarget.append(h("p", { class: "small muted" }, "Blue bars are AI squad picks; grey bars were left out (see the AI squad tab for why)."));
    },

    checks(target, payload) {
        clear(target);
        const output = h("div", { class: "stack", style: { marginTop: "10px" } });
        const verify = async () => {
            clear(output).append(h("span", { class: "muted" }, "Checking…"));
            const filters = views.predictions.filters;
            const positionCode = { GK: 1, DEF: 2, MID: 3, FWD: 4 }[filters.position];
            try {
                const response = await api.get("/api/gw/playerpoints", { gameweek: state.gameweek, element_type: positionCode, team_name: filters.team || undefined }, { timeoutMs: CONFIG.slowTimeoutMs });
                const mine = Object.fromEntries(payload.players.map((row) => [row.id, row.expected_points]));
                let maxDiff = 0;
                let compared = 0;
                (response.player_points || []).forEach((row) => {
                    if (mine[row.id] === undefined) return;
                    compared += 1;
                    maxDiff = Math.max(maxDiff, Math.abs((row.expected_points || 0) - (mine[row.id] || 0)));
                });
                clear(output).append(maxDiff < 0.001
                    ? statusBadge("good", `${compared} players compared with /api/gw/playerpoints — identical`)
                    : statusBadge("warning", `${compared} players compared — largest difference ${utils.fmt(maxDiff, 3)} (a newer forecast may have been made in between)`));
            } catch (error) {
                clear(output).append(ui.errorBox(error));
            }
        };
        const exportCsv = async () => {
            try {
                const response = await api.post("/api/scout", undefined, data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs });
                const rows = response.player_points || [];
                const columns = Object.keys(rows[0] || {});
                utils.download(`openfpl-gw${response.gameweek}-predictions.csv`, utils.csv(rows, columns), "text/csv");
                toast.show(`Exported ${rows.length} projections`, "success");
            } catch (error) {
                toast.show(`Export failed: ${error.message}`, "error");
            }
        };
        const exportTable = () => {
            const rows = views.predictions.filtered(payload).map((row) => ({
                id: row.id, name: row.web_name, team: row.team_name, position: row.position, expected_points: row.expected_points,
                availability: row.availability_factor, price: row.price, value: row.value, ownership: row.selected_by_percent,
                ...Object.fromEntries(Object.entries(row.models || {}).map(([key, value]) => [`model_${key}`, value])),
                ...Object.fromEntries(Object.entries(row.features || {}).map(([key, value]) => [`avg_${key}`, value])),
                in_squad: row.in_squad, decision: row.decision,
            }));
            utils.download(`openfpl-gw${state.gameweek}-dashboard.csv`, utils.csv(rows, Object.keys(rows[0] || { id: 0 })), "text/csv");
        };
        target.append(h("div", { class: "btn-row" },
            h("button", { class: "btn", type: "button", onClick: verify }, "Verify against /api/gw/playerpoints"),
            h("button", { class: "btn", type: "button", onClick: exportCsv }, "Export projections (POST /api/scout)"),
            h("button", { class: "btn ghost", type: "button", onClick: exportTable }, "Export this table with inputs")), output);
    },
};

/* ---------- AI squad ---------- */
views.squad = {
    async render(main, context) {
        main.append(sectionTitle("AI squad", `The GW${state.gameweek} squad, and the reason behind every pick`));
        const metaHolder = h("div");
        main.append(metaHolder);
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const pitchCard = ui.card("Starting XI and bench", "Best legal XI by projection; captain has the highest projection", { span: 7 });
        const captainCard = ui.card("Captaincy and checks", null, { span: 5 });
        const picksCard = ui.card("Why each player was picked");
        const cutoffCard = ui.card("Position cut-offs", "The last player in and the first left out per position", { span: 6 });
        const missCard = ui.card("Near misses", "Highest projections that did not make it, and why", { span: 6 });
        const dreamCard = ui.card("Official dream team", "GET /api/fpl/gameweeks/{gw}/dream-team — after the gameweek", { span: 12 });
        [pitchCard, captainCard, picksCard, cutoffCard, missCard, dreamCard].forEach((card) => grid.append(card.root));

        const squad = await ui.fill(pitchCard, () => api.get("/api/admin/squad", data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs }));
        if (!squad || !context.alive()) return;
        metaHolder.append(forecastChips(squad));
        views.squad.pitch(pitchCard.body, squad);
        views.squad.captaincy(captainCard, squad);
        views.squad.picks(picksCard.body, squad);
        views.squad.cutoffs(cutoffCard.body, squad);
        const blocked = squad.club_limited || [];
        if (blocked.length) {
            missCard.body.append(h("p", { class: "notes" }, `Blocked by the ${squad.max_per_club || 3}-per-club rule despite out-projecting the last pick in their position: ${blocked.map((row) => `${row.web_name} (${row.team_name}, ${utils.fmt(row.expected_points, 2)})`).join(", ")}.`));
        }
        missCard.body.append(ui.table(squad.near_misses || [], [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, h("span", { class: "sub" }, `${row.team_name} · ${row.position}`)) },
            { key: "expected_points", label: "xP", num: true, digits: 2 },
            { key: "status", label: "Left out because", render: (row) => h("span", { class: "small secondary" }, row.reason || row.status) },
        ], { footer: false, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
        ui.fill(dreamCard, async (target) => {
            const event = state.eventsById[state.gameweek];
            if (!event || (!event.finished && !event.is_current)) {
                clear(target).append(h("p", { class: "empty" }, "Published once the gameweek has been played."));
                return;
            }
            const dream = await api.get(`/api/fpl/gameweeks/${state.gameweek}/dream-team`);
            const team = (dream.data && dream.data.team) || [];
            const aiIds = new Set((squad.squad || []).map((player) => Number(player.id)));
            const overlap = team.filter((item) => aiIds.has(Number(item.element)));
            clear(target);
            target.append(h("p", null, statusBadge(overlap.length ? "good" : "neutral", `${overlap.length} of ${team.length} dream-team players were in the AI squad`)));
            target.append(ui.table(team, [
                { key: "player", label: "Player", value: (row) => (row.player && row.player.web_name) || row.element, render: (row) => h("span", null, (row.player && row.player.web_name) || `#${row.element}`, aiIds.has(Number(row.element)) ? chip("AI pick", "lime") : null) },
                { key: "points", label: "Points", num: true },
                { key: "position", label: "Slot", num: true },
            ], { footer: false, sortKey: "points" }));
        });
    },

    pitch(target, squad) {
        clear(target);
        const lineup = squad.lineup || {};
        const pitch = ui.pitch(squad.squad || [], { startingIds: lineup.starting_ids, title: (player) => player.decision_reason || "" });
        target.append(h("p", { class: "small secondary", style: { marginTop: 0 } },
            `Formation ${lineup.formation || "–"} · XI ${utils.fmt(lineup.starting_points, 2)} + captain = ${utils.fmt(lineup.projected_total, 2)} projected · 15-man total ${utils.fmt(squad.projected_squad_total, 2)}`,
            squad.actual ? ` · actual so far ${utils.fmt(squad.actual.total, 0)} (XI ${utils.fmt(squad.actual.starting_xi, 0)} + captain ${utils.fmt(squad.actual.captain_bonus, 0)})` : ""), pitch);
    },

    async captaincy(card, squad) {
        const target = clear(card.body);
        const captaincy = squad.captaincy || {};
        if (captaincy.summary) target.append(h("p", null, captaincy.summary));
        target.append(ui.kv([
            ["Captain margin", utils.fmt(captaincy.captain_margin, 2)],
            ["Vice margin over 3rd", utils.fmt(captaincy.vice_margin, 2)],
            ["Clubs used", String(Object.keys(squad.club_counts || {}).length)],
        ]));
        target.append(h("div", { class: "chips", style: { marginTop: "8px" } },
            Object.entries(squad.club_counts || {}).sort((a, b) => b[1] - a[1]).map(([club, count]) => chip(`${club} ${count}`, count >= 3 ? "lime" : ""))));
        const check = h("p", { class: "small", style: { marginTop: "10px" } }, h("span", { class: "muted" }, "Checking against /api/gw/scout…"));
        target.append(check);
        try {
            const service = await api.get("/api/gw/scout", { gameweek: state.gameweek }, { timeoutMs: CONFIG.slowTimeoutMs });
            const mine = new Set((squad.squad || []).map((player) => Number(player.id)));
            const theirs = (service.scout_team || []).map((player) => Number(player.id));
            const same = theirs.length === mine.size && theirs.every((id) => mine.has(id));
            clear(check).append(same ? statusBadge("good", "Identical to the squad /api/gw/scout serves") : statusBadge("warning", "Differs from /api/gw/scout (a newer forecast may have been made)"));
        } catch (error) {
            clear(check).append(statusBadge("serious", `Could not verify: ${error.message}`));
        }
    },

    picks(target, squad) {
        clear(target).append(ui.table(squad.squad || [], [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.role ? " " : null, row.role ? chip(row.role === "captain" ? "C" : "V", "lime") : null, h("span", { class: "sub" }, `${row.team_name} · ${utils.positionName(row.element_type)}`)) },
            { key: "expected_points", label: "xP", num: true, digits: 2 },
            { key: "models", label: "Model range", value: (row) => { const values = Object.values(row.models || {}); return values.length ? Math.max(...values) - Math.min(...values) : null; }, render: (row) => { const values = Object.values(row.models || {}).filter(utils.isNumber); return values.length ? `${utils.fmt(Math.min(...values), 2)}–${utils.fmt(Math.max(...values), 2)}` : "–"; } },
            { key: "availability_factor", label: "Avail.", render: ui.availability },
            { key: "rank_in_position", label: "Pos. rank", num: true },
            { key: "decision_reason", label: "Why", render: (row) => h("span", { class: "small secondary" }, row.decision_reason || "–") },
            { key: "actual_points", label: "Actual", num: true },
            { key: "price", label: "£", num: true, digits: 1 },
            { key: "selected_by_percent", label: "Own %", num: true, digits: 1 },
        ], { sortKey: "expected_points", footer: false, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
    },

    cutoffs(target, squad) {
        const rows = Object.entries(squad.cutoffs || {}).map(([position, cutoff]) => ({
            position,
            places: cutoff.places,
            last: cutoff.last_selected,
            first: cutoff.first_left_out,
            gap: cutoff.last_selected && cutoff.first_left_out ? cutoff.last_selected.expected_points - cutoff.first_left_out.expected_points : null,
        }));
        clear(target).append(ui.table(rows, [
            { key: "position", label: "Pos." },
            { key: "places", label: "Places", num: true },
            { key: "last", label: "Last in", render: (row) => row.last ? `${row.last.web_name} (${utils.fmt(row.last.expected_points, 2)})` : "–", sort: false },
            { key: "first", label: "First out", render: (row) => row.first ? h("span", null, `${row.first.web_name} (${utils.fmt(row.first.expected_points, 2)})`, row.first.status === "club-limit" ? h("span", { class: "sub" }, `club limit: ${row.first.blocking_club}`) : null) : "–", sort: false },
            { key: "gap", label: "Gap", num: true, digits: 2 },
        ], { footer: false }));
    },
};

/* ---------- My team: working squad, overrides and alerts (this browser only) ---------- */

/* A working squad: the published picks plus edits FPL cannot see yet. Pure
 * operations return { draft } or { draft, error } and never mutate input. */
const draft = {
    PREFIX: "openfpl.admin.draft.v1.",
    VERSION: 1,
    CHIPS: { bboost: "Bench Boost", "3xc": "Triple Captain", freehit: "Free Hit", wildcard: "Wildcard" },

    load(entryId) {
        try {
            const value = JSON.parse(storage.get("localStorage", draft.PREFIX + entryId) || "null");
            return draft.valid(value) ? value : null;
        } catch (error) {
            return null;
        }
    },
    save(entryId, value) {
        return storage.set("localStorage", draft.PREFIX + entryId, JSON.stringify({ ...value, updated_at: new Date().toISOString() }));
    },
    clear(entryId) {
        storage.remove("localStorage", draft.PREFIX + entryId);
    },
    valid(value) {
        return Boolean(value && value.version === draft.VERSION && Number.isInteger(value.gameweek)
            && Array.isArray(value.picks) && value.picks.length <= 20
            && value.picks.every((pick) => pick && Number.isInteger(pick.id) && Number.isInteger(pick.position))
            && value.players && typeof value.players === "object");
    },
    positionCode(value) {
        if (typeof value === "number") return value;
        return { GK: 1, DEF: 2, MID: 3, FWD: 4, Goalkeeper: 1, Defender: 2, Midfielder: 3, Forward: 4 }[value] || 0;
    },
    info(player) {
        return {
            web_name: player.web_name || `#${player.id}`,
            team_name: player.team_name || null,
            element_type: draft.positionCode(player.element_type ?? player.position),
            price: utils.isNumber(player.price) ? player.price : null,
        };
    },
    fromPublished(team) {
        const players = {};
        (team.squad || []).forEach((row) => { if (!row.unknown) players[row.id] = draft.info(row); });
        return {
            version: draft.VERSION,
            entry_id: team.entry_id,
            gameweek: team.gameweek,
            base_picks_gameweek: team.picks_gameweek,
            picks: (team.published_picks || []).map((pick) => ({
                id: pick.id, position: pick.position, is_captain: Boolean(pick.is_captain), is_vice_captain: Boolean(pick.is_vice_captain),
            })),
            transfers: [],
            chip: null,
            free_transfers: null,
            bank_override: null,
            purchase_prices: {},
            players,
        };
    },
    copy(value) {
        return JSON.parse(JSON.stringify(value));
    },
    position(value, id) {
        return draft.positionCode((value.players[id] || {}).element_type);
    },
    sorted(value) {
        return [...value.picks].sort((a, b) => a.position - b.position);
    },
    starters(value) {
        return draft.sorted(value).filter((pick) => pick.position <= 11);
    },
    bench(value) {
        return draft.sorted(value).filter((pick) => pick.position > 11);
    },
    formationOk(value) {
        const starters = draft.starters(value);
        const counts = { 1: 0, 2: 0, 3: 0, 4: 0 };
        starters.forEach((pick) => { counts[draft.position(value, pick.id)] = (counts[draft.position(value, pick.id)] || 0) + 1; });
        return starters.length === 11 && counts[1] === 1 && counts[2] >= 3 && counts[3] >= 2 && counts[4] >= 1;
    },
    formation(value) {
        const counts = { 2: 0, 3: 0, 4: 0 };
        draft.starters(value).forEach((pick) => { const code = draft.position(value, pick.id); if (counts[code] !== undefined) counts[code] += 1; });
        return `${counts[2]}-${counts[3]}-${counts[4]}`;
    },
    /* FPL's slot order: starters GK→FWD, then the bench keeper, then subs in order. */
    normalize(value) {
        const starters = draft.starters(value).sort((a, b) => draft.position(value, a.id) - draft.position(value, b.id));
        const bench = draft.bench(value);
        const keepers = bench.filter((pick) => draft.position(value, pick.id) === 1);
        const others = bench.filter((pick) => draft.position(value, pick.id) !== 1);
        [...starters, ...keepers, ...others].forEach((pick, index) => { pick.position = index + 1; });
        return value;
    },
    swap(value, firstId, secondId) {
        if (firstId === secondId) return { draft: value, error: "Pick two different players." };
        const next = draft.copy(value);
        const first = next.picks.find((pick) => pick.id === firstId);
        const second = next.picks.find((pick) => pick.id === secondId);
        if (!first || !second) return { draft: value, error: "Both players must be in your squad." };
        const firstStarts = first.position <= 11;
        const secondStarts = second.position <= 11;
        const keeper = (pick) => draft.position(next, pick.id) === 1;
        if (firstStarts && secondStarts) return { draft: value, error: "Both are already in the starting XI." };
        if (!firstStarts && !secondStarts) {
            if (keeper(first) || keeper(second)) return { draft: value, error: "The bench goalkeeper always sits first on the bench." };
            [first.position, second.position] = [second.position, first.position];
            return { draft: draft.normalize(next) };
        }
        const starter = firstStarts ? first : second;
        const sub = firstStarts ? second : first;
        if (keeper(starter) !== keeper(sub)) return { draft: value, error: "A goalkeeper can only swap with the other goalkeeper." };
        [starter.position, sub.position] = [sub.position, starter.position];
        // A benched captain or vice hands the armband to the player coming in.
        ["is_captain", "is_vice_captain"].forEach((key) => {
            if (starter[key]) {
                starter[key] = false;
                sub[key] = true;
            }
        });
        if (!draft.formationOk(next)) return { draft: value, error: "That leaves an illegal formation (the XI needs 1 GK, 3+ DEF, 2+ MID and 1+ FWD)." };
        return { draft: draft.normalize(next) };
    },
    moveBench(value, id, delta) {
        const next = draft.copy(value);
        const outfield = draft.bench(next).filter((pick) => draft.position(next, pick.id) !== 1);
        const index = outfield.findIndex((pick) => pick.id === id);
        const target = index + delta;
        if (index < 0) return { draft: value, error: "Only outfield substitutes can be reordered." };
        if (target < 0 || target >= outfield.length) return { draft: value, error: "Already at that end of the bench." };
        [outfield[index].position, outfield[target].position] = [outfield[target].position, outfield[index].position];
        return { draft: draft.normalize(next) };
    },
    setRole(value, id, key) {
        const next = draft.copy(value);
        const pick = next.picks.find((item) => item.id === id);
        if (!pick) return { draft: value, error: "That player is not in your squad." };
        if (pick.position > 11) return { draft: value, error: "Only a starter can wear the armband: bring him into the XI first." };
        const other = key === "is_captain" ? "is_vice_captain" : "is_captain";
        const holder = next.picks.find((item) => item[key]);
        if (holder && holder.id === id) return { draft: value };
        const swapping = pick[other];
        next.picks.forEach((item) => { item[key] = false; });
        pick[key] = true;
        if (swapping) {
            pick[other] = false;
            if (holder) holder[other] = true;
        }
        return { draft: next };
    },
    transfer(value, outId, player) {
        const next = draft.copy(value);
        const pick = next.picks.find((item) => item.id === outId);
        if (!pick) return { draft: value, error: "That player is not in your squad." };
        if (next.picks.some((item) => item.id === player.id)) return { draft: value, error: `${player.web_name || "He"} is already in your squad.` };
        const incoming = draft.info(player);
        if (draft.position(next, outId) !== incoming.element_type) return { draft: value, error: "A transfer must replace a player in the same position." };
        pick.id = player.id;
        next.players[player.id] = incoming;
        next.transfers = next.transfers || [];
        const chained = next.transfers.find((item) => item.in === outId);
        if (chained && chained.out === player.id) next.transfers = next.transfers.filter((item) => item !== chained);
        else if (chained) chained.in = player.id;
        else next.transfers.push({ out: outId, in: player.id });
        return { draft: next };
    },
    undoTransfer(value, inId) {
        const record = (value.transfers || []).find((item) => item.in === inId);
        if (!record) return { draft: value, error: "That isn't a transfer made here." };
        const original = value.players[record.out];
        if (!original) return { draft: value, error: "The original player is unknown; reset to published instead." };
        return draft.transfer(value, inId, { id: record.out, ...original });
    },
    setChip(value, chip) {
        const next = draft.copy(value);
        next.chip = draft.CHIPS[chip] ? chip : null;
        return { draft: next };
    },
    setField(value, key, fieldValue) {
        const next = draft.copy(value);
        next[key] = fieldValue;
        return { draft: next };
    },
    setPurchasePrice(value, id, price) {
        const next = draft.copy(value);
        next.purchase_prices = { ...(next.purchase_prices || {}) };
        if (utils.isNumber(price)) next.purchase_prices[id] = Math.round(price * 10) / 10;
        else delete next.purchase_prices[id];
        return { draft: next };
    },
    /* Why a stored draft no longer applies, or null while it does. */
    expire(value, { nextOpen, picksGameweek } = {}) {
        if (!value) return null;
        if (utils.isNumber(nextOpen) && value.gameweek < nextOpen) return `the GW${value.gameweek} deadline has passed`;
        if (utils.isNumber(picksGameweek) && picksGameweek >= value.gameweek) return `FPL now shows your GW${value.gameweek} team`;
        return null;
    },
    toRequest(value) {
        return {
            picks: value.picks.map(({ id, position, is_captain: captain, is_vice_captain: vice }) => ({ id, position, is_captain: Boolean(captain), is_vice_captain: Boolean(vice) })),
            chip: value.chip || null,
            free_transfers: utils.isNumber(value.free_transfers) ? value.free_transfers : null,
            bank_override: utils.isNumber(value.bank_override) ? value.bank_override : null,
            purchase_prices: value.purchase_prices || {},
        };
    },
    exportJson(value, personal) {
        return JSON.stringify({ kind: "openfpl-working-squad", version: 1, exported_at: new Date().toISOString(), draft: value || null, overrides: personal || {} }, null, 2);
    },
    importJson(text) {
        const value = JSON.parse(text);
        if (!value || value.kind !== "openfpl-working-squad") throw new Error("This isn't an OpenFPL working-squad file.");
        if (value.draft && !draft.valid(value.draft)) throw new Error("The working squad in this file is damaged.");
        return { draft: value.draft || null, overrides: overrides.clean(value.overrides || {}) };
    },
};

/* A manager's own view of players: locks, bans, availability and xP %. */
const overrides = {
    PREFIX: "openfpl.admin.overrides.v1.",
    load(entryId) {
        try {
            return overrides.clean(JSON.parse(storage.get("localStorage", overrides.PREFIX + entryId) || "{}"));
        } catch (error) {
            return {};
        }
    },
    save(entryId, value) {
        return storage.set("localStorage", overrides.PREFIX + entryId, JSON.stringify(value));
    },
    clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    },
    clean(value) {
        const result = {};
        Object.entries(value || {}).forEach(([key, item]) => {
            const id = Number(key);
            if (!Number.isInteger(id) || id < 1 || !item || typeof item !== "object") return;
            const entry = {};
            if (item.lock) entry.lock = true;
            else if (item.ban) entry.ban = true;
            if (utils.isNumber(item.availability)) entry.availability = overrides.clamp(item.availability, 0, 100);
            if (utils.isNumber(item.xp_pct)) entry.xp_pct = overrides.clamp(item.xp_pct, 0, 200);
            if (typeof item.web_name === "string") entry.web_name = item.web_name.slice(0, 60);
            if (Number.isInteger(item.set_for_gw)) entry.set_for_gw = item.set_for_gw;
            if (entry.lock || entry.ban || "availability" in entry || "xp_pct" in entry) result[id] = entry;
        });
        return result;
    },
    set(all, id, patch, gameweek) {
        const entry = { ...(all[id] || {}), ...patch };
        if (patch.lock) delete entry.ban;
        if (patch.ban) delete entry.lock;
        ["availability", "xp_pct"].forEach((key) => { if (key in patch && !utils.isNumber(patch[key])) delete entry[key]; });
        if (utils.isNumber(patch.availability)) entry.set_for_gw = gameweek;
        return overrides.clean({ ...all, [id]: entry });
    },
    remove(all, id) {
        const next = { ...all };
        delete next[id];
        return next;
    },
    /* Availability is about one gameweek: drop it once that gameweek is past. */
    expire(all, gameweek) {
        const expired = [];
        const next = {};
        Object.entries(all).forEach(([id, item]) => {
            const entry = { ...item };
            if ("availability" in entry && Number.isInteger(entry.set_for_gw) && entry.set_for_gw < gameweek) {
                expired.push({ id: Number(id), web_name: entry.web_name, availability: entry.availability, set_for_gw: entry.set_for_gw });
                delete entry.availability;
            }
            next[id] = entry;
        });
        return { overrides: overrides.clean(next), expired };
    },
    toRequest(all) {
        const result = {};
        Object.entries(all).forEach(([id, item]) => {
            result[id] = { lock: Boolean(item.lock), ban: Boolean(item.ban), availability: item.availability ?? null, xp_pct: item.xp_pct ?? null };
        });
        return result;
    },
};

/* What changed for the squad since the manager last looked. */
const alerts = {
    PREFIX: "openfpl.admin.seen.v1.",
    STATUS: { a: "available", d: "doubtful", i: "injured", s: "suspended", u: "unavailable", n: "not in the squad" },
    load(entryId) {
        try {
            const value = JSON.parse(storage.get("localStorage", alerts.PREFIX + entryId) || "null");
            return value && value.players ? value : null;
        } catch (error) {
            return null;
        }
    },
    save(entryId, snapshot) {
        return storage.set("localStorage", alerts.PREFIX + entryId, JSON.stringify(snapshot));
    },
    snapshot(squad, now = new Date()) {
        const players = {};
        (squad || []).forEach((row) => {
            if (!row || row.unknown) return;
            players[row.id] = {
                web_name: row.web_name,
                status: row.status ?? null,
                news: row.news || "",
                chance: row.chance_of_playing_next_round ?? null,
                price: utils.isNumber(row.price) ? row.price : null,
            };
        });
        return { at: now.toISOString(), players };
    },
    diff(previous, squad) {
        if (!previous || !previous.players) return [];
        const changes = [];
        const status = (code) => alerts.STATUS[code] || code || "unknown";
        const chance = (value) => (utils.isNumber(value) ? `${value}%` : "no flag");
        (squad || []).forEach((row) => {
            const before = previous.players[row.id];
            if (!before || row.unknown) return;
            const name = row.web_name || before.web_name;
            const news = row.news || "";
            if ((before.news || "") !== news) changes.push({ id: row.id, web_name: name, kind: "news", text: news ? `${name}: ${news}` : `${name}: news cleared` });
            if ((before.status ?? null) !== (row.status ?? null)) changes.push({ id: row.id, web_name: name, kind: "status", text: `${name}: ${status(before.status)} → ${status(row.status)}` });
            const nowChance = row.chance_of_playing_next_round ?? null;
            if ((before.chance ?? null) !== nowChance) changes.push({ id: row.id, web_name: name, kind: "chance", text: `${name}: chance of playing ${chance(before.chance)} → ${chance(nowChance)}` });
            if (utils.isNumber(before.price) && utils.isNumber(row.price) && Math.abs(before.price - row.price) > 1e-9) {
                changes.push({ id: row.id, web_name: name, kind: "price", delta: Math.round((row.price - before.price) * 10) / 10, text: `${name}: £${utils.fmt(before.price, 1)}m → £${utils.fmt(row.price, 1)}m` });
            }
        });
        return changes;
    },
};

/* ---------- My team ---------- */
views.myteam = {
    SECTIONS: [["squad", "Squad"], ["plan", "Plan"], ["review", "Review"], ["leagues", "Leagues"]],
    SECTION_KEY: "openfpl.admin.myteam.section",

    async render(main, context) {
        main.append(sectionTitle("My team", "Your FPL squad as it really is: add the changes FPL can't see yet, then rate, advise, plan and review it"));
        const saved = storage.get("localStorage", CONFIG.storageKeys.entry) || "";
        const input = h("input", { type: "text", inputmode: "numeric", pattern: "[0-9]*", placeholder: "FPL team ID", value: saved, style: { width: "160px" }, "aria-label": "FPL team ID" });
        const recent = (() => {
            try {
                return JSON.parse(storage.get("localStorage", CONFIG.storageKeys.recentTeams) || "[]").map(String);
            } catch (error) {
                return [];
            }
        })();
        const body = h("div", { class: "stack" });
        const load = () => {
            const id = input.value.trim();
            if (!/^\d+$/.test(id)) {
                toast.show("Enter a numeric FPL team ID", "error");
                return;
            }
            storage.set("localStorage", CONFIG.storageKeys.entry, id);
            views.myteam.load(body, Number(id), context);
        };
        input.addEventListener("keydown", (event) => { if (event.key === "Enter") load(); });
        main.append(h("div", { class: "card" },
            h("div", { class: "filters", style: { marginBottom: 0 } },
                input,
                h("button", { class: "btn primary", type: "button", onClick: load }, "Load team"),
                recent.length ? h("span", { class: "small muted" }, "Recent:") : null,
                [...new Set([saved, ...recent].filter(Boolean))].slice(0, 6).map((id) => h("button", { class: "btn small ghost", type: "button", onClick: () => { input.value = id; load(); } }, id)))));
        main.append(body);
        if (saved) views.myteam.load(body, Number(saved), context);
        else body.append(h("p", { class: "empty" }, "Enter your FPL team ID (the number in your Points page URL)."));
    },

    load(body, entryId, context) {
        clear(body);
        const loaded = overrides.load(entryId);
        const session = {
            entryId,
            context,
            team: null,
            draft: draft.load(entryId),
            overrides: loaded,
            expired: [],
            editing: false,
            selected: null,
            swapping: null,
            picking: null,
            outlookStale: true,
            loaded: {},
            requestId: 0,
            section: views.myteam.SECTIONS.some(([key]) => key === storage.get("localStorage", views.myteam.SECTION_KEY)) ? storage.get("localStorage", views.myteam.SECTION_KEY) : "squad",
            cards: {},
        };
        views.myteam.session = session;
        session.scheduleRefresh = utils.debounce(() => views.myteam.refresh(session), 400);
        const card = (key, title, subtitle, span) => { session.cards[key] = ui.card(title, subtitle, { span }); return session.cards[key].root; };

        const top = h("div", { class: "grid" },
            card("profile", "Manager", `/api/fpl/managers/${entryId}`, 4),
            card("status", "Working squad", "Changes FPL can't see yet, saved in this browser only", 8));
        const switcher = h("div", { class: "segmented", role: "tablist", "aria-label": "My team sections" });
        const sections = {
            squad: h("div", { class: "grid" },
                card("rating", "AI rating", "POST /api/admin/managers/{id}/team · working squad against the published one", 5),
                card("pitch", "Your squad", "Click a player to explain him, or Edit to change your squad", 7),
                card("advice", "Lineup & bench advisor", "Best XI, bench order and armband, with autosubs", 6),
                card("captaincy", "Captaincy", "Armband options with model range, minutes risk and ownership", 6),
                card("alerts", "News & prices", "Since your last visit, current flags and price pressure", 6),
                card("personal", "Personal settings", "Locks, bans and your own availability or xP", 6),
                card("moves", "Suggested transfers", "Up to three single transfers for this gameweek, best first", 12),
                card("squadTable", "Your squad against the AI", "Projections for the selected gameweek", 12)),
            plan: h("div", { class: "grid" },
                card("optimize", "Optimize", "Transfers with your free transfers, or a Wildcard or Free Hit. Plans only: nothing changes on FPL.", 6),
                card("chips", "Chip planner", "POST /api/admin/managers/{id}/outlook · what each chip adds, week by week", 6),
                card("plan", "Optimizer plan", "POST /api/admin/optimize", 12),
                card("ticker", "Fixture ticker", "Fixtures, difficulty and projected points for your squad", 12)),
            review: h("div", { class: "grid" },
                card("review", "Gameweek review", "GET /api/admin/managers/{id}/review · your decisions against hindsight", 12),
                card("picks", "Published picks", "/api/fpl/managers/{id}/gameweeks/{gw}/picks", 5),
                card("history", "Season history", "/api/fpl/managers/{id}/history", 7),
                card("transfers", "Transfer history", "/api/fpl/managers/{id}/transfers", 12)),
            leagues: h("div", { class: "grid" },
                card("leagues", "Leagues and cups", "Classic and head-to-head standings, matches and cup status", 12)),
        };
        session.sections = sections;
        session.switcher = switcher;
        add(body, top, switcher, Object.values(sections));
        clear(session.cards.plan.body).append(h("p", { class: "empty" }, "Choose a mode in Optimize and run it."));

        session.managerPromise = api.get(`/api/fpl/managers/${entryId}`);
        session.managerPromise.catch(() => null);
        ui.fill(session.cards.profile, (target) => views.myteam.profile(target, session));
        views.myteam.show(session, session.section);
        views.myteam.refresh(session);
    },

    show(session, key) {
        session.section = key;
        storage.set("localStorage", views.myteam.SECTION_KEY, key);
        clear(session.switcher);
        views.myteam.SECTIONS.forEach(([name, label]) => {
            session.switcher.append(h("button", {
                type: "button", role: "tab", "aria-selected": name === key ? "true" : "false",
                onClick: () => views.myteam.show(session, name),
            }, label));
        });
        Object.entries(session.sections).forEach(([name, node]) => { node.hidden = name !== key; });
        if (key === "plan" && session.team) views.myteam.loadPlan(session);
        if (key === "review" && !session.loaded.review) views.myteam.loadReview(session);
        if (key === "leagues" && !session.loaded.leagues) {
            session.loaded.leagues = true;
            ui.fill(session.cards.leagues, (target) => views.myteam.leagues(target, session));
        }
    },

    async profile(target, session) {
        const [manager, regions] = await Promise.all([session.managerPromise, data.regions().catch(() => null)]);
        const record = manager.data || {};
        session.manager = record;
        const region = ((regions && regions.results) || []).find((item) => item.id === record.player_region_id);
        clear(target).append(ui.kv([
            ["Team", record.name],
            ["Manager", `${record.player_first_name || ""} ${record.player_last_name || ""}`.trim()],
            ["Region", (region && region.name) || record.player_region_name || "–"],
            ["Favourite club", record.favourite_team ? record.favourite_team.name : "–"],
            ["Overall points · rank", `${utils.fmtInt(record.summary_overall_points)} · ${utils.fmtInt(record.summary_overall_rank)}`],
            ["GW points · rank", `${utils.fmtInt(record.summary_event_points)} · ${utils.fmtInt(record.summary_event_rank)}`],
            ["Squad value · bank", `£${utils.fmt(record.last_deadline_value, 1)}m · £${utils.fmt(record.last_deadline_bank, 1)}m`],
            ["Current gameweek", record.current_event],
        ]));
    },

    /* The draft that applies to the gameweek on screen, if any. */
    activeDraft(session) {
        const team = session.team;
        return session.draft && team && session.draft.gameweek === team.gameweek ? session.draft : null;
    },

    requestBody(session) {
        const body = { gameweek: state.gameweek, overrides: overrides.toRequest(session.overrides) };
        if (session.draft && session.draft.gameweek === state.gameweek) body.draft = draft.toRequest(session.draft);
        return body;
    },

    async refresh(session) {
        if (!session.context.alive()) return;
        const id = ++session.requestId;
        const body = views.myteam.requestBody(session);
        const dependent = ["rating", "pitch", "advice", "captaincy", "alerts", "moves", "squadTable"];
        dependent.forEach((key) => session.cards[key].root.classList.add("loading"));
        try {
            const team = await api.post(`/api/admin/managers/${session.entryId}/team`, body, undefined, { timeoutMs: CONFIG.slowTimeoutMs });
            if (id !== session.requestId || !session.context.alive()) return;
            const reason = draft.expire(session.draft, {
                nextOpen: team.next_open_gameweek,
                picksGameweek: session.draft && session.draft.gameweek === team.gameweek ? team.picks_gameweek : null,
            });
            if (reason) {
                draft.clear(session.entryId);
                session.draft = null;
                session.editing = false;
                toast.show(`Your working squad was cleared: ${reason}.`, "info", 9000);
                views.myteam.refresh(session);
                return;
            }
            const aged = overrides.expire(session.overrides, team.next_open_gameweek || team.gameweek);
            if (aged.expired.length) {
                session.overrides = aged.overrides;
                session.expired = aged.expired;
                overrides.save(session.entryId, session.overrides);
                views.myteam.refresh(session);
                return;
            }
            session.team = team;
            views.myteam.renderSquad(session);
            session.outlookStale = true;
            if (session.section === "plan") views.myteam.loadPlan(session);
        } catch (error) {
            if (id !== session.requestId || !session.context.alive()) return;
            if (error.status === 409 && body.draft) {
                draft.clear(session.entryId);
                session.draft = null;
                toast.show(`Your working squad was cleared: ${error.message}`, "info", 9000);
                views.myteam.refresh(session);
                return;
            }
            ["rating", "pitch", "advice", "captaincy", "moves", "squadTable"].forEach((key) => clear(session.cards[key].body).append(ui.errorBox(error, () => views.myteam.refresh(session))));
            clear(session.cards.status.body).append(ui.errorBox(error));
        } finally {
            if (id === session.requestId) dependent.forEach((key) => session.cards[key].root.classList.remove("loading"));
        }
    },

    renderSquad(session) {
        views.myteam.renderStatus(session);
        views.myteam.renderRating(session);
        views.myteam.renderPitch(session);
        views.myteam.renderAdvice(session);
        views.myteam.renderCaptaincy(session);
        views.myteam.renderAlerts(session);
        views.myteam.renderPersonal(session);
        views.myteam.moves(session.cards.moves.body, session.team);
        views.myteam.squad(session.cards.squadTable.body, session.team);
    },

    /* Apply a draft operation, save it, redraw at once and re-score shortly. */
    edit(session, operation) {
        if (!session.team || !session.team.transfers_open) {
            toast.show("Edits are for a gameweek that is still open.", "error");
            return false;
        }
        const base = views.myteam.activeDraft(session) || draft.fromPublished(session.team);
        const result = operation(base);
        if (result.error) {
            toast.show(result.error, "error");
            return false;
        }
        session.draft = { ...result.draft, updated_at: new Date().toISOString() };
        draft.save(session.entryId, session.draft);
        views.myteam.renderPitch(session);
        views.myteam.renderStatus(session, true);
        session.scheduleRefresh();
        return true;
    },

    setOverride(session, id, patch, name) {
        session.overrides = overrides.set(session.overrides, id, { ...patch, ...(name ? { web_name: name } : {}) }, (session.team && session.team.gameweek) || state.gameweek);
        overrides.save(session.entryId, session.overrides);
        views.myteam.renderPersonal(session);
        if (session.editing) views.myteam.renderPitch(session);
        session.scheduleRefresh();
    },

    reset(session) {
        draft.clear(session.entryId);
        session.draft = null;
        session.selected = null;
        session.swapping = null;
        views.myteam.refresh(session);
    },

    exportFile(session) {
        const text = draft.exportJson(session.draft, session.overrides);
        utils.download(`openfpl-working-squad-${session.entryId}.json`, text, "application/json");
    },

    importFile(session) {
        const input = h("input", { type: "file", accept: "application/json,.json", style: { display: "none" } });
        input.addEventListener("change", async () => {
            const file = input.files && input.files[0];
            input.remove();
            if (!file) return;
            try {
                const result = draft.importJson(await file.text());
                if (result.draft && Number(result.draft.entry_id) !== session.entryId) throw new Error(`This file is for team ${result.draft.entry_id}.`);
                session.draft = result.draft;
                session.overrides = result.overrides;
                if (session.draft) draft.save(session.entryId, session.draft);
                else draft.clear(session.entryId);
                overrides.save(session.entryId, session.overrides);
                toast.show("Working squad imported", "success");
                views.myteam.refresh(session);
            } catch (error) {
                toast.show(`Import failed: ${error.message}`, "error", 8000);
            }
        });
        document.body.append(input);
        input.click();
    },

    renderStatus(session, pending = false) {
        const card = session.cards.status;
        const team = session.team;
        const target = clear(card.body);
        clear(card.actions);
        const active = views.myteam.activeDraft(session);
        const hasOverrides = Object.keys(session.overrides).length > 0;
        if (active || hasOverrides) card.actions.append(h("button", { class: "btn small ghost", type: "button", onClick: () => views.myteam.exportFile(session) }, "Export"));
        card.actions.append(h("button", { class: "btn small ghost", type: "button", onClick: () => views.myteam.importFile(session) }, "Import"));
        if (active) card.actions.append(h("button", { class: "btn small danger", type: "button", onClick: () => views.myteam.reset(session) }, "Reset to published"));

        if (!team.transfers_open) {
            target.append(h("p", { style: { margin: 0 } }, statusBadge("neutral", `GW${team.gameweek} is closed: this is your published GW${team.picks_gameweek} team.`)));
            if (session.draft) {
                target.append(h("p", { class: "small secondary" }, `Your working squad is for GW${session.draft.gameweek}. `,
                    h("button", { class: "btn small", type: "button", onClick: () => app.setGameweek(session.draft.gameweek) }, `Switch to GW${session.draft.gameweek}`)));
            } else {
                target.append(h("p", { class: "small muted" }, `To add changes FPL can't see yet, switch to GW${team.next_open_gameweek}. `,
                    h("button", { class: "btn small", type: "button", onClick: () => app.setGameweek(team.next_open_gameweek) }, `Switch to GW${team.next_open_gameweek}`)));
            }
            return;
        }
        if (!active) {
            target.append(h("p", { style: { margin: 0 } }, statusBadge("good", `Your published team (picks from GW${team.picks_gameweek}), planned for GW${team.gameweek}.`)),
                h("p", { class: "small secondary" }, "Made transfers, changed the bench or the captain on FPL since the deadline? FPL only shows them after the next deadline, so add them here and every card uses them. ",
                    h("button", { class: "btn small primary", type: "button", onClick: () => { session.editing = true; views.myteam.renderPitch(session); session.cards.pitch.root.scrollIntoView({ behavior: "smooth", block: "start" }); } }, "Edit your squad")));
        } else {
            const changes = team.edited ? team.changes || {} : {};
            const summary = pending ? ["updating…"] : (changes.summary || []);
            target.append(h("div", { class: "row" },
                chip("Edited", "lime"),
                summary.length ? summary.map((item) => chip(item)) : chip("No changes yet"),
                h("span", { class: "small muted" }, `saved ${utils.timeAgo(active.updated_at)} · for GW${active.gameweek}`)));
            const validation = team.validation || {};
            if (!pending && (validation.errors || []).length) {
                target.append(h("ul", { class: "reasons small", style: { marginTop: "8px" } }, validation.errors.map((item) => h("li", null, statusBadge("critical", item)))));
            }
            if (!pending && (validation.warnings || []).length) {
                target.append(h("ul", { class: "reasons small", style: { marginTop: "8px" } }, validation.warnings.map((item) => h("li", null, statusBadge("warning", item)))));
            }
            if (!pending && changes.transfers && changes.transfers.length) {
                target.append(h("p", { class: "small secondary", style: { marginBottom: 0 } },
                    changes.transfers.map((item) => `${(item.out || {}).web_name || "–"} (£${utils.fmt((item.out || {}).sell, 1)}m) → ${(item.in || {}).web_name || "–"} (£${utils.fmt((item.in || {}).buy, 1)}m)`).join(" · "),
                    ` · bank £${utils.fmt(changes.bank, 1)}m → £${utils.fmt(changes.bank_after, 1)}m`));
            }
        }
        if (session.expired.length) {
            target.append(h("p", { class: "notes" }, `Your availability for ${session.expired.map((item) => `${item.web_name || `#${item.id}`} (GW${item.set_for_gw})`).join(", ")} expired with its gameweek.`));
        }
    },

    renderRating(session) {
        const team = session.team;
        const target = clear(session.cards.rating.body);
        const working = team.rating;
        const published = team.published_rating;
        if (!working) {
            target.append(statusBadge("warning", "The working squad can be rated once it has 15 players with 11 starters."));
            return;
        }
        const changed = team.edited || (team.overrides_applied || []).length > 0;
        const delta = published ? working.rating - published.rating : null;
        target.append(h("div", { class: "headline" },
            h("span", { class: "big" }, `${working.rating}`),
            h("span", { class: "secondary" }, `/ 100 · grade ${working.grade}`),
            changed && published ? chip(`published ${published.rating} (${utils.fmtSigned(delta, 0)})`, "aqua") : chip("Published team", "aqua"),
            team.frozen ? chip("Frozen forecast", "aqua") : chip("Live forecast", "lime")));
        const expected = team.expected || {};
        target.append(ui.tiles([
            { label: "Projected", value: utils.fmt(working.projected_points, 1), detail: `AI ${utils.fmt(working.ai_projected_points, 1)} · with your captain and chip` },
            { label: "With autosubs", value: utils.fmt(expected.expected_points, 1), detail: `${utils.fmtSigned(expected.autosub_value, 1)} from the bench and vice` },
            changed && published ? { label: "Published", value: utils.fmt(published.projected_points, 1), detail: `rating ${published.rating}` } : null,
        ]));
        charts.hbars(target, [
            { label: "Starting XI (of 80)", value: working.components.starting_xi },
            { label: "Captaincy (of 10)", value: working.components.captaincy },
            { label: "Availability (of 10)", value: working.components.availability },
        ], { format: (value) => utils.fmt(value, 1), valueLabel: "Points" });
        target.append(h("div", { class: "split" },
            h("div", null, h("h3", { class: "small muted" }, "Strengths"), h("ul", { class: "reasons" }, (working.strengths || []).map((item) => h("li", null, item)))),
            h("div", null, h("h3", { class: "small muted" }, "Risks"), h("ul", { class: "reasons" }, (working.risks || []).map((item) => h("li", null, item))))));
        const over = Object.entries(team.clubs_over_limit || {});
        if (over.length) target.append(h("p", { class: "notes" }, `Over the club limit: ${over.map(([club, count]) => `${club} (${count})`).join(", ")}. Your next transfer out must come from one of them.`));
        if ((team.overrides_applied || []).length) target.append(h("p", { class: "small muted" }, `Includes your own availability or xP for ${team.overrides_applied.length} player${team.overrides_applied.length > 1 ? "s" : ""}. ${team.benchmark}`));
        (team.notes || []).forEach((note) => target.append(h("p", { class: "notes" }, note)));
    },

    /* Players for the pitch: the draft's picks (if any) with the latest scores. */
    pitchPlayers(session) {
        const team = session.team || {};
        const rows = Object.fromEntries((team.squad || []).map((row) => [row.id, row]));
        const active = views.myteam.activeDraft(session);
        const picks = active ? draft.sorted(active) : [...(team.working_picks || [])].sort((a, b) => a.position - b.position);
        const incoming = new Set(active ? (active.transfers || []).map((item) => item.in) : []);
        const players = picks.map((pick) => {
            const row = rows[pick.id];
            const info = (active && active.players[pick.id]) || {};
            const personal = session.overrides[pick.id] || {};
            return {
                ...info,
                ...(row || {}),
                id: pick.id,
                pick_position: pick.position,
                element_type: row ? row.element_type : info.element_type,
                role: pick.is_captain ? "captain" : pick.is_vice_captain ? "vice" : "",
                new: incoming.has(pick.id) || Boolean(row && row.new),
                locked: Boolean(personal.lock),
                banned: Boolean(personal.ban),
                pending: !row,
            };
        });
        return { players, startingIds: picks.filter((pick) => pick.position <= 11).map((pick) => pick.id), active };
    },

    renderPitch(session) {
        const card = session.cards.pitch;
        const team = session.team;
        const target = clear(card.body);
        clear(card.actions);
        if (!team) return;
        const { players, startingIds, active } = views.myteam.pitchPlayers(session);
        if (team.transfers_open) {
            card.actions.append(h("button", {
                class: `btn small${session.editing ? " primary" : ""}`, type: "button", "aria-pressed": session.editing ? "true" : "false",
                onClick: () => { session.editing = !session.editing; session.selected = null; session.swapping = null; session.picking = null; views.myteam.renderPitch(session); },
            }, session.editing ? "Done editing" : "Edit squad"));
        }
        if (session.editing) {
            const current = active || {};
            const chipSelect = h("select", { "aria-label": "Chip for this gameweek" },
                h("option", { value: "" }, "No chip"),
                Object.entries(draft.CHIPS).map(([key, label]) => h("option", { value: key, selected: current.chip === key ? true : null }, label)));
            chipSelect.addEventListener("change", () => views.myteam.edit(session, (value) => draft.setChip(value, chipSelect.value)));
            const estimate = (team.free_transfers || {}).free_transfers;
            const free = h("input", { type: "number", min: 0, max: 5, step: 1, value: utils.isNumber(current.free_transfers) ? current.free_transfers : "", placeholder: utils.isNumber(estimate) ? String(estimate) : "∞", style: { width: "64px" }, "aria-label": "Free transfers before your changes" });
            free.addEventListener("change", () => views.myteam.edit(session, (value) => draft.setField(value, "free_transfers", free.value === "" ? null : Math.max(0, Math.min(5, Math.round(Number(free.value)))))));
            const bank = h("input", { type: "number", min: 0, max: 100, step: 0.1, value: utils.isNumber(current.bank_override) ? current.bank_override : "", placeholder: utils.fmt(team.changes ? team.changes.bank_computed : team.bank, 1), style: { width: "80px" }, "aria-label": "Bank override in £m" });
            bank.addEventListener("change", () => views.myteam.edit(session, (value) => draft.setField(value, "bank_override", bank.value === "" ? null : Math.max(0, Number(bank.value)))));
            target.append(h("div", { class: "row", style: { marginBottom: "8px" } },
                h("label", { class: "small row", style: { gap: "6px" } }, "Chip", chipSelect),
                h("label", { class: "small row", style: { gap: "6px" }, title: "Free transfers you had before the changes below (blank: our estimate)" }, "Free transfers", free),
                h("label", { class: "small row", style: { gap: "6px" }, title: "Leave blank to use the bank worked out from your transfers" }, "Bank £m", bank),
                team.changes && team.changes.hits ? chip(`−${team.changes.hit_cost} hit`, "") : null));
            target.append(h("p", { class: "small secondary", style: { marginTop: 0 } },
                session.swapping ? `Now click the player to swap with ${(players.find((player) => player.id === session.swapping) || {}).web_name || ""} (or click him again to cancel).`
                    : "Click a player for captaincy, substitutions, bench order, transfers and your own settings."));
        }
        const projected = utils.sum(players.map((player) => (player.expected_points || 0) * (player.multiplier || 0)));
        const expected = team.expected || {};
        target.append(h("p", { class: "small secondary", style: { marginTop: 0 } },
            `${active ? `Working squad for GW${active.gameweek}` : `Picks from GW${team.picks_gameweek}`} · ${active ? draft.formation(active) : (team.validation || {}).formation || ""} · projected ${utils.fmt(projected, 2)}${utils.isNumber(expected.expected_points) ? ` · ${utils.fmt(expected.expected_points, 2)} with autosubs` : ""}`));
        target.append(ui.pitch(players, {
            startingIds,
            selectedId: session.editing ? session.selected : null,
            onClick: (player) => views.myteam.onPitchClick(session, player),
        }));
        if (session.editing && session.selected) {
            const player = players.find((item) => item.id === session.selected);
            if (player) target.append(views.myteam.actionPanel(session, player));
        }
    },

    onPitchClick(session, player) {
        if (!session.editing) {
            drawer.open(player.id, player.web_name);
            return;
        }
        if (session.swapping) {
            const from = session.swapping;
            session.swapping = null;
            if (from !== player.id) {
                session.selected = null;
                views.myteam.edit(session, (value) => draft.swap(value, from, player.id));
                return;
            }
        } else {
            session.selected = session.selected === player.id ? null : player.id;
            session.picking = null;
        }
        views.myteam.renderPitch(session);
    },

    actionPanel(session, player) {
        const team = session.team;
        const active = views.myteam.activeDraft(session) || draft.fromPublished(team);
        const pick = active.picks.find((item) => item.id === player.id) || {};
        const starting = pick.position <= 11;
        const keeper = draft.position(active, player.id) === 1;
        const incoming = (active.transfers || []).some((item) => item.in === player.id);
        const personal = session.overrides[player.id] || {};
        const row = (team.squad || []).find((item) => item.id === player.id) || {};
        const panel = h("div", { class: "action-panel" });
        const button = (label, onClick, options = {}) => h("button", { class: `btn small${options.kind ? ` ${options.kind}` : ""}`, type: "button", disabled: options.disabled ? true : null, title: options.title || null, "aria-pressed": options.pressed === undefined ? null : String(options.pressed), onClick }, label);
        const run = (operation) => views.myteam.edit(session, operation);
        panel.append(h("div", { class: "row" },
            h("strong", null, player.web_name),
            h("span", { class: "small muted" }, `${player.team_name || ""} · ${utils.positionName(player.element_type)} · ${starting ? `starting (slot ${pick.position})` : `bench ${pick.position - 11}`}`),
            chip(`${utils.fmt(player.expected_points, 2)} xP`, "lime"),
            utils.isNumber(row.selling_price) ? chip(`sells £${utils.fmt(row.selling_price, 1)}m`) : null,
            incoming ? chip("bought here", "aqua") : null));
        panel.append(h("div", { class: "btn-row", style: { marginTop: "8px" } },
            button("Captain", () => run((value) => draft.setRole(value, player.id, "is_captain")), { disabled: !starting || pick.is_captain }),
            button("Vice", () => run((value) => draft.setRole(value, player.id, "is_vice_captain")), { disabled: !starting || pick.is_vice_captain }),
            button(starting ? "Bench him…" : "Bring him on…", () => { session.swapping = player.id; views.myteam.renderPitch(session); }),
            !starting && !keeper ? button("Bench ←", () => run((value) => draft.moveBench(value, player.id, -1)), { title: "Earlier on the bench (comes on sooner)" }) : null,
            !starting && !keeper ? button("Bench →", () => run((value) => draft.moveBench(value, player.id, 1)), { title: "Later on the bench" }) : null,
            button("Transfer out…", () => { session.picking = session.picking === player.id ? null : player.id; views.myteam.renderPitch(session); }, { pressed: session.picking === player.id }),
            incoming ? button("Undo transfer", () => { session.selected = null; run((value) => draft.undoTransfer(value, player.id)); }) : null,
            button("Explain", () => drawer.open(player.id, player.web_name), { kind: "ghost" })));
        const availability = h("input", { type: "number", min: 0, max: 100, step: 5, value: utils.isNumber(personal.availability) ? personal.availability : "", placeholder: utils.isNumber(row.base_availability_factor ?? row.availability_factor) ? String(Math.round((row.base_availability_factor ?? row.availability_factor) * 100)) : "100", style: { width: "64px" }, "aria-label": `My availability for ${player.web_name} (%)` });
        availability.addEventListener("change", () => views.myteam.setOverride(session, player.id, { availability: availability.value === "" ? null : Number(availability.value) }, player.web_name));
        const xp = h("input", { type: "number", min: 0, max: 200, step: 5, value: utils.isNumber(personal.xp_pct) ? personal.xp_pct : "", placeholder: "100", style: { width: "64px" }, "aria-label": `My xP % for ${player.web_name}` });
        xp.addEventListener("change", () => views.myteam.setOverride(session, player.id, { xp_pct: xp.value === "" ? null : Number(xp.value) }, player.web_name));
        const personalRow = h("div", { class: "row", style: { marginTop: "8px" } },
            button(personal.lock ? "Locked" : "Lock", () => views.myteam.setOverride(session, player.id, { lock: !personal.lock }, player.web_name), { pressed: Boolean(personal.lock), title: "Never sold by suggestions or the optimizer" }),
            button(personal.ban ? "Banned" : "Ban", () => views.myteam.setOverride(session, player.id, { ban: !personal.ban }, player.web_name), { pressed: Boolean(personal.ban), title: "Sold and never bought by the optimizer" }),
            h("label", { class: "small row", style: { gap: "6px" } }, "My chance %", availability),
            h("label", { class: "small row", style: { gap: "6px" } }, "My xP %", xp));
        panel.append(personalRow);
        if (!player.new && !incoming) {
            const bought = h("input", { type: "number", min: 3, max: 20, step: 0.1, value: utils.isNumber((active.purchase_prices || {})[player.id]) ? active.purchase_prices[player.id] : "", placeholder: utils.fmt(row.purchase_price, 1), style: { width: "72px" }, "aria-label": `Price you paid for ${player.web_name}` });
            bought.addEventListener("change", () => run((value) => draft.setPurchasePrice(value, player.id, bought.value === "" ? null : Number(bought.value))));
            panel.append(h("div", { class: "row small", style: { marginTop: "8px" } },
                h("label", { class: "row", style: { gap: "6px" }, title: "FPL doesn't publish what you paid; fix our estimate if it's wrong" }, "Bought for £", bought),
                h("span", { class: "muted" }, row.purchase_source ? `estimate: ${row.purchase_source}` : "")));
        }
        if (session.picking === player.id) {
            const holder = h("div", { style: { marginTop: "10px" } });
            panel.append(holder);
            views.myteam.transferPicker(holder, session, player);
        }
        return panel;
    },

    async transferPicker(holder, session, outgoing) {
        holder.append(h("p", { class: "muted small" }, "Loading players…"));
        let predictions;
        try {
            predictions = await data.predictions();
        } catch (error) {
            clear(holder).append(ui.errorBox(error));
            return;
        }
        const team = session.team;
        const active = views.myteam.activeDraft(session) || draft.fromPublished(team);
        const position = draft.position(active, outgoing.id);
        const inSquad = new Set(active.picks.map((pick) => pick.id));
        const clubs = {};
        active.picks.forEach((pick) => {
            if (pick.id === outgoing.id) return;
            const club = (active.players[pick.id] || {}).team_name;
            clubs[club] = (clubs[club] || 0) + 1;
        });
        const row = (team.squad || []).find((item) => item.id === outgoing.id) || {};
        const budget = (utils.isNumber(team.bank) ? team.bank : 0) + (utils.isNumber(row.selling_price) ? row.selling_price : (outgoing.price || 0));
        const candidates = (predictions.players || []).filter((player) => player.element_type === position && !inSquad.has(player.id));
        const search = h("input", { type: "search", placeholder: `Search ${utils.positionName(position)}s…`, "aria-label": "Search replacements", autocomplete: "off" });
        const list = h("div", { class: "pick-list" });
        const draw = () => {
            const term = search.value.trim().toLowerCase();
            const shown = candidates.filter((player) => !term || `${player.web_name} ${player.team_name}`.toLowerCase().includes(term)).slice(0, 24);
            clear(list);
            shown.forEach((player) => {
                const full = (clubs[player.team_name] || 0) >= 3;
                const over = utils.isNumber(player.price) ? player.price - budget : 0;
                list.append(h("button", {
                    class: "btn small pick", type: "button", disabled: full ? true : null,
                    title: full ? `You already have 3 from ${player.team_name}` : player.news || null,
                    onClick: () => {
                        session.picking = null;
                        session.selected = player.id;
                        views.myteam.edit(session, (value) => draft.transfer(value, outgoing.id, player));
                    },
                },
                h("strong", null, player.web_name),
                h("span", { class: "muted" }, ` ${player.team_name} · £${utils.fmt(player.price, 1)}m`),
                h("span", { class: "xp" }, ` ${utils.fmt(player.expected_points, 2)} xP`),
                utils.isNumber(player.availability_factor) && player.availability_factor < 1 ? h("span", { class: "warn" }, ` ${utils.fmtPct(player.availability_factor)}`) : null,
                full ? h("span", { class: "out" }, " club full") : over > 0.05 ? h("span", { class: "out" }, ` £${utils.fmt(over, 1)}m over`) : null));
            });
            if (!shown.length) list.append(h("span", { class: "muted small" }, "No match"));
        };
        search.addEventListener("input", draw);
        clear(holder).append(h("p", { class: "small secondary", style: { margin: "0 0 6px" } },
            `Replace ${outgoing.web_name}: £${utils.fmt(budget, 1)}m to spend (bank plus his selling price), best projections first.`), search, list);
        draw();
        search.focus();
    },

    renderAdvice(session) {
        const team = session.team;
        const target = clear(session.cards.advice.body);
        const advice = team.advice;
        if (!advice) {
            target.append(h("p", { class: "empty" }, "Advice appears once the working squad is a legal 15."));
            return;
        }
        const current = advice.current_expected || {};
        const best = advice.expected || {};
        target.append(h("div", { class: "row" },
            advice.gain > 0.005 ? chip(`${utils.fmtSigned(advice.gain, 2)} expected`, "lime") : statusBadge("good", "Your lineup is already the best one"),
            h("span", { class: "small secondary" }, `with autosubs: ${utils.fmt(current.expected_points, 2)} now → ${utils.fmt(best.expected_points, 2)} advised`)));
        target.append(h("ul", { class: "reasons", style: { marginTop: "8px" } }, (advice.reasons || []).map((item) => h("li", null, item))));
        const names = Object.fromEntries((team.squad || []).map((row) => [row.id, row.web_name]));
        const bench = advice.bench_ids || [];
        const use = best.bench_use || {};
        target.append(h("p", { class: "small secondary" },
            `Captain ${names[advice.captain_id] || "–"}, vice ${names[advice.vice_id] || "–"}. Bench: `,
            bench.map((id, index) => `${index ? ", " : ""}${names[id] || `#${id}`}${utils.isNumber(use[id]) && use[id] > 0 ? ` (${utils.fmtPct(use[id])} to come on)` : ""}`).join(""), "."));
        if (team.transfers_open && advice.changed) {
            target.append(h("div", { class: "btn-row" }, h("button", { class: "btn small primary", type: "button", onClick: () => views.myteam.applyAdvice(session) }, "Apply to working squad")));
        }
        target.append(h("p", { class: "small muted" }, `${best.method === "sampled" ? `Sampled over ${best.scenarios} scenarios. ` : ""}${best.assumption || ""}`));
    },

    applyAdvice(session) {
        const advice = session.team.advice;
        views.myteam.edit(session, (value) => {
            const ids = new Set(value.picks.map((pick) => pick.id));
            if (advice.picks.length !== value.picks.length || advice.picks.some((pick) => !ids.has(pick.id))) {
                return { draft: value, error: "Your squad changed since this advice; wait for it to update." };
            }
            const next = draft.copy(value);
            next.picks = advice.picks.map((pick) => ({ id: pick.id, position: pick.position, is_captain: pick.is_captain, is_vice_captain: pick.is_vice_captain }));
            return { draft: next };
        });
    },

    renderCaptaincy(session) {
        const team = session.team;
        const target = clear(session.cards.captaincy.body);
        const options = team.captaincy || [];
        if (!options.length) {
            target.append(h("p", { class: "empty" }, "No captaincy options until the squad is legal."));
            return;
        }
        target.append(ui.table(options, [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.current_captain ? " " : null, row.current_captain ? chip("C", "lime") : null, row.current_vice ? " " : null, row.current_vice ? chip("V", "aqua") : null, row.starting ? null : chip("bench"), h("span", { class: "sub" }, `${row.team_name || ""} · ${row.position || ""}`)) },
            { key: "armband_value", label: "Armband", num: true, digits: 2, title: "Expected extra points from the armband, with your vice as cover" },
            { key: "points_if_plays", label: "If plays", num: true, title: "Projected points if he plays, and his chance of playing", render: (row) => h("span", null, utils.fmt(row.points_if_plays, 1), h("span", { class: "sub" }, `${utils.fmtPct(row.availability)} to play`)) },
            { key: "model_spread", label: "Models", value: (row) => row.model_spread, title: "Lowest to highest model projection, and the risk it implies", render: (row) => h("span", null, utils.isNumber(row.model_min) ? `${utils.fmt(row.model_min, 1)}–${utils.fmt(row.model_max, 1)}` : "–", h("span", { class: "sub" }, `${row.risk} risk`)) },
            { key: "ownership", label: "Own %", num: true, render: (row) => h("span", null, utils.fmt(row.ownership, 1), row.ownership_tag ? h("span", { class: "sub" }, row.ownership_tag) : null) },
            { key: "fixtures", label: "Fixture", sort: false, render: (row) => ui.fixtures((row.fixtures || []).map((fixture) => ({ opponent: fixture.opponent_short || fixture.opponent, home: fixture.home, difficulty: fixture.difficulty }))) },
            {
                key: "action", label: "", sort: false,
                render: (row) => (team.transfers_open && row.starting && !row.current_captain
                    ? h("button", { class: "btn small", type: "button", title: `Make ${row.web_name} captain in your working squad`, onClick: (event) => { event.stopPropagation(); views.myteam.edit(session, (value) => draft.setRole(value, row.id, "is_captain")); } }, "C")
                    : ""),
            },
        ], { footer: false, sortKey: "armband_value", onRowClick: (row) => drawer.open(row.id, row.web_name) }));
    },

    renderAlerts(session) {
        const team = session.team;
        const target = clear(session.cards.alerts.body);
        const squad = (team.squad || []).filter((row) => !row.unknown);
        const previous = alerts.load(session.entryId);
        if (!previous) {
            alerts.save(session.entryId, alerts.snapshot(squad));
            target.append(h("p", { class: "small secondary", style: { marginTop: 0 } }, "Watching your squad from now on: changes to news, status, chance of playing and price show here on your next visit."));
        } else {
            const changes = alerts.diff(previous, squad);
            target.append(h("div", { class: "row" },
                h("h3", { class: "small muted", style: { margin: 0 } }, `Since ${utils.dateTime(previous.at)}`),
                h("span", { class: "spacer" }),
                h("button", { class: "btn small ghost", type: "button", onClick: () => { alerts.save(session.entryId, alerts.snapshot(squad)); views.myteam.renderAlerts(session); } }, "Mark all seen")));
            target.append(changes.length
                ? h("ul", { class: "reasons small" }, changes.map((item) => h("li", null, statusBadge(item.kind === "price" ? (item.delta > 0 ? "good" : "serious") : "warning", item.text))))
                : h("p", { class: "small muted" }, "No changes since then."));
        }
        const flagged = squad.filter((row) => (row.status && row.status !== "a") || (utils.isNumber(row.chance_of_playing_next_round) && row.chance_of_playing_next_round < 100));
        target.append(h("h3", { class: "small muted", style: { margin: "10px 0 4px" } }, "Flags"));
        target.append(flagged.length
            ? h("ul", { class: "reasons small" }, flagged.map((row) => h("li", null, statusBadge(utils.isNumber(row.chance_of_playing_next_round) && row.chance_of_playing_next_round > 0 ? "warning" : "critical",
                `${row.web_name}: ${utils.isNumber(row.chance_of_playing_next_round) ? `${row.chance_of_playing_next_round}% · ` : ""}${row.news || alerts.STATUS[row.status] || row.status}`))))
            : h("p", { class: "small muted" }, "Nobody in your squad is flagged."));
        target.append(h("h3", { class: "small muted", style: { margin: "10px 0 4px" } }, "Price pressure (a heuristic: FPL's price algorithm isn't public)"));
        const priced = squad.map((row) => ({ ...row, pressure: (row.price_pressure || {}).pressure }));
        target.append(ui.table(priced, [
            { key: "web_name", label: "Player" },
            { key: "price", label: "£", num: true, digits: 1 },
            { key: "gw_change", label: "GW Δ", num: true, value: (row) => (row.price_pressure || {}).change_this_gameweek, render: (row) => utils.fmtSigned((row.price_pressure || {}).change_this_gameweek, 1) },
            { key: "net", label: "Net transfers", num: true, value: (row) => (row.price_pressure || {}).net_transfers, render: (row) => utils.fmtCompact((row.price_pressure || {}).net_transfers) },
            { key: "pressure", label: "Of owners", num: true, render: (row) => utils.fmtPct(row.pressure, 1) },
            { key: "direction", label: "Outlook", value: (row) => (row.price_pressure || {}).direction, render: (row) => {
                const direction = (row.price_pressure || {}).direction;
                return direction === "rise likely" ? statusBadge("good", direction) : direction === "fall likely" ? statusBadge("serious", direction) : h("span", { class: "muted" }, direction || "–");
            } },
            { key: "sells", label: "Sells now / after rise", sort: false, render: (row) => (row.selling_effect && utils.isNumber(row.selling_effect.now) ? `£${utils.fmt(row.selling_effect.now, 1)} / £${utils.fmt(row.selling_effect.after_rise, 1)}` : "–") },
        ], { footer: false, sortKey: "pressure", pageSize: 6 }));
    },

    renderPersonal(session) {
        const target = clear(session.cards.personal.body);
        const gameweek = (session.team && session.team.gameweek) || state.gameweek;
        target.append(h("p", { class: "small secondary", style: { marginTop: 0 } },
            "Locks and bans steer the suggestions and the optimizer; your chance of playing and xP % change the projections everywhere in My team. Your chance of playing is for this gameweek and expires after it."));
        target.append(ui.playerSearch((player) => views.myteam.setOverride(session, player.id, { xp_pct: 100 }, player.web_name), "Add a player…"));
        const entries = Object.entries(session.overrides);
        if (!entries.length) {
            target.append(h("p", { class: "empty" }, "No personal settings yet. Add a player above, or use Lock, Ban and My chance on the pitch."));
            return;
        }
        const rows = entries.map(([id, item]) => ({ id: Number(id), ...item }));
        target.append(ui.table(rows, [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name || `#${row.id}`, utils.isNumber(row.set_for_gw) && utils.isNumber(row.availability) ? h("span", { class: "sub" }, `chance set for GW${row.set_for_gw}`) : null) },
            { key: "lock", label: "Lock", sort: false, render: (row) => {
                const box = h("input", { type: "checkbox", checked: row.lock ? true : null, "aria-label": `Lock ${row.web_name || row.id}` });
                box.addEventListener("change", () => views.myteam.setOverride(session, row.id, { lock: box.checked }));
                return box;
            } },
            { key: "ban", label: "Ban", sort: false, render: (row) => {
                const box = h("input", { type: "checkbox", checked: row.ban ? true : null, "aria-label": `Ban ${row.web_name || row.id}` });
                box.addEventListener("change", () => views.myteam.setOverride(session, row.id, { ban: box.checked }));
                return box;
            } },
            { key: "availability", label: "Chance %", sort: false, render: (row) => {
                const input = h("input", { type: "number", min: 0, max: 100, step: 5, value: utils.isNumber(row.availability) ? row.availability : "", placeholder: "FPL", style: { width: "64px" }, "aria-label": `My chance of playing for ${row.web_name || row.id}` });
                input.addEventListener("change", () => views.myteam.setOverride(session, row.id, { availability: input.value === "" ? null : Number(input.value) }));
                return input;
            } },
            { key: "xp_pct", label: "xP %", sort: false, render: (row) => {
                const input = h("input", { type: "number", min: 0, max: 200, step: 5, value: utils.isNumber(row.xp_pct) ? row.xp_pct : "", placeholder: "100", style: { width: "64px" }, "aria-label": `My xP % for ${row.web_name || row.id}` });
                input.addEventListener("change", () => views.myteam.setOverride(session, row.id, { xp_pct: input.value === "" ? null : Number(input.value) }));
                return input;
            } },
            { key: "remove", label: "", sort: false, render: (row) => h("button", { class: "btn small ghost", type: "button", "aria-label": `Remove ${row.web_name || row.id}`, onClick: () => {
                session.overrides = overrides.remove(session.overrides, row.id);
                overrides.save(session.entryId, session.overrides);
                views.myteam.renderPersonal(session);
                session.scheduleRefresh();
            } }, "Remove") },
        ], { footer: false, sortKey: "web_name", sortDir: "asc" }));
        target.append(h("p", { class: "small muted" }, `Planning GW${gameweek}.`));
    },

    /* ---------- Plan section ---------- */
    loadPlan(session) {
        ui.fill(session.cards.optimize, (target) => optimizerView.controls(target, session));
        if (!session.outlookStale) return;
        session.outlookStale = false;
        views.myteam.loadOutlook(session);
    },

    async loadOutlook(session) {
        const team = session.team;
        const chipsCard = session.cards.chips;
        const tickerCard = session.cards.ticker;
        clear(chipsCard.actions);
        const horizon = h("select", { "aria-label": "Gameweeks ahead" }, [3, 4, 5, 6, 7, 8].map((value) => h("option", { value, selected: value === (session.horizon || 5) ? true : null }, `${value} GWs`)));
        horizon.addEventListener("change", () => { session.horizon = Number(horizon.value); views.myteam.loadOutlook(session); });
        chipsCard.actions.append(horizon);
        const body = { ...views.myteam.requestBody(session), horizon: session.horizon || 5 };
        if (!team.transfers_open) {
            delete body.draft;
            body.gameweek = team.next_open_gameweek;
        }
        clear(chipsCard.body).append(h("p", { class: "muted" }, "Projecting the coming gameweeks… (the first run can take a few seconds)"));
        clear(tickerCard.body).append(h("p", { class: "muted" }, "Loading fixtures and projections…"));
        tickerCard.root.classList.add("loading");
        await ui.fill(chipsCard, async (target) => {
            const outlook = await api.post(`/api/admin/managers/${session.entryId}/outlook`, body, undefined, { timeoutMs: CONFIG.slowTimeoutMs });
            if (!session.context.alive()) return;
            views.myteam.renderChips(target, outlook);
            views.myteam.renderTicker(tickerCard.body, outlook);
        }).finally(() => tickerCard.root.classList.remove("loading"));
        if (chipsCard.body.querySelector(".status.critical")) clear(tickerCard.body).append(h("p", { class: "empty" }, "The outlook could not be loaded."));
    },

    renderChips(target, outlook) {
        clear(target);
        const chips = outlook.chips || {};
        const best = chips.best || {};
        const names = { bboost: "Bench Boost", "3xc": "Triple Captain", freehit: "Free Hit" };
        target.append(h("p", { class: "small secondary", style: { marginTop: 0 } },
            `${outlook.edited ? "Your working squad" : "Your squad"} as it stands, with no further transfers: what each chip would add on top of your best XI and captain. Free Hit uses your £${utils.fmt(outlook.budget, 1)}m budget.`));
        const cell = (key, row) => {
            if (!utils.isNumber(row[key])) return "–";
            const available = (row.available || {})[key] !== false;
            const isBest = best[key] === row.gameweek;
            return h("span", { class: available ? null : "muted", title: available ? null : "Already played this half (estimate)" },
                utils.fmtSigned(row[key], 1), isBest ? " " : null, isBest ? chip("best", "lime") : null, available ? null : " (played)");
        };
        target.append(ui.table(chips.gameweeks || [], [
            { key: "gameweek", label: "GW", num: true, render: (row) => h("span", null, `GW${row.gameweek}`, row.doubles ? " " : null, row.doubles ? chip(`${row.doubles} DGW`, "aqua") : null, row.blanks ? " " : null, row.blanks ? chip(`${row.blanks} blank`) : null) },
            { key: "base", label: "Best XI + C", num: true, digits: 1 },
            { key: "bboost", label: names.bboost, num: true, render: (row) => cell("bboost", row) },
            { key: "3xc", label: names["3xc"], num: true, render: (row) => cell("3xc", row) },
            { key: "freehit", label: names.freehit, num: true, render: (row) => cell("freehit", row) },
            { key: "captain_name", label: "Captain" },
        ], { footer: false, sort: false }));
        const lines = Object.entries(names).map(([key, label]) => (best[key] ? `${label}: GW${best[key]}` : `${label}: not available in this window`));
        target.append(h("p", { class: "small secondary" }, `Best week in this window: ${lines.join(" · ")}. Wildcard: run it in Optimize.`));
        (outlook.notes || []).forEach((note) => target.append(h("p", { class: "small muted" }, note)));
    },

    renderTicker(target, outlook) {
        clear(target);
        const ticker = outlook.ticker || {};
        const gameweeks = ticker.gameweeks || [];
        const rows = ticker.rows || [];
        const head = h("tr", null, h("th", { scope: "col" }, "Player"), gameweeks.map((gameweek) => h("th", { scope: "col", class: "num" }, `GW${gameweek}`)), h("th", { scope: "col", class: "num" }, "Total"), h("th", { scope: "col", class: "num", title: "Average fixture difficulty" }, "FDR"));
        const body = h("tbody", null, rows.map((row) => h("tr", { class: "clickable", tabindex: 0, onClick: () => drawer.open(row.id, row.web_name) },
            h("td", null, h("span", null, row.web_name, h("span", { class: "sub" }, `${row.team_name || ""} · ${row.position || ""}`))),
            row.cells.map((cell) => h("td", { class: "ticker-cell" },
                cell.fixtures.length ? h("div", { class: "row", style: { gap: "3px", justifyContent: "flex-end" } }, cell.fixtures.map((fixture) => ui.fdr(fixture.difficulty, `${fixture.opponent || "?"} ${fixture.home ? "H" : "A"}`))) : h("span", { class: "muted small" }, "blank"),
                h("div", { class: "xp small" }, `${utils.fmt(cell.expected_points, 1)} xP`))),
            h("td", { class: "num" }, utils.fmt(row.total, 1)),
            h("td", { class: "num" }, utils.fmt(row.difficulty, 1)))));
        const foot = h("tfoot", null, h("tr", null, h("th", { scope: "row" }, "Best XI + captain"), (ticker.totals || []).map((item) => h("td", { class: "num" }, utils.fmt(item.best_xi, 1))), h("td", { class: "num" }, utils.fmt(utils.sum((ticker.totals || []).map((item) => item.best_xi)), 1)), h("td")));
        target.append(h("div", { class: "table-wrap" }, h("table", { class: "ticker" }, h("thead", null, head), body, foot)));
        target.append(h("p", { class: "small muted" }, "Difficulty is FPL's fixture difficulty rating (1 easy – 5 hard). Projections include your own availability and xP settings."));
    },

    /* ---------- Review section ---------- */
    async loadReview(session, gameweek) {
        session.loaded.review = true;
        const card = session.cards.review;
        const manager = await session.managerPromise.catch(() => null);
        const latest = Number((((manager || {}).data) || {}).current_event) || state.gameweek;
        const chosen = gameweek || latest;
        clear(card.actions);
        const select = h("select", { "aria-label": "Gameweek to review" }, Array.from({ length: latest }, (_, index) => latest - index).map((value) => h("option", { value, selected: value === chosen ? true : null }, `GW${value}`)));
        select.addEventListener("change", () => views.myteam.loadReview(session, Number(select.value)));
        card.actions.append(select);
        ui.fill(card, async (target) => {
            const review = await api.get(`/api/admin/managers/${session.entryId}/review`, { gameweek: chosen }, { timeoutMs: CONFIG.slowTimeoutMs });
            views.myteam.renderReview(target, review);
        });
        ui.fill(session.cards.picks, (target) => views.myteam.picks(target, session.entryId, chosen));
        if (!gameweek) {
            ui.fill(session.cards.history, (target) => views.myteam.history(target, session.entryId));
            ui.fill(session.cards.transfers, (target) => views.myteam.transferHistory(target, session.entryId));
        }
    },

    renderReview(target, review) {
        clear(target);
        const points = review.points || {};
        const captain = review.captain || {};
        const hindsight = review.hindsight || {};
        const moves = review.transfers || {};
        target.append(h("div", { class: "row" },
            review.provisional ? chip("Provisional: still being played", "aqua") : chip("Final", "lime"),
            review.chip ? chip(draft.CHIPS[review.chip] || review.chip, "aqua") : null,
            points.matches === false ? h("span", { class: "small muted", title: "Our sum from live points differs from FPL's (bonus or late corrections)" }, `FPL says ${utils.fmtInt(points.official)}, live points add to ${utils.fmt(points.computed, 0)}`) : null));
        target.append(ui.tiles([
            { label: "Points", value: utils.fmtInt(points.official ?? points.computed), detail: `${points.hits ? `−${points.hits} hits → ${utils.fmtInt(points.net)} · ` : ""}average ${utils.fmtInt(review.average)} · top ${utils.fmtInt(review.highest)}` },
            { label: "Captain", value: `${captain.armband_name || "–"} ${utils.fmtInt(captain.points)}×${captain.multiplier || 2}`, detail: captain.cost > 0 ? `${captain.best_name} (${utils.fmtInt(captain.best_points)}) would have added ${utils.fmt(captain.cost, 0)}` : "the best choice in hindsight" },
            { label: "Bench", value: utils.fmtInt(review.bench ? review.bench.official ?? review.bench.computed : null), detail: "points left on the bench" },
            { label: "Best XI in hindsight", value: utils.fmt(hindsight.total, 0), detail: utils.isNumber(hindsight.left_on_table) ? `${utils.fmt(hindsight.left_on_table, 0)} more than you scored` : "" },
            moves.count ? { label: "Transfers", value: utils.fmtSigned(moves.since_net ?? moves.net, 0), detail: `${moves.verdict}${moves.hits ? ` after −${moves.hits}` : ""}${utils.isNumber(moves.since_net) ? " (that week and since)" : ""}` } : null,
            { label: "Overall rank", value: utils.fmtInt(review.overall_rank), detail: `GW rank ${utils.fmtInt(review.rank)}` },
        ]));
        const players = (review.players || []).map((player) => ({ ...player, role: player.role, expected_points: player.points }));
        const startingIds = players.filter((player) => player.in_final_xi).map((player) => player.id);
        const grid = h("div", { class: "split" });
        grid.append(h("div", null,
            h("h3", { class: "small muted", style: { margin: "0 0 6px" } }, "Your team as it scored (after autosubs)"),
            ui.pitch(players, { startingIds, value: (player) => player.points, unit: "pts" }),
            (review.autosubs || []).length ? h("p", { class: "small secondary" }, `Autosubs: ${review.autosubs.map((sub) => `${sub.in_name} for ${sub.out_name}`).join(", ")}`) : null));
        const side = h("div", { class: "stack", style: { alignContent: "start" } });
        if (moves.count) {
            side.append(h("h3", { class: "small muted", style: { margin: 0 } }, moves.free_hit ? "Free Hit moves (the squad reverted after)" : "Transfers that week"));
            side.append(ui.table(moves.items || [], [
                { key: "out_name", label: "Out", render: (row) => `${row.out_name} (${utils.fmtInt(row.out_points)})` },
                { key: "in_name", label: "In", render: (row) => `${row.in_name} (${utils.fmtInt(row.in_points)})` },
                { key: "delta", label: "That week", num: true, render: (row) => utils.fmtSigned(row.delta, 0) },
                { key: "since_delta", label: "Since", num: true, render: (row) => utils.fmtSigned(row.since_delta, 0) },
            ], { footer: false, sort: false }));
        }
        const forecast = review.forecast || {};
        if (forecast.available) {
            side.append(h("h3", { class: "small muted", style: { margin: 0 } }, `The AI's forecast: ${utils.fmt(forecast.xi_expected, 1)} expected for your XI and captain, ${utils.fmt(forecast.xi_points, 0)} scored`));
            side.append(ui.table(forecast.players || [], [
                { key: "web_name", label: "Starter" },
                { key: "expected_points", label: "xP", num: true, digits: 1 },
                { key: "points", label: "Pts", num: true, digits: 0 },
                { key: "difference", label: "Δ", num: true, render: (row) => utils.fmtSigned(row.difference, 1) },
            ], { footer: false, sortKey: "difference", pageSize: 11, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
        } else {
            side.append(h("p", { class: "small muted" }, forecast.note || "No archived forecast for this gameweek, so xP can't be compared."));
        }
        grid.append(side);
        target.append(grid);
    },

    async history(target, entryId) {
        const response = await api.get(`/api/fpl/managers/${entryId}/history`);
        const payload = response.data || {};
        const current = payload.current || [];
        clear(target);
        const average = (gameweek) => (state.eventsById[gameweek] || {}).average_entry_score;
        target.append(h("h3", { class: "small muted", style: { margin: "0 0 4px" } }, "Points per gameweek"));
        charts.lines(target, [
            { name: "You", color: CONFIG.series[0], points: current.map((row) => ({ x: row.event, y: row.points })) },
            { name: "Average manager", color: CONFIG.series[1], points: current.map((row) => ({ x: row.event, y: average(row.event) ?? null })) },
        ], { format: (value) => utils.fmtInt(value), yMin: 0 });
        target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Overall rank (higher is better)"));
        charts.lines(target, [{ name: "Overall rank", color: CONFIG.series[0], points: current.map((row) => ({ x: row.event, y: row.overall_rank })) }], { invert: true, format: (value) => utils.fmtInt(value), height: 180 });
        const chips = payload.chips || [];
        target.append(h("div", { class: "chips", style: { margin: "10px 0" } }, chips.length ? chips.map((item) => chip(`${item.name} · GW${item.event}`, "aqua")) : h("span", { class: "muted small" }, "No chips played yet")));
        target.append(ui.table(current.slice().reverse(), [
            { key: "event", label: "GW", num: true },
            { key: "points", label: "Pts", num: true },
            { key: "points_on_bench", label: "Bench", num: true },
            { key: "event_transfers", label: "Transfers", num: true },
            { key: "event_transfers_cost", label: "Hits", num: true },
            { key: "overall_rank", label: "Overall rank", num: true },
            { key: "value", label: "Value", num: true, render: (row) => utils.fmt((row.value || 0) / 10, 1) },
        ], { footer: false, pageSize: 10 }));
        const past = payload.past || [];
        if (past.length) {
            target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Past seasons"));
            target.append(ui.autoTable(past.slice().reverse(), { keys: ["season_name", "total_points", "rank"] }));
        }
    },

    async transferHistory(target, entryId) {
        const response = await api.get(`/api/fpl/managers/${entryId}/transfers`, { limit: 100 });
        clear(target).append(ui.table(response.results || [], [
            { key: "event", label: "GW", num: true },
            { key: "out", label: "Out", value: (row) => (row.player_out || {}).web_name || row.element_out_id, render: (row) => `${(row.player_out || {}).web_name || row.element_out_id} (£${utils.fmt(row.element_out_cost, 1)})` },
            { key: "in", label: "In", value: (row) => (row.player_in || {}).web_name || row.element_in_id, render: (row) => `${(row.player_in || {}).web_name || row.element_in_id} (£${utils.fmt(row.element_in_cost, 1)})` },
            { key: "time", label: "When", render: (row) => utils.dateTime(row.time) },
        ], { sortKey: "event", pageSize: 15, empty: "No transfers made yet." }));
        target.append(h("p", { class: "small muted" }, `${response.total ?? 0} transfers this season.`));
    },

    async leagues(target, session) {
        const entryId = session.entryId;
        const manager = await session.managerPromise;
        const record = manager.data || {};
        const classic = ((record.leagues || {}).classic || []).map((league) => ({ ...league, kind: "classic" }));
        const h2h = ((record.leagues || {}).h2h || []).map((league) => ({ ...league, kind: "h2h" }));
        const all = [...classic, ...h2h];
        clear(target);
        if (!all.length) {
            target.append(h("p", { class: "empty" }, "No leagues found."));
            return;
        }
        const output = h("div", { class: "stack", style: { marginTop: "10px" } });
        const select = h("select", { "aria-label": "League" }, all.map((league, index) => h("option", { value: index }, `${league.kind === "h2h" ? "H2H · " : ""}${league.name}${league.entry_rank ? ` (#${utils.fmtInt(league.entry_rank)})` : ""}`)));
        const show = async () => {
            const league = all[Number(select.value)];
            clear(output).append(h("span", { class: "muted" }, "Loading…"));
            try {
                const blocks = [];
                if (league.kind === "classic") {
                    const standings = await api.get(`/api/fpl/leagues/classic/${league.id}/standings`, { page_standings: 1 });
                    const results = ((standings.data || {}).standings || {}).results || [];
                    blocks.push(ui.table(results, [
                        { key: "rank", label: "#", num: true },
                        { key: "entry_name", label: "Team", render: (row) => h("span", null, row.entry_name, h("span", { class: "sub" }, row.player_name)) },
                        { key: "event_total", label: "GW", num: true },
                        { key: "total", label: "Total", num: true },
                    ], { sortKey: "rank", sortDir: "asc", pageSize: 25, rowClass: (row) => (Number(row.entry) === entryId ? "in-squad" : "") }));
                } else {
                    const [standings, matches] = await Promise.all([
                        api.get(`/api/fpl/leagues/h2h/${league.id}/standings`),
                        api.get(`/api/fpl/leagues/h2h/${league.id}/matches`, { entry_id: entryId }),
                    ]);
                    blocks.push(ui.autoTable((((standings.data || {}).standings || {}).results) || [], { keys: ["rank", "entry_name", "player_name", "matches_won", "matches_drawn", "matches_lost", "points_for", "total"] }));
                    blocks.push(h("h3", { class: "small muted" }, "Your head-to-head matches"));
                    blocks.push(ui.autoTable(((matches.data || {}).results) || [], { keys: ["event", "entry_1_name", "entry_1_points", "entry_2_points", "entry_2_name", "winner"] }));
                }
                const cupButton = h("button", { class: "btn small", type: "button" }, "Cup status");
                const cupOutput = h("div");
                cupButton.addEventListener("click", async () => {
                    try {
                        const cup = await api.get(`/api/fpl/leagues/${league.id}/cup-status`);
                        clear(cupOutput).append(ui.json(cup.data));
                    } catch (error) {
                        clear(cupOutput).append(ui.errorBox(error));
                    }
                });
                clear(output).append(...blocks, h("div", { class: "row" }, cupButton), cupOutput);
            } catch (error) {
                clear(output).append(ui.errorBox(error, show));
            }
        };
        select.addEventListener("change", show);
        target.append(select, output);
        show();
    },

    moves(target, result) {
        clear(target);
        if (!result.transfers_open) {
            target.append(h("p", { class: "empty" }, "Transfers are closed for this gameweek (its deadline has passed)."));
            return;
        }
        if (!(result.transfers || []).length) {
            target.append(statusBadge("good", "No affordable single transfer raises your projection — the squad is already strong for this gameweek."));
            return;
        }
        target.append(h("p", { class: "small muted" }, `Bank £${utils.fmt(result.bank, 1)}m. FPL does not publish selling prices, so affordability is approximate.`));
        result.transfers.forEach((move, index) => {
            const panel = h("div");
            target.append(h("div", { class: "card", style: { background: "var(--surface-2)", marginBottom: "10px" } },
                h("div", { class: "row" },
                    h("strong", null, `${index + 1}. ${move.out.web_name} → ${move.in.web_name}`),
                    chip(`${utils.fmtSigned(move.gain, 2)} pts`, "lime"),
                    chip(`cost ${utils.fmtSigned(move.cost, 1)}m`),
                    h("span", { class: "spacer" }),
                    h("button", { class: "btn small", type: "button", onClick: () => views.myteam.explainMove(panel, move) }, "Explain this move")),
                h("p", { class: "small secondary", style: { margin: "6px 0 0" } },
                    `${move.out.web_name} (${move.out.team_name}, ${utils.fmt(move.out.expected_points, 2)} xP, £${utils.fmt(move.out.price, 1)}m) out for ${move.in.web_name} (${move.in.team_name}, ${utils.fmt(move.in.expected_points, 2)} xP, £${utils.fmt(move.in.price, 1)}m). The gain is measured on your best XI plus captain.`),
                panel));
        });
    },

    async explainMove(panel, move) {
        clear(panel).append(h("p", { class: "muted small" }, "Explaining both players…"));
        try {
            const [outgoing, incoming] = await Promise.all([
                api.get(`/api/admin/players/${move.out.id}/explain`, data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs }),
                api.get(`/api/admin/players/${move.in.id}/explain`, data.gameweekQuery(), { timeoutMs: CONFIG.slowTimeoutMs }),
            ]);
            const column = (title, explanation) => h("div", null,
                h("h3", { class: "small" }, `${title}: ${explanation.web_name} · ${utils.fmt(explanation.expected_points, 2)} xP`),
                h("ol", { class: "reasons small" }, (explanation.reasons || []).map((reason) => h("li", null, reason))),
                h("button", { class: "btn small ghost", type: "button", onClick: () => drawer.open(explanation.id, explanation.web_name) }, "Full explanation"));
            const groups = {};
            (outgoing.groups || []).forEach((group) => { groups[group.group] = { out: group.contribution, in: 0 }; });
            (incoming.groups || []).forEach((group) => { groups[group.group] = { ...(groups[group.group] || { out: 0 }), in: group.contribution }; });
            const comparison = h("div");
            add(clear(panel),
                h("div", { class: "split", style: { marginTop: "10px" } }, column("Out", outgoing), column("In", incoming)),
                h("h3", { class: "small muted" }, "Where the incoming player is stronger (positive) or weaker, by input group"),
                comparison);
            charts.hbars(comparison, Object.entries(groups).map(([group, values]) => ({ label: group, value: (values.in || 0) - (values.out || 0) })), { diverging: true, format: (value) => utils.fmtSigned(value, 2), valueLabel: "Incoming minus outgoing" });
        } catch (error) {
            clear(panel).append(ui.errorBox(error));
        }
    },

    squad(target, result) {
        clear(target).append(ui.table(result.squad || [], [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.role ? " " : null, row.role ? chip(row.role === "captain" ? "C" : "V", "lime") : null, row.new ? " " : null, row.new ? chip("IN", "aqua") : null, row.projection_missing ? chip("no projection") : null, row.overridden ? chip("your view") : null, h("span", { class: "sub" }, `${row.team_name || ""} · ${utils.positionName(row.element_type || row.position)}`)) },
            { key: "pick_position", label: "Slot", num: true },
            { key: "expected_points", label: "xP", num: true, digits: 2 },
            { key: "availability_factor", label: "Avail.", render: ui.availability },
            { key: "multiplier", label: "×", num: true },
            { key: "price", label: "£", num: true, digits: 1 },
            { key: "selling_price", label: "Sells", num: true, digits: 1, title: "Estimated selling price" },
            { key: "selected_by_percent", label: "Own %", num: true, digits: 1 },
        ], { sortKey: "pick_position", sortDir: "asc", footer: false, empty: "No players.", onRowClick: (row) => !row.unknown && drawer.open(row.id, row.web_name) }));
    },

    async picks(target, entryId, gameweek) {
        const response = await api.get(`/api/fpl/managers/${entryId}/gameweeks/${gameweek}/picks`);
        const payload = response.data || {};
        const entry = payload.entry_history || {};
        clear(target).append(ui.kv([
            ["Gameweek", `GW${gameweek}`],
            ["Active chip", payload.active_chip || "none"],
            ["Points", `${utils.fmtInt(entry.points)} (bench ${utils.fmtInt(entry.points_on_bench)})`],
            ["Transfers · cost", `${utils.fmtInt(entry.event_transfers)} · ${utils.fmtInt(entry.event_transfers_cost)}`],
            ["Rank", utils.fmtInt(entry.overall_rank)],
            ["Bank", utils.isNumber(entry.bank) ? `£${utils.fmt(entry.bank / 10, 1)}m` : "–"],
        ]));
        const subs = payload.automatic_subs || [];
        if (subs.length) target.append(h("p", { class: "small secondary" }, `Automatic subs: ${subs.map((sub) => `${(sub.player_in || {}).web_name || sub.element_in} for ${(sub.player_out || {}).web_name || sub.element_out}`).join(", ")}`));
        target.append(ui.table(payload.picks || [], [
            { key: "position", label: "Slot", num: true },
            { key: "player", label: "Player", value: (row) => (row.player || {}).web_name, render: (row) => `${(row.player || {}).web_name || row.element}${row.is_captain ? " (C)" : row.is_vice_captain ? " (V)" : ""}` },
            { key: "multiplier", label: "×", num: true },
        ], { sortKey: "position", sortDir: "asc", footer: false }));
    },
};

/* ---------- My team optimizer ---------- */
const optimizerView = {
    MODES: {
        transfers: { label: "Transfers", detail: "Use your free transfers (hits cost 4 points each)" },
        wildcard: { label: "Wildcard", detail: "Best squad for the next 5 gameweeks within your budget" },
        freehit: { label: "Free Hit", detail: "Best squad for this gameweek only; your squad returns afterwards" },
    },
    mode: "transfers",

    /* Inputs come from the working squad (POST .../team), so plans start from it. */
    controls(target, session) {
        const team = session.team;
        clear(target);
        if (!team.transfers_open) {
            target.append(h("p", null, statusBadge("neutral", `GW${team.gameweek} is closed, so it can't be planned.`)),
                h("button", { class: "btn small", type: "button", onClick: () => app.setGameweek(team.next_open_gameweek) }, `Plan from GW${team.next_open_gameweek}`));
            return;
        }
        const planCard = session.cards.plan;
        const estimate = team.free_transfers || {};
        const remaining = estimate.unlimited ? 15 : (team.free_transfers_remaining ?? estimate.free_transfers ?? 1);
        const inputs = {
            free: h("input", { type: "number", min: 0, max: 5, step: 1, value: Math.min(5, remaining), style: { width: "72px" }, "aria-label": "Free transfers", id: "opt-free" }),
            bank: h("input", { type: "number", min: 0, max: 100, step: 0.1, value: utils.fmt(Math.max(0, team.bank || 0), 1).replace(/,/g, ""), style: { width: "88px" }, "aria-label": "Bank in £m", id: "opt-bank" }),
            horizon: h("select", { "aria-label": "Gameweeks to plan for", id: "opt-horizon" }, [1, 2, 3, 4, 5].map((value) => h("option", { value, selected: value === 3 ? true : null }, `${value} GW${value > 1 ? "s" : ""}`))),
            max: h("select", { "aria-label": "Most transfers to consider", id: "opt-max" }, [0, 1, 2, 3, 4, 5, 6, 7, 8].map((value) => h("option", { value, selected: value === Math.min(8, Math.min(5, remaining) + 2) ? true : null }, `${value}`))),
        };
        const chips = (team.chips && team.chips.chips) || {};
        const chipNote = h("p", { class: "small muted", style: { margin: 0 } });
        const transferOnly = h("div", { class: "row" },
            h("label", { class: "small row", style: { gap: "6px" } }, "Plan over", inputs.horizon),
            h("label", { class: "small row", style: { gap: "6px" } }, "Up to", inputs.max, "transfers"));
        const switcher = h("div", { class: "segmented", role: "tablist", "aria-label": "Optimizer mode" });
        const description = h("p", { class: "small secondary", style: { margin: 0 } });
        const renderMode = () => {
            clear(switcher);
            Object.entries(optimizerView.MODES).forEach(([key, mode]) => {
                const label = key === "wildcard" ? `${mode.label} · 5 GWs` : key === "freehit" ? `${mode.label} · GW${team.gameweek}` : mode.label;
                switcher.append(h("button", {
                    type: "button", role: "tab", "aria-selected": optimizerView.mode === key ? "true" : "false",
                    onClick: () => { optimizerView.mode = key; renderMode(); },
                }, label));
            });
            description.textContent = optimizerView.MODES[optimizerView.mode].detail;
            transferOnly.hidden = optimizerView.mode !== "transfers";
            const status = chips[optimizerView.mode];
            chipNote.textContent = status && !status.available_this_half
                ? `${optimizerView.MODES[optimizerView.mode].label} already played this half (GW${status.played.join(", GW")}), so this is a what-if.`
                : status ? `${optimizerView.MODES[optimizerView.mode].label} looks available this half (${team.chips.half}, estimate).` : "";
        };
        renderMode();

        const trail = (estimate.trail || []).map((row) => h("li", null,
            `GW${row.gameweek}: ${row.note}${row.available !== undefined ? ` (had ${row.available})` : ""} → ${row.available_next} for the next gameweek`));
        const locks = Object.entries(session.overrides).filter(([, item]) => item.lock || item.ban);
        add(target,
            team.edited ? h("p", { class: "small", style: { marginTop: 0 } }, chip("Working squad", "lime"), ` Plans start from your edited squad${(team.changes || {}).count ? ` after its ${team.changes.count} transfer${team.changes.count > 1 ? "s" : ""}` : ""}.`) : null,
            switcher, description,
            h("div", { class: "row", style: { marginTop: "10px" } },
                h("label", { class: "small row", style: { gap: "6px" } }, "Free transfers", inputs.free),
                chip(estimate.unlimited ? "unlimited before the first deadline" : `estimated ${estimate.free_transfers}${team.edited && team.free_transfers_remaining !== estimate.free_transfers ? `, ${team.free_transfers_remaining} left` : ""}`, "aqua"),
                h("label", { class: "small row", style: { gap: "6px" } }, "Bank £m", inputs.bank)),
            transferOnly,
            chipNote,
            locks.length ? h("p", { class: "small secondary", style: { margin: "6px 0 0" } }, `Respecting ${locks.map(([id, item]) => `${item.lock ? "lock" : "ban"} ${item.web_name || `#${id}`}`).join(", ")}.`) : null,
            h("div", { class: "btn-row", style: { marginTop: "12px" } },
                h("button", { class: "btn primary", type: "button", id: "opt-run", onClick: () => optimizerView.run(session, inputs, planCard) }, "Optimize")),
            h("p", { class: "small secondary" }, `Budget £${utils.fmt(team.budget, 1)}m = bank £${utils.fmt(team.bank, 1)}m + squad selling value £${utils.fmt(team.squad_value, 1)}m. Squad from GW${team.picks_gameweek}${team.edited ? " plus your edits" : ""}; planning GW${team.gameweek}.`),
            h("details", null, h("summary", { class: "small" }, "How free transfers were estimated"),
                h("p", { class: "small muted" }, estimate.note || ""), h("ul", { class: "small secondary" }, trail)),
            h("details", null, h("summary", { class: "small" }, "Estimated selling prices"),
                ui.table((team.squad || []).filter((row) => !row.unknown), [
                    { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, h("span", { class: "sub" }, `${row.team_name || ""} · ${utils.positionName(row.element_type)}`)) },
                    { key: "purchase_price", label: "Bought", num: true, digits: 1, title: "Estimated purchase price (fix it on the pitch in Edit)" },
                    { key: "price", label: "Now", num: true, digits: 1 },
                    { key: "selling_price", label: "Sells for", num: true, digits: 1 },
                    { key: "purchase_source", label: "From", render: (row) => h("span", { class: "small muted" }, row.purchase_source || "") },
                ], { footer: false, sortKey: "selling_price" })),
            h("ul", { class: "small muted", style: { paddingLeft: "18px", marginBottom: 0 } }, (team.caveats || []).map((line) => h("li", null, line))));
    },

    async run(session, inputs, planCard) {
        const request = views.myteam.requestBody(session);
        const body = {
            entry_id: session.entryId,
            gameweek: state.gameweek,
            mode: optimizerView.mode,
            free_transfers: Math.max(0, Math.min(5, Number(inputs.free.value) || 0)),
            bank: Math.max(0, Number(inputs.bank.value) || 0),
            overrides: request.overrides,
        };
        if (request.draft) body.draft = request.draft;
        if (optimizerView.mode === "transfers") {
            body.horizon = Number(inputs.horizon.value) || 3;
            body.max_transfers = Number(inputs.max.value);
        }
        const label = optimizerView.MODES[optimizerView.mode].label;
        clear(planCard.body).append(h("p", { class: "muted" }, `${label}: projecting the gameweeks and solving… (the first run of a horizon can take a few seconds)`));
        await ui.fill(planCard, async (target) => {
            const result = await api.post("/api/admin/optimize", body, undefined, { timeoutMs: CONFIG.slowTimeoutMs });
            optimizerView.results(target, result);
        });
    },

    gameweekLabel(result) {
        const gameweeks = result.gameweeks || [];
        return gameweeks.length > 1 ? `GW${gameweeks[0]}–${gameweeks[gameweeks.length - 1]}` : `GW${gameweeks[0]}`;
    },

    results(target, result) {
        clear(target);
        const holder = h("div", { class: "stack" });
        if (result.mode === "transfers") {
            target.append(h("h3", { class: "small muted", style: { margin: "0 0 6px" } },
                `Options over ${optimizerView.gameweekLabel(result)} with ${result.free_transfers ?? "unlimited"} free transfer${result.free_transfers === 1 ? "" : "s"}: click one to see it`));
            const options = result.plans.map((plan, index) => ({ ...plan, index }));
            const table = ui.table(options, [
                { key: "transfers", label: "Transfers", num: true, render: (row) => h("span", null, row.transfers === 0 ? "0 (roll)" : `${row.transfers}`, row.index === result.best_index ? " " : null, row.index === result.best_index ? chip("best", "lime") : null) },
                { key: "hits", label: "Hits", num: true, render: (row) => (row.feasible ? (row.hits ? `${row.hits} (−${utils.fmt(row.hit_cost, 0)})` : "0") : "–") },
                { key: "projected_total", label: "Projected", num: true, digits: 2 },
                { key: "net_total", label: "After hits", num: true, digits: 2 },
                { key: "net_gain", label: "Net gain", num: true, render: (row) => (row.feasible ? utils.fmtSigned(row.net_gain, 2) : row.reason) },
                { key: "bank_after", label: "Bank after", num: true, digits: 1 },
            ], { footer: false, sort: false, onRowClick: (row) => row.feasible && optimizerView.plan(holder, result, result.plans[row.index]) });
            target.append(table);
        }
        target.append(holder);
        optimizerView.plan(holder, result, result.plans[result.best_index]);
        if ((result.notes || []).length) target.append(h("ul", { class: "small muted", style: { paddingLeft: "18px" } }, result.notes.map((note) => h("li", null, note))));
    },

    plan(holder, result, plan) {
        clear(holder);
        const baseline = result.baseline || {};
        const names = Object.fromEntries((plan.squad || []).map((player) => [player.id, player.web_name]));
        const label = optimizerView.gameweekLabel(result);
        const heading = result.mode === "transfers"
            ? `Showing the ${plan.transfers === 0 ? "roll (no transfer)" : `${plan.transfers}-transfer`} plan${plan.index === result.best_index || result.plans[result.best_index] === plan ? ", the best after hits" : ""}`
            : `${optimizerView.MODES[result.mode].label} squad for ${label}`;
        holder.append(h("h3", { style: { margin: "12px 0 0", fontSize: "15px" } }, heading));
        holder.append(ui.tiles([
            { label: `Projected, ${label}`, value: utils.fmt(plan.projected_total, 1), detail: `current squad ${utils.fmt(baseline.projected_total, 1)}` },
            { label: "Net gain", value: utils.fmtSigned(plan.net_gain, 2), detail: plan.hits ? `after ${plan.hits} hit${plan.hits > 1 ? "s" : ""} (−${utils.fmt(plan.hit_cost, 0)})` : "no hits" },
            { label: result.mode === "transfers" ? "Transfers" : "Players changed", value: `${plan.transfers}`, detail: result.mode === "transfers" ? `${result.free_transfers ?? "unlimited"} free` : "free with the chip" },
            { label: "Bank after", value: `£${utils.fmt(plan.bank_after, 1)}m`, detail: `budget £${utils.fmt(result.budget, 1)}m` },
        ]));
        const moves = plan.moves || [];
        if (moves.length) {
            holder.append(h("h3", { class: "small muted", style: { margin: "4px 0 0" } }, `Moves (points over ${label})`));
            moves.forEach((move) => {
                const panel = h("div");
                holder.append(h("div", { class: "card", style: { background: "var(--surface-2)" } },
                    h("div", { class: "row" },
                        h("strong", null, `${move.out.web_name} → ${move.in.web_name}`),
                        chip(move.in.position),
                        chip(`${utils.fmtSigned(move.gain, 2)} pts`, move.gain >= 0 ? "lime" : ""),
                        chip(`sell £${utils.fmt(move.out.cost, 1)}m · buy £${utils.fmt(move.in.cost, 1)}m`),
                        h("span", { class: "spacer" }),
                        h("button", { class: "btn small", type: "button", onClick: () => views.myteam.explainMove(panel, move) }, "Explain this move")),
                    h("p", { class: "small secondary", style: { margin: "6px 0 0" } },
                        `${move.out.web_name} (${move.out.team_name}) ${move.out.points.map((value) => utils.fmt(value, 1)).join(" / ")} → ${move.in.web_name} (${move.in.team_name}) ${move.in.points.map((value) => utils.fmt(value, 1)).join(" / ")} projected per gameweek.`),
                    panel));
            });
        } else {
            holder.append(h("p", null, statusBadge("good", result.mode === "transfers" ? "Rolling the transfer is the best option." : "Your current squad is already the best within this budget.")));
        }
        const first = (plan.lineups || [])[0] || {};
        const grid = h("div", { class: "split" });
        const pitchHolder = h("div", null, h("h3", { class: "small muted", style: { margin: "0 0 6px" } }, `Planned squad · GW${first.gameweek} XI, captain ${names[first.captain_id] || "–"}`));
        grid.append(pitchHolder);
        const tableHolder = h("div");
        grid.append(tableHolder);
        holder.append(grid);
        pitchHolder.append(ui.pitch(plan.squad || [], { startingIds: first.starting_ids, value: (player) => (player.points || [])[0] }));
        tableHolder.append(h("h3", { class: "small muted", style: { margin: "0 0 6px" } }, "Each gameweek: best XI, captain and points"));
        tableHolder.append(ui.table(plan.lineups || [], [
            { key: "gameweek", label: "GW", num: true },
            { key: "captain_id", label: "Captain", render: (row) => names[row.captain_id] || "–" },
            { key: "points", label: "XI + captain", num: true, digits: 2 },
            { key: "bench_points", label: "Bench", num: true, digits: 2 },
        ], { footer: false, sortKey: "gameweek", sortDir: "asc" }));
        tableHolder.append(h("h3", { class: "small muted", style: { margin: "12px 0 6px" } }, "Squad"));
        tableHolder.append(ui.table(plan.squad || [], [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.new ? " " : null, row.new ? chip("IN", "aqua") : null, h("span", { class: "sub" }, `${row.team_name} · ${row.position}`)) },
            { key: "cost", label: "£", num: true, digits: 1, title: "Selling price for players you keep, price for new ones" },
            { key: "total", label: `xP ${label}`, num: true, digits: 2 },
        ], { footer: false, sortKey: "total", onRowClick: (row) => drawer.open(row.id, row.web_name) }));
    },
};

/* ---------- Model health ---------- */
views.models = {
    playedOnly: false,

    async render(main, context) {
        main.append(sectionTitle("Model health", "Each model, how they agree, the inputs they see, and how the forecasts have done"));
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const cardsHolder = h("div", { class: "grid span-12" });
        const ensemble = ui.card("Ensemble settings", null, { span: 4 });
        const coverage = ui.card("Feature coverage", "Inputs with data in the latest model input", { span: 8 });
        const agreement = ui.card("Model agreement", `GW${state.gameweek} projections`, { span: 6 });
        const spread = ui.card("Disagreements", "Players the models disagree on most", { span: 6 });
        const enrichment = ui.card("FPL Data enrichment", "Fills inputs official FPL does not publish", { span: 12 });
        const accuracy = ui.card("Live accuracy", "Archived pre-deadline forecasts against actual points");
        const offline = ui.card("Offline training metrics", "From the technical report (window.REPORT)");
        grid.append(cardsHolder);
        [ensemble, coverage, agreement, spread, enrichment, accuracy, offline].forEach((card) => grid.append(card.root));

        const payload = await ui.fill(ensemble, () => data.models());
        if (!payload || !context.alive()) return;
        views.models.cards(cardsHolder, payload);
        clear(ensemble.body).append(ui.kv([
            ["Minimum successful", payload.ensemble.minimum_successful_models],
            ["History window", `${payload.ensemble.history_window} matches`],
            ["Clip", `${payload.ensemble.clip_min ?? "–"} to ${payload.ensemble.clip_max ?? "∞"}`],
            ["Max per club", payload.ensemble.max_players_per_team],
            ["Cache fresh · stale", `${utils.fmtDuration(payload.ensemble.prediction_cache_ttl_seconds)} · ${utils.fmtDuration(payload.ensemble.prediction_stale_seconds)}`],
            ["GW1 cold start", payload.ensemble.cold_start_enabled ? "on" : "off"],
            ["Effective weights", Object.entries(payload.ensemble.effective_weights || {}).map(([name, weight]) => `${name} ${utils.fmt(weight, 2)}`).join(" · ")],
        ]));
        ensemble.actions.append(h("button", { class: "btn small danger", type: "button", onClick: () => app.action("Reset model overrides", "Remove every runtime weight and on/off override? The live forecast is predicted again with the configured models.", () => api.post("/api/admin/models/reset"), { okLabel: "Reset" }) }, "Reset overrides"));

        const coverageData = payload.feature_coverage || {};
        clear(coverage.body);
        if (coverageData.populated) {
            coverage.body.append(h("p", { class: "small secondary" }, `${coverageData.populated.length} of ${payload.feature_contract.length} model inputs have data; ${coverageData.missing.length} are entirely missing and left to each model's imputer.`));
            coverage.body.append(h("div", { class: "chips" },
                coverageData.populated.map((feature) => chip(feature, "lime")),
                coverageData.missing.map((feature) => h("span", { class: "chip", title: "No data: imputed" }, `✕ ${feature}`))));
        } else {
            charts.empty(coverage.body, "Coverage is recorded on the next prediction.");
        }

        const agree = payload.agreement || {};
        clear(agreement.body);
        if (agree.available) {
            const names = agree.models;
            const matrix = names.map((left) => names.map((right) => agree.correlation[left][right]));
            const lowest = Math.min(...matrix.flat().filter(utils.isNumber));
            const floor = Math.max(0, Math.floor((lowest - 0.02) * 20) / 20);
            agreement.body.append(h("p", { class: "small muted" }, `Pearson correlation between models' projections; colour runs from ${utils.fmt(floor, 2)} (dark) to 1.00 (light).`));
            charts.heatmap(agreement.body, names, matrix, { min: floor, max: 1 });
            agreement.body.append(ui.table(names.map((name) => ({ name, ...agree.distributions[name] })), [
                { key: "name", label: "Model", render: (row) => h("span", null, h("i", { class: "model-key", style: { background: CONFIG.series[names.indexOf(row.name) % CONFIG.series.length] } }), row.name) },
                { key: "mean", label: "Mean", num: true, digits: 2 },
                { key: "std", label: "SD", num: true, digits: 2 },
                { key: "max", label: "Max", num: true, digits: 2 },
                { key: "mean_abs_diff_from_ensemble", label: "|Δ| vs ensemble", num: true, digits: 3 },
                { key: "top_pick_overlap", label: "Top-30 overlap", num: true, render: (row) => utils.fmtPct(row.top_pick_overlap) },
            ], { footer: false }));
            agreement.body.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Spread across models (players per bucket)"));
            const histogram = agree.spread_histogram || { counts: [], edges: [] };
            charts.columns(agreement.body, histogram.counts.map((count, index) => ({ label: utils.fmt(histogram.edges[index], 2), title: `${utils.fmt(histogram.edges[index], 2)}–${utils.fmt(histogram.edges[index + 1], 2)} points`, value: count })), { height: 160, valueLabel: "Players", xLabel: "Standard deviation across models (points)" });
            clear(spread.body).append(ui.table(agree.disagreements || [], [
                { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, h("span", { class: "sub" }, row.team_name)) },
                { key: "expected_points", label: "Ensemble", num: true, digits: 2 },
                { key: "spread", label: "Spread", num: true, digits: 2 },
                ...names.map((name, index) => ({ key: `m:${name}`, label: name, num: true, value: (row) => row.models[name], render: (row) => h("span", null, h("i", { class: "model-key", style: { background: CONFIG.series[index % CONFIG.series.length] } }), utils.fmt(row.models[name], 2)) })),
            ], { sortKey: "spread", footer: false, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
        } else {
            charts.empty(agreement.body, agree.error || "Per-model projections are recorded from the next prediction (GW1 uses the ownership cold start).");
            charts.empty(spread.body, "Not available yet.");
        }

        views.models.enrichment(enrichment, payload);
        views.models.accuracy(accuracy, context);
        views.models.offline(offline.body);
    },

    cards(holder, payload) {
        payload.models.forEach((model, index) => {
            const card = ui.card(model.name, `${model.version || "no version"} · trained ${model.last_trained || "–"}`, { span: 3 });
            card.root.querySelector("h2").prepend(h("i", { class: "model-key", style: { background: CONFIG.series[index % CONFIG.series.length] } }));
            const run = model.last_run || {};
            const statusKind = { ok: "good", failed: "critical", disabled: "warning", "not-run": "neutral" }[run.status] || "neutral";
            const weight = h("input", { type: "number", min: 0.05, step: 0.05, value: utils.fmt(model.effective_weight, 2).replace(/,/g, ""), style: { width: "80px" }, "aria-label": `${model.name} weight` });
            const enabled = h("input", { type: "checkbox", checked: model.enabled ? true : null });
            add(card.body,
                h("div", { class: "row", style: { marginBottom: "8px" } }, statusBadge(statusKind, `last run: ${run.status}`), model.weight_override ? chip("weight overridden", "lime") : null, !model.enabled ? chip("disabled") : null),
                run.error ? h("p", { class: "error-text small" }, run.error) : null,
                ui.kv([
                    ["Estimator", model.estimator],
                    ["Pipeline", model.steps ? model.steps.map((step) => step[1]).join(" → ") : "–"],
                    ["Features", model.feature_contract.declared ? `${model.feature_contract.features} ${model.feature_contract.matches ? "✓ contract" : "✕ mismatch"}` : "not declared"],
                    ["File", model.file.exists ? `${utils.fmtBytes(model.file.bytes)} · ${utils.dateTime(model.file.modified_at)}` : "missing"],
                    ["Weight", `${utils.fmt(model.configured_weight, 2)} configured · ${utils.fmt(model.effective_weight, 2)} effective`],
                ]),
                h("div", { class: "row", style: { marginTop: "10px" } },
                    h("label", { class: "check small" }, enabled, "Enabled"),
                    h("label", { class: "small row", style: { gap: "4px" } }, "Weight", weight),
                    h("button", { class: "btn small", type: "button", onClick: () => {
                        const body = { enabled: enabled.checked, weight: Number(weight.value) || undefined };
                        app.action(`Update ${model.name}`, `Set ${model.name} to ${body.enabled ? `enabled with weight ${body.weight}` : "disabled"}? The live forecast is predicted again and the public squad can change. Lasts until restart.`, () => api.post(`/api/admin/models/${encodeURIComponent(model.name)}`, body), { okLabel: "Apply live" });
                    } }, "Apply live")));
            holder.append(card.root);
        });
    },

    enrichment(card, payload) {
        const info = payload.enrichment || {};
        const last = info.last_result || {};
        const provider = info.provider || {};
        const dataset = provider.dataset || {};
        const kind = last.status === "applied" ? "good" : ["stale", "unavailable", "rejected-low-match-ratio", "season-mismatch", "season-unverified"].includes(last.status) ? "warning" : "neutral";
        clear(card.body).append(h("div", { class: "split" },
            ui.kv([
                ["Status", statusBadge(kind, last.status || "not attempted")],
                ["Enabled", info.enabled ? "yes" : "no"],
                ["Season", info.season],
                ["From gameweek", info.start_gameweek],
                ["Permission", `${info.permission_status}${info.permission_pending_acknowledged ? " (acknowledged)" : ""}`],
                ["Remote downloads", provider.remote_download_allowed ? "allowed" : "local files only"],
                ["Match ratio", utils.isNumber(last.match_ratio) ? utils.fmtPct(last.match_ratio, 1) : "–"],
                ["Unenriched GWs", (last.unenriched_gameweeks || []).join(", ") || "none"],
                ["Error", last.error || provider.last_refresh_error || provider.last_error || "none"],
            ]),
            ui.kv([
                ["Dataset loaded", provider.loaded ? "yes" : "no"],
                ["Rows · players", `${utils.fmtInt(dataset.rows)} · ${utils.fmtInt(dataset.players)}`],
                ["Gameweeks", dataset.min_gameweek ? `${dataset.min_gameweek}–${dataset.max_gameweek} (observed ${dataset.latest_observed_gameweek})` : "–"],
                ["Size · checksum", `${utils.fmtBytes(dataset.bytes)} · ${dataset.sha256 || "–"}`],
                ["Last refresh attempt", utils.isNumber(provider.seconds_since_refresh_attempt) ? `${utils.fmtDuration(provider.seconds_since_refresh_attempt)} ago` : "–"],
                ["Next refresh", utils.isNumber(provider.next_refresh_in_seconds) ? `in ${utils.fmtDuration(provider.next_refresh_in_seconds)}` : "–"],
                ["Unmatched opponents", Object.entries(last.unmatched_opponents || {}).map(([team, count]) => `${team} (${count})`).join(", ") || "none"],
            ])));
        const filled = Object.entries(last.filled_values || {}).map(([feature, count]) => ({ label: feature, value: count }));
        if (filled.length) {
            card.body.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Values filled per input in the last prediction"));
            charts.hbars(card.body, filled.sort((a, b) => b.value - a.value), { format: (value) => utils.fmtInt(value), valueLabel: "Values filled" });
        }
        card.actions.append(h("button", { class: `btn small ${info.enabled ? "danger" : ""}`, type: "button", onClick: () => app.action(info.enabled ? "Turn enrichment off" : "Turn enrichment on", `${info.enabled ? "Stop" : "Start"} filling missing inputs from FPL Data? The live forecast is predicted again. Lasts until restart.`, () => api.post("/api/admin/enrichment", { enabled: !info.enabled }), { okLabel: info.enabled ? "Turn off" : "Turn on" }) }, info.enabled ? "Turn off (live)" : "Turn on (live)"));
    },

    async accuracy(card, context) {
        const toggle = h("input", { type: "checkbox", checked: views.models.playedOnly ? true : null });
        toggle.addEventListener("change", () => {
            views.models.playedOnly = toggle.checked;
            views.models.accuracy(card, context);
        });
        clear(card.actions).append(h("label", { class: "check small" }, toggle, "Only players who played"));
        await ui.fill(card, async (target) => {
            const result = await cache.get(`accuracy:${views.models.playedOnly}`, () => api.get("/api/admin/accuracy", { played_only: views.models.playedOnly }, { timeoutMs: CONFIG.slowTimeoutMs }), 120000);
            if (!context.alive()) return;
            clear(target);
            if (!result.archive_enabled) {
                target.append(h("p", { class: "empty" }, "The archive is disabled, so no past forecasts can be checked."));
                return;
            }
            const gameweeks = result.gameweeks || [];
            const summary = result.summary || {};
            if (!gameweeks.length) {
                target.append(h("p", { class: "empty" }, `No finished gameweek has a forecast archived before its deadline yet${(result.missing || []).length ? ` (missing: GW${result.missing.join(", GW")})` : ""}${(result.retrospective || []).length ? ` (made after the deadline: GW${result.retrospective.join(", GW")})` : ""}.`));
                return;
            }
            const report = globalThis.REPORT || {};
            const holdout = (report.holdout || []).find((row) => row.key === "equal_ensemble") || (report.holdout || [])[0];
            const offlineSpearman = utils.mean((report.per_gameweek || []).map((row) => row.spearman));
            target.append(ui.tiles([
                { label: "MAE (season)", value: utils.fmt(summary.mae, 3), detail: holdout ? `offline holdout ${utils.fmt(holdout.mae, 3)}` : null },
                { label: "RMSE (season)", value: utils.fmt(summary.rmse, 3), detail: holdout ? `offline holdout ${utils.fmt(holdout.rmse, 3)}` : null },
                { label: "Mean Spearman", value: utils.fmt(summary.mean_spearman, 3), detail: offlineSpearman ? `offline ${utils.fmt(offlineSpearman, 3)}` : null },
                { label: "Bias", value: utils.fmtSigned(summary.bias, 3), detail: "projected minus actual" },
                { label: "AI squad points", value: utils.fmt(summary.squad_actual_total, 0), detail: summary.average_entry_total ? `average manager ${utils.fmt(summary.average_entry_total, 0)}` : `projected ${utils.fmt(summary.squad_projected_total, 0)}` },
                { label: "Beat the average", value: `${summary.beat_average ?? 0}/${gameweeks.length}`, detail: "gameweeks" },
            ]));
            const grid = h("div", { class: "split", style: { marginTop: "12px" } });
            target.append(grid);
            // Every holder joins the grid before any chart measures its width.
            const titles = ["Mean absolute error per gameweek", "Rank correlation (Spearman) per gameweek", "AI squad points per gameweek", "Calibration: mean points per projection decile"];
            const holders = titles.map((title) => {
                const holder = h("div");
                grid.append(h("div", null, h("h3", { class: "small muted", style: { margin: "0 0 4px" } }, title), holder));
                return holder;
            });
            const block = (title) => holders[titles.indexOf(title)];
            const points = (key) => gameweeks.map((gw) => ({ x: gw.gameweek, y: gw[key] }));
            charts.lines(block("Mean absolute error per gameweek"), [{ name: "MAE", color: CONFIG.series[0], points: points("mae") }], { references: holdout ? [{ y: holdout.mae, label: "offline holdout" }] : [], format: (value) => utils.fmt(value, 3), yMin: 0 });
            charts.lines(block("Rank correlation (Spearman) per gameweek"), [{ name: "Spearman", color: CONFIG.series[0], points: points("spearman") }], { references: offlineSpearman ? [{ y: offlineSpearman, label: "offline mean" }] : [], format: (value) => utils.fmt(value, 3) });
            charts.lines(block("AI squad points per gameweek"), [
                { name: "AI squad (actual)", color: CONFIG.series[0], points: gameweeks.map((gw) => ({ x: gw.gameweek, y: gw.squad && gw.squad.actual })) },
                { name: "AI squad (projected)", color: CONFIG.series[1], points: gameweeks.map((gw) => ({ x: gw.gameweek, y: gw.squad && gw.squad.projected })) },
                { name: "Average manager", color: CONFIG.series[2], points: gameweeks.map((gw) => ({ x: gw.gameweek, y: gw.squad && gw.squad.average_entry_score })) },
            ], { format: (value) => utils.fmt(value, 1), yMin: 0 });
            const calibration = summary.calibration || [];
            const offlineCalibration = report.calibration || [];
            charts.lines(block("Calibration: mean points per projection decile"), [
                { name: "Live projected", color: CONFIG.series[1], points: calibration.map((row) => ({ x: row.decile, y: row.predicted })) },
                { name: "Live actual", color: CONFIG.series[0], points: calibration.map((row) => ({ x: row.decile, y: row.actual })) },
                { name: "Offline actual", color: CONFIG.series[2], points: offlineCalibration.map((row) => ({ x: row.decile, y: row.actual })) },
            ], { xFormat: (value) => `D${value}`, format: (value) => utils.fmt(value, 2), yMin: 0 });

            const modelNames = [...new Set(gameweeks.flatMap((gw) => Object.keys(gw.models || {})))];
            target.append(h("h3", { class: "small muted", style: { margin: "16px 0 4px" } }, "Per gameweek"));
            target.append(ui.table(gameweeks, [
                { key: "gameweek", label: "GW", num: true },
                { key: "n", label: "Players", num: true },
                { key: "mae", label: "MAE", num: true, digits: 3 },
                { key: "rmse", label: "RMSE", num: true, digits: 3 },
                { key: "bias", label: "Bias", num: true, digits: 3 },
                { key: "spearman", label: "Spearman", num: true, digits: 3 },
                { key: "top_precision", label: "Top-20 hit", num: true, render: (row) => utils.fmtPct(row.top_precision) },
                ...modelNames.map((name) => ({ key: `model:${name}`, label: `${name} MAE`, num: true, digits: 3, value: (row) => ((row.models || {})[name] || {}).mae ?? null })),
                { key: "squad", label: "AI squad", num: true, value: (row) => row.squad && row.squad.actual, render: (row) => row.squad && utils.isNumber(row.squad.actual) ? `${utils.fmt(row.squad.actual, 0)} (avg ${utils.fmt(row.squad.average_entry_score, 0)})` : "–" },
                { key: "captain", label: "Captain", value: (row) => row.squad && row.squad.captain, render: (row) => row.squad && row.squad.captain ? `${row.squad.captain} ${utils.fmt(row.squad.captain_actual, 0)} · best ${row.squad.best_captain} ${utils.fmt(row.squad.best_captain_actual, 0)}` : "–" },
            ], { sortKey: "gameweek", footer: false }));
            const positions = {};
            gameweeks.forEach((gw) => Object.entries(gw.by_position || {}).forEach(([position, metrics]) => {
                const entry = positions[position] || { position, n: 0, mae: 0, predicted: 0, actual: 0 };
                entry.n += metrics.n;
                entry.mae += (metrics.mae || 0) * metrics.n;
                entry.predicted += (metrics.predicted_mean || 0) * metrics.n;
                entry.actual += (metrics.actual_mean || 0) * metrics.n;
                positions[position] = entry;
            }));
            target.append(h("h3", { class: "small muted", style: { margin: "16px 0 4px" } }, "By position (season)"));
            target.append(ui.table(Object.values(positions).map((row) => ({ position: row.position, n: row.n, mae: row.n ? row.mae / row.n : null, predicted: row.n ? row.predicted / row.n : null, actual: row.n ? row.actual / row.n : null })), [
                { key: "position", label: "Pos." },
                { key: "n", label: "Rows", num: true },
                { key: "mae", label: "MAE", num: true, digits: 3 },
                { key: "predicted", label: "Mean projected", num: true, digits: 2 },
                { key: "actual", label: "Mean actual", num: true, digits: 2 },
            ], { footer: false }));
            if ((result.missing || []).length) target.append(h("p", { class: "small muted" }, `No archived forecast or live data for GW${result.missing.join(", GW")}.`));
            if ((result.retrospective || []).length) target.append(h("p", { class: "small muted" }, `Left out: GW${result.retrospective.join(", GW")}, whose archived forecast was made after the deadline.`));
        });
    },

    offline(target) {
        const report = globalThis.REPORT;
        clear(target);
        if (!report) {
            charts.empty(target, "The report data did not load.");
            return;
        }
        const meta = report.meta || {};
        target.append(ui.kv([
            ["Trained", meta.trained],
            ["Rows", utils.fmtInt(meta.rows)],
            ["Features", Array.isArray(meta.features) ? meta.features.length : meta.features],
            ["Seasons", Array.isArray(meta.seasons) ? meta.seasons.join(", ") : meta.seasons],
            ["Window", meta.window],
        ]));
        target.append(h("div", { class: "split", style: { marginTop: "10px" } },
            h("div", null, h("h3", { class: "small muted" }, "Holdout"), ui.table(report.holdout || [], [
                { key: "label", label: "Model" },
                { key: "rmse", label: "RMSE", num: true, digits: 3 },
                { key: "mae", label: "MAE", num: true, digits: 3 },
                { key: "r2", label: "R²", num: true, digits: 3 },
            ], { sortKey: "rmse", sortDir: "asc", footer: false })),
            h("div", null, h("h3", { class: "small muted" }, "Cross-validation"), ui.table(report.cv_summary || [], [
                { key: "label", label: "Model" },
                { key: "rmse", label: "RMSE", num: true, digits: 3 },
                { key: "std", label: "± SD", num: true, digits: 3 },
                { key: "mae", label: "MAE", num: true, digits: 3 },
            ], { sortKey: "rmse", sortDir: "asc", footer: false }))));
        target.append(h("p", { class: "small" }, h("a", { href: "/static/report/index.html", target: "_blank", rel: "noopener" }, "Open the full technical report")));
    },
};

/* ---------- What-if sandbox ---------- */
views.sandbox = {
    settings: { weights: {}, disabled: [], locks: [], excludes: [], availability: {}, maxPerClub: 3, labels: {} },

    async render(main, context) {
        main.append(sectionTitle("What-if sandbox", "Re-select the squad under your own settings. Nothing here changes the public site until you apply it."));
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const controls = ui.card("Settings", "Model weights (0 = off), constraints and availability", { span: 5 });
        const results = ui.card("Result", "POST /api/admin/simulate", { span: 7 });
        [controls, results].forEach((card) => grid.append(card.root));
        const payload = await ui.fill(controls, () => data.models());
        if (!payload || !context.alive()) return;
        views.sandbox.controls(controls.body, payload, results);
        clear(results.body).append(h("p", { class: "empty" }, "Adjust the settings and run the simulation."));
    },

    controls(target, payload, results) {
        const settings = views.sandbox.settings;
        clear(target);
        const models = payload.models || [];
        models.forEach((model) => {
            if (settings.weights[model.name] === undefined) settings.weights[model.name] = model.enabled ? model.effective_weight : 0;
        });
        const sliders = h("div", { class: "stack" }, models.map((model, index) => {
            const output = h("span", { class: "num small" }, utils.fmt(settings.weights[model.name], 2));
            const slider = h("input", { type: "range", min: 0, max: 3, step: 0.05, value: settings.weights[model.name], "aria-label": `${model.name} weight` });
            slider.addEventListener("input", () => {
                settings.weights[model.name] = Number(slider.value);
                output.textContent = settings.weights[model.name] === 0 ? "off" : utils.fmt(settings.weights[model.name], 2);
            });
            return h("div", { class: "slider-row" }, h("span", { class: "small" }, h("i", { class: "model-key", style: { background: CONFIG.series[index % CONFIG.series.length] } }), model.name), slider, output);
        }));
        const club = h("select", { "aria-label": "Max players per club" }, [1, 2, 3, 4, 5].map((value) => h("option", { value, selected: value === settings.maxPerClub ? true : null }, `${value} per club`)));
        club.addEventListener("change", () => { settings.maxPerClub = Number(club.value); });

        const chipsFor = (list, label) => {
            const holder = h("div", { class: "chips" });
            const renderChips = () => {
                clear(holder);
                list.forEach((id) => holder.append(h("button", { class: "btn small", type: "button", title: "Remove", onClick: () => { list.splice(list.indexOf(id), 1); renderChips(); } }, `${settings.labels[id] || id} ✕`)));
                if (!list.length) holder.append(h("span", { class: "muted small" }, label));
            };
            renderChips();
            return { holder, renderChips };
        };
        const locks = chipsFor(settings.locks, "No locked players");
        const excludes = chipsFor(settings.excludes, "No excluded players");
        const availabilityHolder = h("div", { class: "stack" });
        const renderAvailability = () => {
            clear(availabilityHolder);
            Object.entries(settings.availability).forEach(([id, value]) => {
                const output = h("span", { class: "num small" }, `${Math.round(value * 100)}%`);
                const slider = h("input", { type: "range", min: 0, max: 1, step: 0.05, value, "aria-label": `${settings.labels[id]} availability` });
                slider.addEventListener("input", () => { settings.availability[id] = Number(slider.value); output.textContent = `${Math.round(settings.availability[id] * 100)}%`; });
                availabilityHolder.append(h("div", { class: "slider-row" },
                    h("button", { class: "btn small ghost", type: "button", title: "Remove", onClick: () => { delete settings.availability[id]; renderAvailability(); } }, `${settings.labels[id] || id} ✕`), slider, output));
            });
            if (!Object.keys(settings.availability).length) availabilityHolder.append(h("span", { class: "muted small" }, "Use current official availability"));
        };
        renderAvailability();
        const remember = (player) => { settings.labels[player.id] = player.web_name; };

        add(target,
            h("h3", { class: "small muted" }, "Model weights"), sliders,
            h("h3", { class: "small muted" }, "Club limit"), club,
            h("h3", { class: "small muted" }, "Lock in"), ui.playerSearch((player) => { remember(player); if (!settings.locks.includes(player.id)) settings.locks.push(player.id); locks.renderChips(); }, "Lock a player…"), locks.holder,
            h("h3", { class: "small muted" }, "Exclude"), ui.playerSearch((player) => { remember(player); if (!settings.excludes.includes(player.id)) settings.excludes.push(player.id); excludes.renderChips(); }, "Exclude a player…"), excludes.holder,
            h("h3", { class: "small muted" }, "What if availability were…"), ui.playerSearch((player) => { remember(player); settings.availability[player.id] = settings.availability[player.id] ?? 1; renderAvailability(); }, "Pick a player…"), availabilityHolder,
            h("div", { class: "btn-row", style: { marginTop: "14px" } },
                h("button", { class: "btn primary", type: "button", onClick: () => views.sandbox.run(results) }, "Run simulation"),
                h("button", { class: "btn ghost", type: "button", onClick: () => {
                    views.sandbox.settings = { weights: {}, disabled: [], locks: [], excludes: [], availability: {}, maxPerClub: 3, labels: {} };
                    views.sandbox.controls(target, payload, results);
                } }, "Reset"),
                h("button", { class: "btn danger", type: "button", onClick: () => views.sandbox.applyLive(payload) }, "Apply weights live")),
            h("p", { class: "small muted" }, "Locks and exclusions apply only here: the live squad always follows the projections."));
    },

    request() {
        const settings = views.sandbox.settings;
        return {
            gameweek: state.gameweek,
            weights: { ...settings.weights },
            exclude_ids: [...settings.excludes],
            lock_ids: [...settings.locks],
            max_per_club: settings.maxPerClub,
            availability_overrides: { ...settings.availability },
        };
    },

    async run(results) {
        await ui.fill(results, async (target) => {
            const result = await api.post("/api/admin/simulate", views.sandbox.request(), undefined, { timeoutMs: CONFIG.slowTimeoutMs });
            clear(target);
            const diff = result.diff || {};
            const gain = (diff.projected_total_sandbox || 0) - (diff.projected_total_live || 0);
            target.append(ui.tiles([
                { label: "Sandbox squad total", value: utils.fmt(diff.projected_total_sandbox, 2), detail: `live ${utils.fmt(diff.projected_total_live, 2)} (${utils.fmtSigned(gain, 2)})` },
                { label: "Best XI + captain", value: utils.fmt(result.lineup && result.lineup.projected_total, 2), detail: `formation ${result.lineup && result.lineup.formation}` },
                { label: "Captain", value: diff.captain_sandbox ? diff.captain_sandbox.web_name : "–", detail: diff.captain_live ? `live: ${diff.captain_live.web_name}` : null },
                { label: "Changes", value: `${(diff.in || []).length} in / ${(diff.out || []).length} out`, detail: `weights: ${Object.entries(result.weights || {}).map(([name, weight]) => `${name} ${utils.fmt(weight, 2)}`).join(", ")}` },
            ]));
            if (result.inputs && result.inputs.approximate) target.append(h("p", { class: "notes" }, `Inputs were rebuilt (${result.inputs.origin}) with current availability.`));
            target.append(h("div", { class: "split", style: { marginTop: "12px" } },
                h("div", null, h("h3", { class: "small muted" }, "In"), ui.table(diff.in || [], [
                    { key: "web_name", label: "Player", render: (row) => `${row.web_name} (${row.team_name}, ${row.position})` },
                    { key: "expected_points", label: "xP", num: true, digits: 2 },
                ], { footer: false, empty: "No new players" })),
                h("div", null, h("h3", { class: "small muted" }, "Out"), ui.table(diff.out || [], [
                    { key: "web_name", label: "Player", render: (row) => `${row.web_name} (${row.team_name}, ${row.position})` },
                    { key: "expected_points", label: "xP", num: true, digits: 2 },
                ], { footer: false, empty: "Nobody dropped" }))));
            const rejected = Object.entries(result.rejected_locks || {});
            if (rejected.length) target.append(h("p", { class: "notes" }, rejected.map(([id, decision]) => `${views.sandbox.settings.labels[id] || id}: ${decision.reason}`).join(" · ")));
            target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Sandbox squad"));
            target.append(ui.table(result.squad || [], [
                { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, row.role ? " " : null, row.role ? chip(row.role === "captain" ? "C" : "V", "lime") : null, h("span", { class: "sub" }, `${row.team_name} · ${utils.positionName(row.element_type)}`)) },
                { key: "expected_points", label: "xP", num: true, digits: 2 },
                { key: "availability_factor", label: "Avail.", render: ui.availability },
                { key: "decision_reason", label: "Why", render: (row) => h("span", { class: "small secondary" }, row.decision_reason || "–") },
            ], { sortKey: "expected_points", footer: false, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
            if ((result.movers || []).length) {
                target.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Biggest projection changes against live"));
                charts.hbars(target, result.movers.map((row) => ({ label: `${row.web_name} (${row.team_name})`, value: row.delta, tooltip: [{ label: "Live", value: utils.fmt(row.live, 2) }, { label: "Sandbox", value: utils.fmt(row.sandbox, 2) }] })), { diverging: true, format: (value) => utils.fmtSigned(value, 2) });
            }
        });
    },

    async applyLive(payload) {
        const settings = views.sandbox.settings;
        const changes = (payload.models || []).map((model) => {
            const weight = settings.weights[model.name];
            return { name: model.name, enabled: weight > 0, weight: weight > 0 ? weight : undefined };
        });
        const summary = changes.map((change) => `${change.name}: ${change.enabled ? utils.fmt(change.weight, 2) : "off"}`).join(", ");
        await app.action("Apply weights live", `Apply these weights to the live service? ${summary}. The public squad is predicted again. Lasts until restart.`, async () => {
            for (const change of changes.filter((item) => item.enabled)) {
                await api.post(`/api/admin/models/${encodeURIComponent(change.name)}`, { enabled: true, weight: change.weight });
            }
            for (const change of changes.filter((item) => !item.enabled)) {
                await api.post(`/api/admin/models/${encodeURIComponent(change.name)}`, { enabled: false });
            }
            return changes;
        }, { okLabel: "Apply live", success: () => "Live model weights updated", rerender: false });
    },
};

/* ---------- FPL data ---------- */
views.data = {
    playerFilters: { search: "", team_id: "", element_type: "", status: "", min_price: "", max_price: "", order_by: "total_points", descending: true, selectable_only: true, offset: 0 },

    async render(main, context) {
        main.append(sectionTitle("FPL data", "Everything the official API exposes, through OpenFPL's mapped endpoints"));
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const fdr = ui.card("Fixture difficulty — next 6 gameweeks", "/api/fpl/fixtures?future_only=true · easiest run first");
        const fixtures = ui.card(`GW${state.gameweek} fixtures and results`, "/api/fpl/fixtures · click Stats for /api/fpl/fixtures/{id}/stats", { span: 6 });
        const live = ui.card(`GW${state.gameweek} top performers`, "/api/fpl/gameweeks/{gw}/live", { span: 6 });
        const players = ui.card("Player explorer", "/api/fpl/players with every filter");
        const strength = ui.card("Club strength", "/api/fpl/teams", { span: 6 });
        const dream = ui.card("Dream teams", "/api/fpl/dream-team and /api/fpl/gameweeks/{gw}/dream-team", { span: 6 });
        const setPieces = ui.card("Set-piece notes", "/api/fpl/set-piece-notes", { span: 6 });
        const rankings = ui.card("Winners and rankings", "Gameweek and phase winners, best leagues, most valuable teams, regions", { span: 6 });
        [fdr, fixtures, live, players, strength, dream, setPieces, rankings].forEach((card) => grid.append(card.root));

        await ui.fill(fdr, (target) => views.data.fdr(target));
        if (!context.alive()) return;
        ui.fill(fixtures, (target) => views.data.fixtures(target));
        ui.fill(live, (target) => views.data.live(target));
        ui.fill(players, (target) => views.data.players(target));
        ui.fill(strength, (target) => views.data.strength(target));
        ui.fill(dream, (target) => views.data.dream(target));
        ui.fill(setPieces, (target) => views.data.setPieces(target));
        ui.fill(rankings, (target) => views.data.rankings(target));
    },

    async fdr(target) {
        const [fixtures, teams] = await Promise.all([api.get("/api/fpl/fixtures", { future_only: true }), data.teams()]);
        const upcoming = [...new Set((fixtures.results || []).map((fixture) => fixture.gameweek).filter((gw) => gw !== null && gw !== undefined))].sort((a, b) => a - b).slice(0, 6);
        const rows = (teams.results || []).map((team) => {
            const cells = upcoming.map((gw) => (fixtures.results || []).filter((fixture) => fixture.gameweek === gw && (fixture.home_team.id === team.id || fixture.away_team.id === team.id)).map((fixture) => {
                const home = fixture.home_team.id === team.id;
                const opponent = home ? fixture.away_team : fixture.home_team;
                return { label: `${opponent.short_name || opponent.name} (${home ? "H" : "A"})`, difficulty: home ? fixture.home_team.difficulty : fixture.away_team.difficulty };
            }));
            const difficulties = cells.flat().map((cell) => cell.difficulty).filter(utils.isNumber);
            return { team: team.name, short: team.short_name, cells, average: utils.mean(difficulties), count: difficulties.length };
        });
        clear(target);
        if (!upcoming.length) {
            target.append(h("p", { class: "empty" }, "No upcoming fixtures."));
            return;
        }
        target.append(h("p", { class: "small muted" }, "Official FDR: 1–2 blue (easier), 3 neutral, 4–5 red (harder). Blank = no fixture; two chips = double gameweek."));
        target.append(ui.table(rows, [
            { key: "team", label: "Club" },
            { key: "average", label: "Avg FDR", num: true, digits: 2 },
            ...upcoming.map((gw, index) => ({ key: `gw${gw}`, label: `GW${gw}`, sort: false, render: (row) => row.cells[index].length ? h("span", { class: "row", style: { gap: "3px" } }, row.cells[index].map((cell) => ui.fdr(cell.difficulty, cell.label))) : h("span", { class: "muted small" }, "Blank") })),
        ], { sortKey: "average", sortDir: "asc", footer: false }));
    },

    async fixtures(target) {
        const response = await api.get("/api/fpl/fixtures", { gameweek: state.gameweek });
        clear(target).append(ui.table(response.results || [], [
            { key: "kickoff_time", label: "Kick-off", render: (row) => utils.dateTime(row.kickoff_time) },
            { key: "home", label: "Match", sort: false, render: (row) => h("span", null, `${row.home_team.name} `, h("strong", null, row.started ? `${row.home_team.score ?? 0} – ${row.away_team.score ?? 0}` : "v"), ` ${row.away_team.name}`) },
            { key: "state", label: "State", sort: false, render: (row) => row.finished ? statusBadge("good", "finished") : row.started ? statusBadge("warning", `live ${row.minutes ?? ""}'`) : statusBadge("neutral", "upcoming") },
            { key: "stats", label: "", sort: false, render: (row) => row.started ? h("button", { class: "btn small", type: "button", onClick: (event) => { event.stopPropagation(); views.data.fixtureStats(row); } }, "Stats") : "" },
        ], { sortKey: "kickoff_time", sortDir: "asc", footer: false, empty: "No fixtures in this gameweek." }));
    },

    async fixtureStats(fixture) {
        const body = dialogs.modal(`${fixture.home_team.name} ${fixture.home_team.score ?? ""} – ${fixture.away_team.score ?? ""} ${fixture.away_team.name}`, h("p", { class: "muted" }, "Loading…"));
        try {
            const response = await api.get(`/api/fpl/fixtures/${fixture.id}/stats`);
            const payload = response.data || {};
            const stats = Object.entries(payload).filter(([key, value]) => Array.isArray(value) && value.length && typeof value[0] === "object");
            clear(body);
            if (!stats.length) body.append(ui.json(payload));
            stats.forEach(([key, items]) => {
                body.append(h("h3", { class: "small muted" }, key.replace(/_/g, " ")));
                body.append(ui.autoTable(items.map((item) => ({ player: (item.player || {}).web_name || item.element, value: item.value, ...item })), { keys: ["player", "value"] }));
            });
        } catch (error) {
            clear(body).append(ui.errorBox(error));
        }
    },

    async live(target) {
        const response = await api.get(`/api/fpl/gameweeks/${state.gameweek}/live`);
        const elements = ((response.data || {}).elements || []).map((element) => ({ ...element, ...(element.stats || {}) }));
        const top = elements.filter((element) => (element.minutes || 0) > 0).sort((a, b) => (b.total_points || 0) - (a.total_points || 0)).slice(0, 25);
        clear(target);
        if (!top.length) {
            target.append(h("p", { class: "empty" }, "No minutes played yet in this gameweek."));
            return;
        }
        target.append(ui.table(top, [
            { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, h("span", { class: "sub" }, `${row.team_name} · ${row.position || ""}`)) },
            { key: "total_points", label: "Pts", num: true },
            { key: "minutes", label: "Min", num: true },
            { key: "goals_scored", label: "G", num: true },
            { key: "assists", label: "A", num: true },
            { key: "bonus", label: "Bonus", num: true },
            { key: "bps", label: "BPS", num: true },
            { key: "expected_goals", label: "xG", num: true, digits: 2, value: (row) => Number(row.expected_goals) || 0 },
            { key: "expected_assists", label: "xA", num: true, digits: 2, value: (row) => Number(row.expected_assists) || 0 },
        ], { sortKey: "total_points", footer: false, onRowClick: (row) => drawer.open(row.element_id, row.web_name) }));
    },

    async players(target) {
        const filters = views.data.playerFilters;
        const teams = await data.teams();
        clear(target);
        const holder = h("div");
        const pager = h("div", { class: "table-foot" });
        const load = async () => {
            holder.classList.add("loading");
            try {
                const response = await api.get("/api/fpl/players", { ...filters, limit: 50 });
                clear(holder).append(ui.table(response.results || [], [
                    { key: "web_name", label: "Player", render: (row) => h("span", null, row.web_name, h("span", { class: "sub" }, `${row.team_name} · ${utils.positionName(row.element_type)}`)) },
                    { key: "price", label: "£", num: true, digits: 1 },
                    { key: "total_points", label: "Pts", num: true },
                    { key: "selected_by_percent", label: "Own %", num: true, digits: 1 },
                    { key: "minutes", label: "Min", num: true },
                    { key: "goals", label: "G", num: true },
                    { key: "assists", label: "A", num: true },
                    { key: "expected_goals", label: "xG", num: true, digits: 2 },
                    { key: "expected_assists", label: "xA", num: true, digits: 2 },
                    { key: "defensive_contribution", label: "DefCon", num: true },
                    { key: "status", label: "Status", render: (row) => h("span", { title: row.news || "" }, row.status === "a" ? statusBadge("good", "a") : statusBadge("warning", row.status || "?")) },
                ], { footer: false, sort: false, onRowClick: (row) => drawer.open(row.id, row.web_name) }));
                add(clear(pager),
                    h("span", null, `${filters.offset + 1}–${filters.offset + (response.count || 0)} of ${response.total}`),
                    h("span", { class: "btn-row" },
                        h("button", { class: "btn small", type: "button", disabled: filters.offset === 0 ? true : null, onClick: () => { filters.offset = Math.max(0, filters.offset - 50); load(); } }, "Previous"),
                        h("button", { class: "btn small", type: "button", disabled: filters.offset + 50 >= response.total ? true : null, onClick: () => { filters.offset += 50; load(); } }, "Next")));
            } catch (error) {
                clear(holder).append(ui.errorBox(error, load));
            } finally {
                holder.classList.remove("loading");
            }
        };
        const set = (key) => (event) => {
            const element = event.target;
            filters[key] = element.type === "checkbox" ? element.checked : element.value;
            filters.offset = 0;
            load();
        };
        const search = h("input", { type: "search", placeholder: "Search", value: filters.search, "aria-label": "Search players" });
        search.addEventListener("input", utils.debounce(set("search"), 300));
        const select = (key, options, label) => {
            const element = h("select", { "aria-label": label }, options.map(([value, text]) => h("option", { value, selected: String(filters[key]) === String(value) ? true : null }, text)));
            element.addEventListener("change", set(key));
            return element;
        };
        const number = (key, placeholder) => {
            const element = h("input", { type: "number", step: 0.5, min: 0, placeholder, value: filters[key], style: { width: "84px" }, "aria-label": placeholder });
            element.addEventListener("input", utils.debounce(set(key), 300));
            return element;
        };
        const selectable = h("input", { type: "checkbox", checked: filters.selectable_only ? true : null });
        selectable.addEventListener("change", set("selectable_only"));
        const descending = h("input", { type: "checkbox", checked: filters.descending ? true : null });
        descending.addEventListener("change", set("descending"));
        target.append(h("div", { class: "filters" },
            search,
            select("team_id", [["", "All clubs"], ...(teams.results || []).map((team) => [team.id, team.name])], "Club"),
            select("element_type", [["", "All positions"], [1, "GK"], [2, "DEF"], [3, "MID"], [4, "FWD"]], "Position"),
            select("status", [["", "Any status"], ["a", "Available"], ["d", "Doubtful"], ["i", "Injured"], ["s", "Suspended"], ["u", "Unavailable"]], "Status"),
            number("min_price", "Min £"), number("max_price", "Max £"),
            select("order_by", [["total_points", "Sort: points"], ["price", "Sort: price"], ["selected_by_percent", "Sort: ownership"], ["web_name", "Sort: name"], ["id", "Sort: id"]], "Order"),
            h("label", { class: "check small" }, descending, "Descending"),
            h("label", { class: "check small" }, selectable, "Selectable only")), holder, pager);
        await load();
    },

    async strength(target) {
        const teams = await data.teams();
        const rows = teams.results || [];
        const fields = ["strength_attack_home", "strength_attack_away", "strength_defence_home", "strength_defence_away", "strength_overall_home", "strength_overall_away"];
        const max = Math.max(...rows.flatMap((row) => fields.map((field) => row[field] || 0)), 1);
        clear(target);
        const unset = fields.filter((field) => rows.every((row) => !row[field]));
        if (unset.length) target.append(h("p", { class: "small muted" }, `Official FPL currently publishes 0 for ${unset.map((field) => field.replace("strength_", "").replace(/_/g, " ")).join(", ")}.`));
        target.append(ui.table(rows, [
            { key: "name", label: "Club", render: (row) => h("span", null, row.name, h("span", { class: "sub" }, row.short_name)) },
            { key: "strength", label: "Str.", num: true },
            ...fields.map((field) => ({
                key: field, label: field.replace("strength_", "").replace("_", " "), num: true,
                render: (row) => h("div", { class: "row", style: { justifyContent: "flex-end", gap: "6px" } }, h("div", { class: "meter", style: { width: "50px" } }, h("span", { style: { width: `${((row[field] || 0) / max) * 100}%` } })), utils.fmtInt(row[field])),
            })),
        ], { sortKey: "strength_overall_home", footer: false }));
    },

    async dream(target) {
        clear(target);
        const block = async (title, path) => {
            const holder = h("div", null, h("h3", { class: "small muted" }, title));
            target.append(holder);
            try {
                const response = await api.get(path);
                const team = (response.data && response.data.team) || [];
                holder.append(team.length ? ui.table(team, [
                    { key: "player", label: "Player", value: (row) => (row.player || {}).web_name, render: (row) => `${(row.player || {}).web_name || row.element} (${(row.player || {}).team_name || ""})` },
                    { key: "points", label: "Pts", num: true },
                ], { sortKey: "points", footer: false, onRowClick: (row) => drawer.open(row.element, (row.player || {}).web_name) }) : h("p", { class: "empty" }, "Not published yet."));
                const top = response.data && response.data.top_player;
                if (top && top.player) holder.append(h("p", { class: "small secondary" }, `Top player: ${top.player.web_name} (${top.points} pts)`));
            } catch (error) {
                holder.append(ui.errorBox(error));
            }
        };
        await block(`GW${state.gameweek} dream team`, `/api/fpl/gameweeks/${state.gameweek}/dream-team`);
        await block("Season dream team", "/api/fpl/dream-team");
    },

    async setPieces(target) {
        const [notes, teams] = await Promise.all([data.setPieces(), data.teams()]);
        const names = Object.fromEntries((teams.results || []).map((team) => [team.id, team.name]));
        const list = ((notes.data || {}).teams || []).map((team) => ({ team: (team.team && team.team.name) || names[team.id] || `Team ${team.id}`, notes: (team.notes || []).map((note) => note.info_message).filter(Boolean) })).filter((team) => team.notes.length);
        clear(target);
        if (!list.length) {
            target.append(h("p", { class: "empty" }, "No set-piece notes published."));
            return;
        }
        target.append(h("div", { class: "stack", style: { maxHeight: "420px", overflowY: "auto" } }, list.sort((a, b) => a.team.localeCompare(b.team)).map((team) => h("div", null,
            h("strong", null, team.team),
            h("ul", { class: "reasons small" }, team.notes.map((note) => h("li", null, note)))))));
        if ((notes.data || {}).last_updated) target.append(h("p", { class: "small muted" }, `Updated ${utils.dateTime(notes.data.last_updated)}`));
    },

    async rankings(target) {
        clear(target);
        const phase = h("select", { "aria-label": "Phase" }, Array.from({ length: 11 }, (_, index) => h("option", { value: index + 1 }, `Phase ${index + 1}`)));
        const outputs = {
            winners: h("div"),
            phase: h("div"),
            leagues: h("div"),
            valuable: h("div"),
            regions: h("div"),
        };
        const load = async (holder, loader) => {
            clear(holder).append(h("span", { class: "muted small" }, "Loading…"));
            try {
                clear(holder).append(await loader());
            } catch (error) {
                clear(holder).append(ui.errorBox(error));
            }
        };
        const listOrJson = (payload) => {
            const items = Array.isArray(payload) ? payload : payload && Array.isArray(payload.results) ? payload.results : null;
            return items ? ui.autoTable(items, { maxColumns: 6, pageSize: 10 }) : ui.json(payload);
        };
        add(target,
            h("h3", { class: "small muted" }, `GW${state.gameweek} winners`), outputs.winners,
            h("div", { class: "row", style: { marginTop: "10px" } }, h("h3", { class: "small muted", style: { margin: 0 } }, "Phase winners"), phase), outputs.phase,
            h("h3", { class: "small muted" }, "Best private classic leagues"), outputs.leagues,
            h("h3", { class: "small muted" }, "Most valuable teams"), outputs.valuable,
            h("h3", { class: "small muted" }, "Regions"), outputs.regions);
        phase.addEventListener("change", () => load(outputs.phase, async () => listOrJson((await api.get(`/api/fpl/phases/${phase.value}/winners`)).data)));
        await Promise.all([
            load(outputs.winners, async () => listOrJson((await api.get(`/api/fpl/gameweeks/${state.gameweek}/winners`)).data)),
            load(outputs.phase, async () => listOrJson((await api.get(`/api/fpl/phases/${phase.value}/winners`)).data)),
            load(outputs.leagues, async () => listOrJson(await api.get("/api/fpl/rankings/best-private-leagues"))),
            load(outputs.valuable, async () => listOrJson(await api.get("/api/fpl/rankings/most-valuable-teams"))),
            load(outputs.regions, async () => {
                const regions = await data.regions();
                return h("p", { class: "small secondary" }, `${regions.count} regions: ${(regions.results || []).slice(0, 12).map((region) => region.name).join(", ")}${regions.count > 12 ? "…" : ""}`);
            }),
        ]);
    },
};

/* ---------- App health ---------- */
views.health = {
    logFilters: { level: "INFO", q: "", logger: "", auto: false },

    async render(main, context) {
        main.append(sectionTitle("App health", "Requests, the official FPL upstream, caches, logs, the archive and configuration"));
        const tilesHolder = h("div");
        main.append(tilesHolder);
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const routes = ui.card("Routes", "Per route since this instance started (latencies over the last 256 requests)");
        const errors = ui.card("Recent failed requests", null, { span: 6 });
        const upstream = ui.card("Official FPL upstream", "Latency by endpoint family and the response cache", { span: 6 });
        const logs = ui.card("Logs", "/api/admin/logs — the last 1,000 records on this instance");
        const timeline = ui.card("Decision log", "/api/admin/timeline", { span: 6 });
        const caches = ui.card("In-memory state", "Live forecasts, model inputs and process", { span: 6 });
        const archive = ui.card("Season archive", "/api/admin/archive");
        const healthJson = ui.card("Health endpoint", "GET /api/health", { span: 6 });
        const config = ui.card("Configuration", "/api/admin/config — no secrets, key count only", { span: 6 });
        [routes, errors, upstream, logs, timeline, caches, archive, healthJson, config].forEach((card) => grid.append(card.root));

        const metrics = await ui.fill(routes, () => api.get("/api/admin/metrics"));
        if (!metrics || !context.alive()) return;
        const requests = metrics.requests || {};
        const up = metrics.upstream || {};
        const process = metrics.process || {};
        tilesHolder.append(ui.tiles([
            { label: "Requests", value: utils.fmtCompact(requests.total_requests), detail: `since ${utils.dateTime(requests.since)}` },
            { label: "Error rate", value: utils.fmtPct(requests.error_rate, 1), detail: `${utils.fmtInt(requests.server_errors)} 5xx · ${utils.fmtInt(requests.client_errors)} 4xx` },
            { label: "Latency p50 · p95", value: `${utils.fmtMs(requests.latency_ms && requests.latency_ms.p50)}`, detail: `p95 ${utils.fmtMs(requests.latency_ms && requests.latency_ms.p95)}` },
            { label: "Upstream calls", value: utils.fmtCompact(up.requests), detail: `${utils.fmtInt(up.failures)} failed · ${utils.fmtInt(up.not_found)} not found` },
            { label: "Cache hit rate", value: utils.fmtPct(up.cache_hit_rate), detail: `${utils.fmtInt((metrics.official_cache || {}).live)} live entries` },
            { label: "Last upstream error", value: up.last_error_at ? utils.timeAgo(up.last_error_at) : "none", detail: up.last_error || "" },
            { label: "Memory", value: utils.fmtBytes(process.rss_bytes), detail: `peak ${utils.fmtBytes(process.peak_rss_bytes)}` },
            { label: "CPU time", value: `${utils.fmt(process.cpu_seconds, 1)} s`, detail: `load ${(process.load_average || []).join(" / ") || "–"} · ${process.cpu_count} CPUs` },
        ]));
        const routeRows = Object.entries(requests.routes || {}).map(([route, stats]) => ({ route, ...stats, p50: stats.latency_ms.p50, p95: stats.latency_ms.p95, max: stats.latency_ms.max }));
        clear(routes.body).append(ui.table(routeRows, [
            { key: "route", label: "Route", render: (row) => h("span", { class: "mono small" }, row.route) },
            { key: "count", label: "Calls", num: true },
            { key: "errors", label: "Errors", num: true },
            { key: "error_rate", label: "Err %", num: true, render: (row) => utils.fmtPct(row.error_rate, 1) },
            { key: "p50", label: "p50", num: true, render: (row) => utils.fmtMs(row.p50) },
            { key: "p95", label: "p95", num: true, render: (row) => utils.fmtMs(row.p95) },
            { key: "max", label: "Max", num: true, render: (row) => utils.fmtMs(row.max) },
            { key: "last_status", label: "Last", num: true },
            { key: "last_at", label: "When", render: (row) => utils.timeAgo(row.last_at) },
        ], { sortKey: "count", pageSize: 30 }));
        clear(errors.body).append(ui.table(requests.recent_errors || [], [
            { key: "time", label: "When", render: (row) => utils.timeAgo(row.time) },
            { key: "method", label: "Method" },
            { key: "path", label: "Path", render: (row) => h("span", { class: "mono small" }, row.path) },
            { key: "status", label: "Status", num: true },
            { key: "duration_ms", label: "Time", num: true, render: (row) => utils.fmtMs(row.duration_ms) },
        ], { footer: false, empty: "No failed requests." }));
        clear(upstream.body);
        const families = Object.entries(up.latency_by_endpoint || {}).map(([family, stats]) => ({ family, ...stats }));
        upstream.body.append(ui.table(families, [
            { key: "family", label: "Endpoint", render: (row) => h("span", { class: "mono small" }, row.family) },
            { key: "samples", label: "Calls", num: true },
            { key: "avg_ms", label: "Avg", num: true, render: (row) => utils.fmtMs(row.avg_ms) },
            { key: "p95_ms", label: "p95", num: true, render: (row) => utils.fmtMs(row.p95_ms) },
            { key: "max_ms", label: "Max", num: true, render: (row) => utils.fmtMs(row.max_ms) },
        ], { footer: false, sortKey: "samples", empty: "No upstream calls yet." }));
        const cacheFamilies = Object.entries((metrics.official_cache || {}).families || {}).map(([family, stats]) => ({ family, ...stats }));
        upstream.body.append(h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Response cache"));
        upstream.body.append(ui.table(cacheFamilies, [
            { key: "family", label: "Endpoint", render: (row) => h("span", { class: "mono small" }, row.family) },
            { key: "entries", label: "Entries", num: true },
            { key: "live", label: "Live", num: true },
            { key: "max_ttl_left_seconds", label: "Fresh for", num: true, render: (row) => utils.fmtDuration(row.max_ttl_left_seconds) },
        ], { footer: false, empty: "Cache empty." }));
        upstream.body.append(h("p", { class: "small muted" }, `${up.base_url} · timeout ${up.timeout_seconds}s · cache ${up.cache_ttl_seconds}s (history ${up.history_cache_ttl_seconds}s)`));

        add(clear(caches.body),
            h("h3", { class: "small muted", style: { margin: "0 0 4px" } }, "Live forecasts"),
            ui.table(metrics.live_forecasts || [], [
                { key: "gameweek", label: "GW", num: true },
                { key: "fresh", label: "State", render: (row) => row.pending ? "predicting" : row.fresh ? "fresh" : "stale" },
                { key: "age_seconds", label: "Age", num: true, render: (row) => utils.fmtDuration(row.age_seconds) },
                { key: "players", label: "Players", num: true },
            ], { footer: false, empty: "None cached." }),
            h("h3", { class: "small muted", style: { margin: "12px 0 4px" } }, "Model inputs kept for explanations"),
            ui.table(metrics.model_inputs || [], [
                { key: "gameweek", label: "GW", num: true },
                { key: "origin", label: "Origin" },
                { key: "approximate", label: "Rebuilt", render: (row) => (row.approximate ? "yes" : "no") },
                { key: "players", label: "Players", num: true },
                { key: "built_at", label: "Built", render: (row) => utils.timeAgo(row.built_at) },
            ], { footer: false, empty: "None yet." }),
            h("p", { class: "small muted" }, `Fixture cache: ${metrics.fixture_cache_entries} gameweek(s) · pid ${process.pid} · ${process.platform}`));

        views.health.logs(logs, context);
        ui.fill(timeline, async (target) => {
            const response = await api.get("/api/admin/timeline", { limit: 60 });
            clear(target).append(h("div", { style: { maxHeight: "520px", overflowY: "auto" } }, ui.timeline(response.events)));
        });
        ui.fill(archive, async (target) => {
            const inventory = await api.get("/api/admin/archive", undefined, { timeoutMs: CONFIG.slowTimeoutMs });
            clear(target);
            target.append(ui.kv([
                ["Enabled", inventory.enabled ? "yes" : "no"],
                ["Root", inventory.root_path],
                ["Season", inventory.season || "–"],
                ["Files · size", `${utils.fmtInt(inventory.total_files)} · ${utils.fmtBytes(inventory.total_bytes)}`],
                ["Last write", inventory.last_result ? `${inventory.last_result.status}${inventory.last_result.error ? ` — ${inventory.last_result.error}` : ""}` : "–"],
            ]));
            const mark = (file) => (file ? statusBadge("good", utils.fmtBytes(file.bytes)) : h("span", { class: "muted" }, "–"));
            target.append(ui.table(inventory.gameweeks || [], [
                { key: "gameweek", label: "GW", num: true },
                { key: "captured_at", label: "Forecast captured", render: (row) => row.captured_at ? utils.dateTime(row.captured_at) : "–" },
                { key: "freeze_method", label: "Freeze", render: (row) => row.freeze_method || (row.captured_at ? "deadline" : "–") },
                { key: "has_model_predictions", label: "Per-model", render: (row) => (row.has_model_predictions ? "yes" : "no") },
                { key: "metadata", label: "Metadata", sort: false, render: (row) => mark(row.files.metadata) },
                { key: "predictions", label: "Predictions", sort: false, render: (row) => mark(row.files.predictions) },
                { key: "squad", label: "Squad", sort: false, render: (row) => mark(row.files.squad) },
                { key: "live", label: "Live", sort: false, render: (row) => mark(row.files.live) },
                { key: "enriched", label: "Inputs", sort: false, render: (row) => mark(row.files.enriched_history) },
            ], { sortKey: "gameweek", footer: false, empty: "Nothing archived yet." }));
        });
        ui.fill(healthJson, async (target) => {
            clear(target).append(ui.json(await api.get("/api/health")));
        });
        ui.fill(config, async (target) => {
            clear(target).append(ui.json(await api.get("/api/admin/config")));
        });
    },

    logs(card, context) {
        const filters = views.health.logFilters;
        const output = h("div", { class: "table-wrap tall" });
        const counts = h("div", { class: "chips" });
        let timer = null;
        const load = async () => {
            if (!context.alive()) {
                clearInterval(timer);
                return;
            }
            try {
                const response = await api.get("/api/admin/logs", { level: filters.level, q: filters.q, logger: filters.logger, limit: 300 }, { retries: 0 });
                add(clear(counts), Object.entries(response.counts || {}).map(([level, count]) => chip(`${level} ${utils.fmtInt(count)}`)));
                const rows = response.records || [];
                clear(output).append(rows.length ? h("table", null,
                    h("thead", null, h("tr", null, ["Time", "Level", "Logger", "Message"].map((label) => h("th", null, label)))),
                    h("tbody", null, rows.map((record) => h("tr", null,
                        h("td", { class: "nowrap small" }, utils.dateTime(record.time)),
                        h("td", null, statusBadge({ ERROR: "critical", CRITICAL: "critical", WARNING: "warning", INFO: "neutral" }[record.level] || "neutral", record.level)),
                        h("td", { class: "small muted" }, `${record.logger} · ${record.location}`),
                        h("td", { class: "mono small" }, record.message, record.exception ? h("pre", { class: "code small muted" }, record.exception) : null))))) : h("p", { class: "empty", style: { padding: "12px" } }, "No matching records."));
            } catch (error) {
                clear(output).append(ui.errorBox(error, load));
            }
        };
        const level = h("select", { "aria-label": "Level" }, ["DEBUG", "INFO", "WARNING", "ERROR"].map((value) => h("option", { value, selected: value === filters.level ? true : null }, value)));
        level.addEventListener("change", () => { filters.level = level.value; load(); });
        const search = h("input", { type: "search", placeholder: "Search messages", value: filters.q, "aria-label": "Search logs" });
        search.addEventListener("input", utils.debounce(() => { filters.q = search.value; load(); }, 300));
        const loggerFilter = h("input", { type: "search", placeholder: "Logger (e.g. src.scout)", value: filters.logger, "aria-label": "Logger" });
        loggerFilter.addEventListener("input", utils.debounce(() => { filters.logger = loggerFilter.value; load(); }, 300));
        const auto = h("input", { type: "checkbox", checked: filters.auto ? true : null });
        const schedule = () => {
            clearInterval(timer);
            if (filters.auto) timer = setInterval(load, CONFIG.logPollMs);
        };
        auto.addEventListener("change", () => { filters.auto = auto.checked; schedule(); });
        clear(card.body).append(h("div", { class: "filters" }, level, search, loggerFilter, h("label", { class: "check small" }, auto, "Auto-refresh every 10 s"), h("button", { class: "btn small", type: "button", onClick: load }, "Reload"), counts), output);
        schedule();
        load();
    },
};

/* ---------- API explorer ---------- */
const ADMIN_ENDPOINTS = [
    { method: "GET", path: "/api/admin/overview", summary: "Command-centre overview" },
    { method: "GET", path: "/api/admin/models", summary: "Model health and agreement", query: ["gameweek"] },
    { method: "GET", path: "/api/admin/predictions", summary: "Every player's projection with inputs", query: ["gameweek"] },
    { method: "GET", path: "/api/admin/players/{player_id}/explain", summary: "Explain one projection", query: ["gameweek"] },
    { method: "GET", path: "/api/admin/squad", summary: "AI squad with the reason for every pick", query: ["gameweek"] },
    { method: "GET", path: "/api/admin/accuracy", summary: "Archived forecasts against actual points", query: ["played_only"] },
    { method: "GET", path: "/api/admin/timeline", summary: "Decision log", query: ["limit"] },
    { method: "GET", path: "/api/admin/logs", summary: "Recent log records", query: ["level", "q", "logger", "limit"] },
    { method: "GET", path: "/api/admin/metrics", summary: "Request, upstream and cache metrics" },
    { method: "GET", path: "/api/admin/archive", summary: "Season archive inventory" },
    { method: "GET", path: "/api/admin/config", summary: "Effective configuration" },
    { method: "POST", path: "/api/admin/predictions/recompute", summary: "Live: predict again now", query: ["gameweek"] },
    { method: "POST", path: "/api/admin/archive/collect", summary: "Live: collect results", query: ["force"] },
    { method: "POST", path: "/api/admin/cache/clear", summary: "Live: clear caches", body: { official: true, fixtures: true, forecasts: true } },
    { method: "POST", path: "/api/admin/models/{name}", summary: "Live: model weight / on-off", body: { weight: 1.0, enabled: true } },
    { method: "POST", path: "/api/admin/models/reset", summary: "Live: reset model overrides" },
    { method: "POST", path: "/api/admin/enrichment", summary: "Live: FPL Data enrichment on/off", body: { enabled: true } },
    { method: "POST", path: "/api/admin/simulate", summary: "Sandbox simulation", body: { gameweek: null, weights: {}, exclude_ids: [], lock_ids: [], max_per_club: 3, availability_overrides: {} } },
    { method: "GET", path: "/api/admin/managers/{entry_id}/plan-context", summary: "Free transfers, selling prices and budget (estimates)", query: ["gameweek"] },
    { method: "POST", path: "/api/admin/optimize", summary: "Plan transfers, a Wildcard or a Free Hit", body: { entry_id: 1, gameweek: null, mode: "transfers", horizon: 3, free_transfers: null, bank: null, max_transfers: null, draft: null, overrides: {} } },
    { method: "POST", path: "/api/admin/managers/{entry_id}/team", summary: "Working squad: rating, checks, lineup and captaincy advice", body: { gameweek: null, draft: null, overrides: {} } },
    { method: "POST", path: "/api/admin/managers/{entry_id}/outlook", summary: "Fixture ticker, projections and chip values", body: { gameweek: null, horizon: 5, draft: null, overrides: {} } },
    { method: "GET", path: "/api/admin/managers/{entry_id}/review", summary: "A gameweek's captain, bench and transfers against hindsight", query: ["gameweek"] },
];

views.explorer = {
    selected: null,

    async render(main, context) {
        main.append(sectionTitle("API explorer", "Call any OpenFPL endpoint with your token and inspect the response"));
        const grid = h("div", { class: "grid" });
        main.append(grid);
        const list = ui.card("Endpoints", "From GET /api and /openapi.json, plus the private admin routes", { span: 4 });
        const runner = ui.card("Request", null, { span: 8 });
        [list, runner].forEach((card) => grid.append(card.root));
        const catalog = await ui.fill(list, async () => {
            const [apiCatalog, openapi] = await Promise.all([
                cache.get("catalog", () => api.get("/api", undefined, { useAuth: false }), 600000),
                cache.get("openapi", () => api.get("/openapi.json", undefined, { useAuth: false }), 600000).catch(() => null),
            ]);
            return { apiCatalog, openapi };
        });
        if (!catalog || !context.alive()) return;
        const endpoints = [];
        (catalog.apiCatalog.tags || []).forEach((tag) => tag.endpoints.forEach((endpoint) => endpoint.methods.forEach((method) => {
            const operation = catalog.openapi && catalog.openapi.paths && catalog.openapi.paths[endpoint.path] && catalog.openapi.paths[endpoint.path][method.toLowerCase()];
            endpoints.push({
                group: tag.name, method, path: endpoint.path, summary: endpoint.summary, authentication: endpoint.authentication,
                query: ((operation && operation.parameters) || []).filter((parameter) => parameter.in === "query").map((parameter) => parameter.name),
                parameters: (operation && operation.parameters) || [],
            });
        })));
        ADMIN_ENDPOINTS.forEach((endpoint) => endpoints.push({ group: "Admin (private)", authentication: "bearer", query: [], parameters: [], ...endpoint }));
        const search = h("input", { type: "search", placeholder: "Filter endpoints", "aria-label": "Filter endpoints", style: { width: "100%", marginBottom: "8px" } });
        const listHolder = h("div", { class: "stack", style: { maxHeight: "70vh", overflowY: "auto", gap: "2px" } });
        const renderList = () => {
            const needle = search.value.trim().toLowerCase();
            clear(listHolder);
            let group = null;
            endpoints.filter((endpoint) => !needle || `${endpoint.path} ${endpoint.summary} ${endpoint.group}`.toLowerCase().includes(needle)).forEach((endpoint) => {
                if (endpoint.group !== group) {
                    group = endpoint.group;
                    listHolder.append(h("h3", { class: "small muted", style: { margin: "10px 0 2px" } }, group));
                }
                listHolder.append(h("button", {
                    class: "btn ghost small endpoint", type: "button",
                    onClick: () => views.explorer.form(runner.body, endpoint),
                }, h("span", { class: "mono" }, `${endpoint.method} ${endpoint.path}`), h("span", { class: "sub" }, `${endpoint.summary} · ${endpoint.authentication}`)));
            });
        };
        search.addEventListener("input", renderList);
        clear(list.body).append(search, listHolder);
        renderList();
        clear(runner.body).append(h("p", { class: "empty" }, `${endpoints.length} endpoints. Pick one to build a request.`));
    },

    form(target, endpoint) {
        clear(target);
        const pathParams = [...endpoint.path.matchAll(/\{(\w+)\}/g)].map((match) => match[1]);
        const fields = {};
        const defaults = { gameweek: state.gameweek, entry_id: storage.get("localStorage", CONFIG.storageKeys.entry) || "" };
        const describe = (name) => endpoint.parameters.find((parameter) => parameter.name === name) || {};
        const field = (name, location) => {
            const parameter = describe(name);
            const schema = parameter.schema || {};
            const input = h("input", { type: "text", value: defaults[name] ?? (schema.default ?? ""), placeholder: [schema.type || (schema.anyOf || []).map((item) => item.type).join("|"), parameter.required ? "required" : "optional"].filter(Boolean).join(" · "), "aria-label": name });
            fields[`${location}:${name}`] = input;
            return h("label", { class: "stack", style: { gap: "2px" } }, h("span", { class: "small muted" }, `${name} (${location})`), input);
        };
        const body = endpoint.method === "POST" && endpoint.body ? h("textarea", { rows: 8, class: "mono", style: { width: "100%" }, "aria-label": "Request body" }) : null;
        if (body) body.value = JSON.stringify({ ...endpoint.body, ...(endpoint.body.gameweek === null ? { gameweek: state.gameweek } : {}) }, null, 2);
        const output = h("div", { class: "stack", style: { marginTop: "12px" } });
        const build = () => {
            let path = endpoint.path;
            for (const name of pathParams) {
                const value = fields[`path:${name}`].value.trim();
                if (!value) throw new Error(`${name} is required`);
                path = path.replace(`{${name}}`, encodeURIComponent(value));
            }
            const query = {};
            endpoint.query.forEach((name) => {
                const value = fields[`query:${name}`].value.trim();
                if (value !== "") query[name] = value;
            });
            return { path, query };
        };
        const send = async () => {
            let request;
            let payload;
            try {
                request = build();
                payload = body && body.value.trim() ? JSON.parse(body.value) : undefined;
            } catch (error) {
                toast.show(error.message, "error");
                return;
            }
            clear(output).append(h("span", { class: "muted" }, "Sending…"));
            const started = api.now();
            try {
                const response = await api.request(request.path, { method: endpoint.method, query: request.query, body: payload, timeoutMs: CONFIG.slowTimeoutMs, retries: 0 });
                clear(output).append(h("div", { class: "row" }, statusBadge("good", `${response.status}`), chip(utils.fmtMs(response.ms)), chip(utils.fmtBytes(response.bytes))), ui.json(response.data));
            } catch (error) {
                add(clear(output), h("div", { class: "row" }, statusBadge("critical", `${error.status || "error"}`), chip(utils.fmtMs(api.now() - started))), h("p", { class: "error-text" }, error.message), error.payload ? ui.json(error.payload) : null);
            }
        };
        const curl = () => {
            try {
                const request = build();
                const url = `${window.location.origin}${request.path}${utils.qs(request.query)}`;
                const parts = [`curl -X ${endpoint.method} "${url}"`];
                if (endpoint.authentication === "bearer") parts.push('-H "Authorization: Bearer $OPENFPL_TOKEN"');
                if (body && body.value.trim()) parts.push(`-H "Content-Type: application/json" -d '${JSON.stringify(JSON.parse(body.value))}'`);
                const text = parts.join(" \\\n  ");
                if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast.show("curl copied (token as $OPENFPL_TOKEN)", "success"));
                else dialogs.modal("curl", h("pre", { class: "code" }, text));
            } catch (error) {
                toast.show(error.message, "error");
            }
        };
        add(target,
            h("div", { class: "row" }, chip(endpoint.method, endpoint.method === "POST" ? "lime" : "aqua"), h("span", { class: "mono" }, endpoint.path), chip(endpoint.authentication)),
            h("p", { class: "small muted" }, endpoint.summary),
            h("div", { class: "split" }, pathParams.map((name) => field(name, "path")), endpoint.query.map((name) => field(name, "query"))),
            body ? h("div", { style: { marginTop: "10px" } }, h("span", { class: "small muted" }, "JSON body"), body) : null,
            endpoint.method === "POST" && endpoint.path.startsWith("/api/admin") && !endpoint.path.includes("simulate") ? h("p", { class: "notes" }, "This is a live control: it changes the running service.") : null,
            h("div", { class: "btn-row", style: { marginTop: "10px" } },
                h("button", { class: "btn primary", type: "button", onClick: send }, "Send"),
                h("button", { class: "btn ghost", type: "button", onClick: curl }, "Copy as curl")),
            output);
    },
};

/* ------------------------------------------------------------------ */
/* Bootstrap                                                           */
/* ------------------------------------------------------------------ */

const OpenFPLAdmin = { CONFIG, ApiError, storage, auth, utils, api, charts, ui, state, cache, data, app, views, optimizerView, drawer, draft, overrides, alerts, h, s, clear };
if (typeof window !== "undefined") window.OpenFPLAdmin = OpenFPLAdmin;
if (typeof document !== "undefined" && typeof document.addEventListener === "function" && document.getElementById && document.getElementById("app")) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => app.start());
    else app.start();
}

/* Render the report's charts without a browser.
 *
 * static/report/charts.js builds every figure through a small set of DOM calls.
 * This script supplies just enough of those calls to run it under Node, then
 * serialises the resulting SVG and table rows to JSON on stdout so the PDF
 * build can inline them. Keeping charts.js as the only chart implementation
 * means the PDF and the web page can never drift apart.
 *
 *   node scripts/report_prerender.js [static/report] > figures.json
 */

"use strict";

const fs = require("fs");
const path = require("path");

const reportDir = process.argv[2] || path.join(__dirname, "..", "static", "report");

/* ---------- minimal DOM ---------- */

function makeNode(tagName) {
    return {
        tagName,
        children: [],
        attrs: {},
        style: {},
        _text: "",
        setAttribute(name, value) { this.attrs[name] = String(value); },
        getAttribute(name) { return this.attrs[name]; },
        appendChild(child) { this.children.push(child); child.parent = this; return child; },
        insertBefore(child) { this.children.unshift(child); return child; },
        addEventListener() { },
        classList: { add() { }, remove() { } },
        get parentNode() { return this.parent; },
        set textContent(value) { this._text = String(value); },
        get textContent() { return this._text; },
        set innerHTML(value) { this._html = value; },
        get innerHTML() { return this._html || ""; },
        set className(value) { this.attrs.class = value; },
        get className() { return this.attrs.class; }
    };
}

const FIGURES = [
    "fig-holdout", "fig-cv", "fig-calib", "fig-weekly",
    "fig-backtest-bar", "fig-backtest-line", "fig-importance", "fig-position"
];
const TABLES = ["tbl-holdout", "tbl-cv", "tbl-backtest", "tbl-position"];

const mounts = {};
FIGURES.forEach(id => {
    const mount = makeNode("div");
    mount.parent = makeNode("figure");
    mounts[id] = mount;
});
TABLES.forEach(id => { mounts[id] = makeNode("tbody"); });

global.document = {
    createElementNS: (ns, tag) => makeNode(tag),
    createElement: tag => makeNode(tag),
    createTextNode: value => ({ tagName: "#text", _text: value, children: [], attrs: {} }),
    getElementById: id => mounts[id] || null,
    body: makeNode("body"),
    readyState: "complete",
    addEventListener() { }
};
global.window = { innerWidth: 1200, innerHeight: 900 };

/* ---------- run the real chart code ---------- */

const load = name => fs.readFileSync(path.join(reportDir, name), "utf8");
eval(load("data.js").replace("window.REPORT", "global.window.REPORT"));
eval(load("charts.js"));

/* ---------- serialise ---------- */

const VOID_ATTRS_ONLY = new Set();
const escapeText = value => String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function serialise(node) {
    if (node.tagName === "#text") return escapeText(node._text);
    const attrs = Object.entries(node.attrs)
        .map(([name, value]) => ` ${name}="${escapeText(value).replace(/"/g, "&quot;")}"`)
        .join("");
    const inner = node.children.map(serialise).join("") +
        (node._text ? escapeText(node._text) : "");
    if (VOID_ATTRS_ONLY.has(node.tagName)) return `<${node.tagName}${attrs}/>`;
    return `<${node.tagName}${attrs}>${inner}</${node.tagName}>`;
}

/* The legend is a sibling <div> that charts.js inserts before the mount. */
function serialiseLegend(mount) {
    const legend = mount.parent.children.find(
        child => child.tagName === "div" && child.attrs.class === "legend"
    );
    if (!legend) return "";
    const items = legend.children.map(entry => {
        const label = entry.children
            .filter(child => child.tagName === "#text")
            .map(child => child._text).join("");
        const swatch = entry.children.find(child => child.tagName === "i");
        const dashed = swatch.attrs.class === "dash";
        const colour = swatch.style.background || swatch.style.borderTopColor || "#888";
        const mark = dashed
            ? `<i class="dash" style="border-top-color:${colour}"></i>`
            : `<i style="background:${colour}"></i>`;
        return `<span>${mark}${escapeText(label)}</span>`;
    }).join("");
    return `<div class="legend">${items}</div>`;
}

const output = { figures: {}, legends: {}, tables: {} };

FIGURES.forEach(id => {
    const mount = mounts[id];
    const svg = mount.children.find(child => child.tagName === "svg");
    if (!svg) throw new Error(`Figure ${id} produced no SVG`);
    /* The browser gets its dimensions from CSS; a standalone SVG needs them. */
    const [, , width, height] = svg.attrs.viewBox.split(" ").map(Number);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    output.figures[id] = serialise(svg);
    output.legends[id] = serialiseLegend(mount);
});

TABLES.forEach(id => {
    output.tables[id] = mounts[id].children.map(row => {
        const html = row.innerHTML;
        if (/NaN|undefined/.test(html)) throw new Error(`Table ${id} has an invalid cell: ${html}`);
        const cls = row.attrs.class ? ` class="${row.attrs.class}"` : "";
        return `<tr${cls}>${html}</tr>`;
    }).join("");
    if (!output.tables[id]) throw new Error(`Table ${id} produced no rows`);
});

process.stdout.write(JSON.stringify(output));

/* OpenFPL Scout AI — report charts.
   Hand-built inline SVG so the page stays self-contained (no CDN, no runtime
   dependency). Every figure is driven by static/report/data.js, which is
   generated from the artifacts in models/. */

(function () {
    "use strict";

    var D = window.REPORT;
    if (!D) return;

    var NS = "http://www.w3.org/2000/svg";
    var PAL = ["#729e00", "#00a8b3", "#9a6500", "#743bfe", "#ca027b"];
    var REF = "#6f6478";

    /* ---------- tiny SVG helpers ---------- */

    function el(name, attrs, parent) {
        var node = document.createElementNS(NS, name);
        for (var key in attrs) {
            if (attrs[key] !== null && attrs[key] !== undefined) {
                node.setAttribute(key, attrs[key]);
            }
        }
        if (parent) parent.appendChild(node);
        return node;
    }

    function text(parent, x, y, value, cls, anchor) {
        var node = el("text", { x: x, y: y, class: cls || "tick" }, parent);
        if (anchor) node.setAttribute("text-anchor", anchor);
        node.textContent = value;
        return node;
    }

    function svg(mount, width, height) {
        var node = el("svg", {
            viewBox: "0 0 " + width + " " + height,
            role: "img",
            preserveAspectRatio: "xMidYMid meet"
        });
        mount.appendChild(node);
        return node;
    }

    function fmt(value, digits) {
        return Number(value).toFixed(digits === undefined ? 2 : digits);
    }

    /* Training artifacts label a season by its END year (fpl-data-stats-2026.csv
       is the 2025/26 season). Render the human-readable label instead. */
    function seasonLabel(period) {
        return String(period).replace(/(20\d{2})-GW/, function (all, year) {
            return (Number(year) - 1) + "/" + String(year).slice(2) + " GW";
        });
    }

    /* Horizontal bar squared off at the baseline and rounded at the data end.
       Drawn as one path so the two ends never overlap and double-composite. */
    function bar(parent, x, y, w, h, color, opacity) {
        var r = Math.min(4, Math.max(0, w), h / 2);
        var d = "M" + x + " " + y +
            "H" + (x + w - r) +
            "a" + r + " " + r + " 0 0 1 " + r + " " + r +
            "V" + (y + h - r) +
            "a" + r + " " + r + " 0 0 1 " + (-r) + " " + r +
            "H" + x + "Z";
        return el("path", {
            d: d, fill: color, "fill-opacity": opacity === undefined ? 1 : opacity
        }, parent);
    }

    /* ---------- shared tooltip ---------- */

    var tip = document.createElement("div");
    tip.className = "tip";
    tip.setAttribute("role", "status");
    document.body.appendChild(tip);

    function showTip(event, html) {
        tip.innerHTML = html;
        tip.classList.add("on");
        var box = tip.getBoundingClientRect();
        var x = event.clientX + 16;
        var y = event.clientY - box.height - 12;
        if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 16;
        if (y < 8) y = event.clientY + 18;
        tip.style.left = Math.max(8, x) + "px";
        tip.style.top = y + "px";
    }

    function hideTip() {
        tip.classList.remove("on");
    }

    function bindTip(node, html) {
        node.addEventListener("mousemove", function (event) { showTip(event, html); });
        node.addEventListener("mouseleave", hideTip);
        node.addEventListener("touchstart", function (event) {
            showTip(event.touches[0], html);
        }, { passive: true });
    }

    function tipRow(color, label, value) {
        return '<div class="row"><i style="background:' + color + '"></i>' +
            label + " &nbsp;<b style=\"display:inline;margin:0\">" + value + "</b></div>";
    }

    /* ---------- axis helpers ---------- */

    function niceTicks(min, max, count) {
        var span = max - min;
        if (span <= 0) return [min];
        var raw = span / (count || 5);
        var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
        var step = mag;
        [1, 2, 2.5, 5, 10].some(function (m) {
            if (mag * m >= raw) { step = mag * m; return true; }
            return false;
        });
        var start = Math.ceil(min / step) * step;
        var ticks = [];
        for (var v = start; v <= max + step * 0.001; v += step) {
            ticks.push(Math.round(v / step) * step);
        }
        return ticks;
    }

    function legend(mount, items) {
        var box = document.createElement("div");
        box.className = "legend";
        items.forEach(function (item) {
            var span = document.createElement("span");
            var mark = document.createElement("i");
            if (item.dash) {
                mark.className = "dash";
                mark.style.borderTopColor = item.color;
            } else {
                mark.style.background = item.color;
            }
            span.appendChild(mark);
            span.appendChild(document.createTextNode(item.label));
            box.appendChild(span);
        });
        mount.parentNode.insertBefore(box, mount);
    }

    /* ================= Figure 1 — holdout accuracy ranking ================= */

    function holdoutChart() {
        var mount = document.getElementById("fig-holdout");
        if (!mount) return;

        var rows = D.holdout.slice().sort(function (a, b) { return a.rmse - b.rmse; });
        var rowH = 30, padT = 34, padB = 44, padL = 176, padR = 58;
        var W = 780, H = padT + rows.length * rowH + padB;
        var plotW = W - padL - padR;
        var max = 2.7;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Holdout root mean squared error by model and baseline");

        var ticks = [0, 0.5, 1, 1.5, 2, 2.5];
        ticks.forEach(function (t) {
            var x = padL + (t / max) * plotW;
            el("line", { x1: x, y1: padT - 10, x2: x, y2: padT + rows.length * rowH, class: "grid-line" }, s);
            text(s, x, H - padB + 20, fmt(t, 1), "tick", "middle");
        });
        text(s, padL + plotW / 2, H - padB + 40, "RMSE — lower is better", "axis-title", "middle");

        rows.forEach(function (row, i) {
            var y = padT + i * rowH;
            var isEnsemble = row.key.indexOf("ensemble") > -1;
            var isBaseline = row.key.indexOf("baseline") > -1;
            var w = (row.rmse / max) * plotW;
            var color = isBaseline ? REF : PAL[0];

            text(s, padL - 12, y + 15, row.label, isEnsemble ? "tick-strong" : "tick", "end");

            bar(s, padL, y + 4, Math.max(2, w), rowH - 10, color,
                isEnsemble ? 1 : (isBaseline ? 0.55 : 0.8));

            text(s, padL + w + 9, y + 15, fmt(row.rmse, 3), "val-label");

            var hit = el("rect", { x: padL, y: y, width: plotW, height: rowH, class: "hit" }, s);
            bindTip(hit, "<b>" + row.label + "</b>" +
                tipRow(color, "RMSE", fmt(row.rmse, 3)) +
                tipRow(color, "MAE", fmt(row.mae, 3)) +
                tipRow(color, "R²", fmt(row.r2, 3)));
        });
    }

    /* ================= Figure 2 — cross-validation folds ================= */

    function cvChart() {
        var mount = document.getElementById("fig-cv");
        if (!mount) return;

        var order = ["catboost", "xgboost", "mlp", "linear_regression"];
        var series = order.map(function (key, i) {
            var found = D.cv_folds.filter(function (m) { return m.key === key; })[0];
            return { key: key, label: found.model, color: PAL[i], folds: found.folds };
        });

        legend(mount, series.map(function (item) {
            return { color: item.color, label: item.label };
        }));

        var padT = 26, padB = 76, padL = 56, padR = 26;
        var W = 780, H = 340;
        var plotW = W - padL - padR, plotH = H - padT - padB;
        var min = 1.85, max = 2.16;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Validation RMSE per cross-validation fold for each model");

        var yOf = function (v) { return padT + plotH - ((v - min) / (max - min)) * plotH; };
        /* Inset the first and last fold so their axis labels stay inside the viewBox. */
        var inset = 54;
        var step = (plotW - inset * 2) / 4;
        var xOf = function (i) { return padL + inset + step * i; };

        [1.9, 1.95, 2.0, 2.05, 2.1, 2.15].forEach(function (t) {
            var y = yOf(t);
            el("line", { x1: padL, y1: y, x2: W - padR, y2: y, class: "grid-line" }, s);
            text(s, padL - 10, y + 4, fmt(t, 2), "tick", "end");
        });
        text(s, 14, padT + plotH / 2, "RMSE", "axis-title", "middle")
            .setAttribute("transform", "rotate(-90 14 " + (padT + plotH / 2) + ")");

        series[0].folds.forEach(function (fold, i) {
            var x = xOf(i);
            var span = fold.val.split("→");
            text(s, x, H - padB + 22, "Fold " + fold.fold, "tick-strong", "middle");
            text(s, x, H - padB + 40, seasonLabel(span[0]), "tick", "middle");
            text(s, x, H - padB + 54, "→ " + seasonLabel(span[1]), "tick", "middle");
        });
        el("line", { x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH, class: "axis-line" }, s);

        series.forEach(function (item) {
            var d = item.folds.map(function (fold, i) {
                return (i ? "L" : "M") + xOf(i) + " " + yOf(fold.rmse);
            }).join(" ");
            el("path", { d: d, fill: "none", stroke: item.color, "stroke-width": 2, "stroke-linejoin": "round" }, s);
            item.folds.forEach(function (fold, i) {
                /* 2px surface ring keeps overlapping markers separable */
                el("circle", { cx: xOf(i), cy: yOf(fold.rmse), r: 5, fill: item.color, stroke: "#1c0828", "stroke-width": 2 }, s);
            });
        });

        series[0].folds.forEach(function (fold, i) {
            var hx = Math.max(padL, xOf(i) - step / 2);
            var hw = Math.min(W - padR, xOf(i) + step / 2) - hx;
            var hit = el("rect", { x: hx, y: padT, width: hw, height: plotH, class: "hit" }, s);
            var html = "<b>Fold " + fold.fold + " · validates " +
                seasonLabel(fold.val.split("→")[0]) + " → " + seasonLabel(fold.val.split("→")[1]) + "</b>" +
                '<div class="row" style="color:#91849e">' + fold.train_rows.toLocaleString() +
                " train rows · " + fold.val_rows.toLocaleString() + " validation rows</div>";
            series.forEach(function (item) {
                html += tipRow(item.color, item.label, fmt(item.folds[i].rmse, 3));
            });
            bindTip(hit, html);
            el("line", { x1: xOf(i), y1: padT, x2: xOf(i), y2: padT + plotH, class: "grid-line" }, s);
        });
    }

    /* ================= Figure 3 — calibration ================= */

    function calibrationChart() {
        var mount = document.getElementById("fig-calib");
        if (!mount) return;

        legend(mount, [
            { color: PAL[0], label: "Holdout decile (predicted vs actual)" },
            { color: REF, label: "Perfect calibration", dash: true }
        ]);

        var padT = 22, padB = 56, padL = 56, padR = 28;
        var W = 720, H = 380;
        var plotW = W - padL - padR, plotH = H - padT - padB;
        /* The lowest decile's mean forecast is fractionally negative before the
           runtime clip at zero, so the domain starts below zero rather than
           pushing that point outside the plot. */
        var min = -0.3, max = 4.2;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Predicted versus actual mean points by holdout decile");

        var xOf = function (v) { return padL + ((v - min) / (max - min)) * plotW; };
        var yOf = function (v) { return padT + plotH - ((v - min) / (max - min)) * plotH; };

        [0, 1, 2, 3, 4].forEach(function (t) {
            el("line", { x1: xOf(t), y1: padT, x2: xOf(t), y2: padT + plotH, class: "grid-line" }, s);
            el("line", { x1: padL, y1: yOf(t), x2: W - padR, y2: yOf(t), class: "grid-line" }, s);
            text(s, xOf(t), H - padB + 20, fmt(t, 0), "tick", "middle");
            text(s, padL - 10, yOf(t) + 4, fmt(t, 0), "tick", "end");
        });

        el("line", {
            x1: xOf(min), y1: yOf(min), x2: xOf(max), y2: yOf(max),
            stroke: REF, "stroke-width": 2, "stroke-dasharray": "5 5"
        }, s);

        text(s, padL + plotW / 2, H - padB + 40, "Predicted points (decile mean)", "axis-title", "middle");
        text(s, 15, padT + plotH / 2, "Actual points", "axis-title", "middle")
            .setAttribute("transform", "rotate(-90 15 " + (padT + plotH / 2) + ")");

        var pts = D.calibration.map(function (row) {
            return { x: xOf(row.pred), y: yOf(row.actual), row: row };
        });
        el("path", {
            d: pts.map(function (p, i) { return (i ? "L" : "M") + p.x + " " + p.y; }).join(" "),
            fill: "none", stroke: PAL[0], "stroke-width": 2
        }, s);

        pts.forEach(function (p) {
            el("circle", { cx: p.x, cy: p.y, r: 6, fill: PAL[0], stroke: "#1c0828", "stroke-width": 2 }, s);
            var hit = el("circle", { cx: p.x, cy: p.y, r: 16, class: "hit" }, s);
            bindTip(hit, "<b>Decile " + p.row.decile + " of 10</b>" +
                tipRow(PAL[0], "Predicted", fmt(p.row.pred, 2)) +
                tipRow(PAL[0], "Actual", fmt(p.row.actual, 2)) +
                '<div class="row" style="color:#91849e">' + p.row.n.toLocaleString() + " player-gameweeks</div>");
        });

        text(s, xOf(pts[9].row.pred) - 12, yOf(pts[9].row.actual) - 14, "Top decile", "val-label", "end");
    }

    /* ================= Figure 4 — weekly ranking skill ================= */

    function weeklyChart() {
        var mount = document.getElementById("fig-weekly");
        if (!mount) return;

        var rows = D.per_gameweek;
        var padT = 24, padB = 56, padL = 52, padR = 26;
        var W = 820, H = 320;
        var plotW = W - padL - padR, plotH = H - padT - padB;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Spearman rank correlation between predicted and actual points, by gameweek");

        var xOf = function (i) { return padL + (plotW / (rows.length - 1)) * i; };
        var yOf = function (v) { return padT + plotH - (v / 0.8) * plotH; };

        [0, 0.2, 0.4, 0.6, 0.8].forEach(function (t) {
            el("line", { x1: padL, y1: yOf(t), x2: W - padR, y2: yOf(t), class: "grid-line" }, s);
            text(s, padL - 10, yOf(t) + 4, fmt(t, 1), "tick", "end");
        });
        text(s, 13, padT + plotH / 2, "Spearman ρ", "axis-title", "middle")
            .setAttribute("transform", "rotate(-90 13 " + (padT + plotH / 2) + ")");
        text(s, padL + plotW / 2, H - padB + 40, "Gameweek (2025/26 holdout season)", "axis-title", "middle");

        rows.forEach(function (row, i) {
            if (row.gw % 4 === 1 || row.gw === 38) {
                text(s, xOf(i), H - padB + 20, row.gw, "tick", "middle");
            }
        });
        el("line", { x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH, class: "axis-line" }, s);

        var mean = rows.reduce(function (a, r) { return a + r.spearman; }, 0) / rows.length;
        el("line", {
            x1: padL, y1: yOf(mean), x2: W - padR, y2: yOf(mean),
            stroke: REF, "stroke-width": 2, "stroke-dasharray": "5 5"
        }, s);
        /* The series hugs the mean line for the whole season, so the annotation
           goes in the empty band below it rather than on top of the data. */
        text(s, padL + plotW * 0.5, yOf(0.40), "Season mean ρ = " + fmt(mean, 2), "tick-strong", "middle");

        el("path", {
            d: rows.map(function (row, i) { return (i ? "L" : "M") + xOf(i) + " " + yOf(row.spearman); }).join(" "),
            fill: "none", stroke: PAL[0], "stroke-width": 2, "stroke-linejoin": "round"
        }, s);

        rows.forEach(function (row, i) {
            var isGw1 = row.gw === 1;
            el("circle", {
                cx: xOf(i), cy: yOf(row.spearman), r: isGw1 ? 6 : 3.5,
                fill: isGw1 ? "#c0392b" : PAL[0], stroke: "#1c0828", "stroke-width": 2
            }, s);
            var hit = el("rect", { x: xOf(i) - plotW / (rows.length * 2), y: padT, width: plotW / rows.length, height: plotH, class: "hit" }, s);
            bindTip(hit, "<b>Gameweek " + row.gw + "</b>" +
                tipRow(isGw1 ? "#c0392b" : PAL[0], "Spearman ρ", fmt(row.spearman, 3)) +
                tipRow(PAL[0], "RMSE", fmt(row.rmse, 3)) +
                '<div class="row" style="color:#91849e">' + row.n + " players ranked</div>");
        });

        text(s, xOf(0) + 12, yOf(rows[0].spearman) + 4, "GW1 cold start", "val-label");
    }

    /* ================= Figure 5 — squad backtest, season summary ================= */

    function backtestSummary() {
        var mount = document.getElementById("fig-backtest-bar");
        if (!mount) return;

        var b = D.backtest;
        var mean = function (key) {
            return b.reduce(function (a, r) { return a + r[key]; }, 0) / b.length;
        };
        var rows = [
            { label: "OpenFPL ensemble", value: mean("ai"), color: PAL[0] },
            { label: "Baseline: 5-gameweek form", value: mean("form5"), color: PAL[1] },
            { label: "Baseline: FPL's own xP", value: mean("fpl_xp"), color: PAL[2] },
            { label: "Baseline: last gameweek", value: mean("last"), color: REF, muted: true },
            { label: "Random legal squad", value: mean("random"), color: REF, muted: true },
            { label: "Perfect hindsight (ceiling)", value: mean("perfect"), color: REF, muted: true }
        ];

        var rowH = 34, padT = 12, padB = 44, padL = 210, padR = 62;
        var W = 780, H = padT + rows.length * rowH + padB;
        var plotW = W - padL - padR;
        var max = 180;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Mean actual points scored per gameweek by squad selection strategy");

        niceTicks(0, max, 5).forEach(function (t) {
            var x = padL + (t / max) * plotW;
            el("line", { x1: x, y1: padT, x2: x, y2: padT + rows.length * rowH, class: "grid-line" }, s);
            text(s, x, H - padB + 20, t, "tick", "middle");
        });
        text(s, padL + plotW / 2, H - padB + 38, "Mean actual points per gameweek (15 players)", "axis-title", "middle");

        rows.forEach(function (row, i) {
            var y = padT + i * rowH;
            var w = (row.value / max) * plotW;
            text(s, padL - 12, y + 20, row.label, row.muted ? "tick" : "tick-strong", "end");
            bar(s, padL, y + 6, Math.max(2, w), rowH - 13, row.color, row.muted ? 0.5 : 0.95);
            text(s, padL + w + 9, y + 20, fmt(row.value, 1), "val-label");

            var hit = el("rect", { x: padL, y: y, width: plotW, height: rowH, class: "hit" }, s);
            bindTip(hit, "<b>" + row.label + "</b>" +
                tipRow(row.color, "Points per gameweek", fmt(row.value, 1)) +
                tipRow(row.color, "Season total", Math.round(row.value * 38).toLocaleString()));
        });
    }

    /* ================= Figure 6 — squad backtest, week by week ================= */

    function backtestWeekly() {
        var mount = document.getElementById("fig-backtest-line");
        if (!mount) return;

        var b = D.backtest;
        var series = [
            { key: "ai", label: "OpenFPL ensemble", color: PAL[0] },
            { key: "fpl_xp", label: "Baseline: FPL's own xP", color: PAL[1] },
            { key: "form5", label: "Baseline: 5-gameweek form", color: PAL[2] }
        ];
        legend(mount, series.map(function (item) {
            return { color: item.color, label: item.label };
        }));

        var padT = 22, padB = 56, padL = 48, padR = 24;
        var W = 820, H = 330;
        var plotW = W - padL - padR, plotH = H - padT - padB;
        var max = 120;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Actual points scored per gameweek by each squad selection strategy");

        var xOf = function (i) { return padL + (plotW / (b.length - 1)) * i; };
        var yOf = function (v) { return padT + plotH - (v / max) * plotH; };

        niceTicks(0, max, 5).forEach(function (t) {
            el("line", { x1: padL, y1: yOf(t), x2: W - padR, y2: yOf(t), class: "grid-line" }, s);
            text(s, padL - 10, yOf(t) + 4, t, "tick", "end");
        });
        text(s, 13, padT + plotH / 2, "Points", "axis-title", "middle")
            .setAttribute("transform", "rotate(-90 13 " + (padT + plotH / 2) + ")");
        text(s, padL + plotW / 2, H - padB + 40, "Gameweek (2025/26 holdout season)", "axis-title", "middle");

        b.forEach(function (row, i) {
            if (row.gw % 4 === 1 || row.gw === 38) text(s, xOf(i), H - padB + 20, row.gw, "tick", "middle");
        });
        el("line", { x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH, class: "axis-line" }, s);

        series.forEach(function (item) {
            el("path", {
                d: b.map(function (row, i) { return (i ? "L" : "M") + xOf(i) + " " + yOf(row[item.key]); }).join(" "),
                fill: "none", stroke: item.color, "stroke-width": 2,
                "stroke-linejoin": "round", "stroke-opacity": item.key === "ai" ? 1 : 0.8
            }, s);
        });

        b.forEach(function (row, i) {
            var hit = el("rect", { x: xOf(i) - plotW / (b.length * 2), y: padT, width: plotW / b.length, height: plotH, class: "hit" }, s);
            var html = "<b>Gameweek " + row.gw + "</b>";
            series.forEach(function (item) { html += tipRow(item.color, item.label, Math.round(row[item.key])); });
            html += '<div class="row" style="color:#91849e">Ceiling ' + Math.round(row.perfect) + " · random " + fmt(row.random, 1) + "</div>";
            bindTip(hit, html);
            el("line", { x1: xOf(i), y1: padT, x2: xOf(i), y2: padT + plotH, class: "grid-line", "stroke-opacity": 0.35 }, s);
        });
    }

    /* ================= Figure 7 — feature importance ================= */

    function importanceChart() {
        var mount = document.getElementById("fig-importance");
        if (!mount) return;

        var rows = D.importance.slice(0, 12);
        legend(mount, [
            { color: PAL[0], label: "CatBoost" },
            { color: PAL[1], label: "XGBoost" }
        ]);

        var groupH = 34, padT = 10, padB = 44, padL = 168, padR = 50;
        var W = 780, H = padT + rows.length * groupH + padB;
        var plotW = W - padL - padR;
        var max = 28;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Share of tree-model split importance by input feature");

        [0, 5, 10, 15, 20, 25].forEach(function (t) {
            var x = padL + (t / max) * plotW;
            el("line", { x1: x, y1: padT, x2: x, y2: padT + rows.length * groupH, class: "grid-line" }, s);
            text(s, x, H - padB + 20, t + "%", "tick", "middle");
        });
        text(s, padL + plotW / 2, H - padB + 38, "Share of model split importance", "axis-title", "middle");

        rows.forEach(function (row, i) {
            var y = padT + i * groupH;
            text(s, padL - 12, y + 21, row.feature, "tick", "end");
            /* 2px surface gap between the two adjacent fills */
            [["catboost", PAL[0], 0], ["xgboost", PAL[1], 1]].forEach(function (pair) {
                var w = (row[pair[0]] / max) * plotW;
                bar(s, padL, y + 5 + pair[2] * 12, Math.max(1.5, w), 10, pair[1]);
            });
            var wider = Math.max(row.catboost, row.xgboost);
            text(s, padL + (wider / max) * plotW + 9, y + 21, fmt(wider, 1) + "%", "val-label");

            var hit = el("rect", { x: padL, y: y, width: plotW, height: groupH, class: "hit" }, s);
            bindTip(hit, "<b>" + row.feature + "</b>" +
                tipRow(PAL[0], "CatBoost", fmt(row.catboost, 2) + "%") +
                tipRow(PAL[1], "XGBoost", fmt(row.xgboost, 2) + "%"));
        });
    }

    /* ================= Figure 8 — error by position ================= */

    function positionChart() {
        var mount = document.getElementById("fig-position");
        if (!mount) return;

        var rows = D.by_position;
        legend(mount, [
            { color: PAL[0], label: "Mean predicted points" },
            { color: PAL[1], label: "Mean actual points" }
        ]);

        var groupH = 46, padT = 12, padB = 46, padL = 116, padR = 116;
        var W = 760, H = padT + rows.length * groupH + padB;
        var plotW = W - padL - padR;
        var max = 1.5;
        var s = svg(mount, W, H);
        s.setAttribute("aria-label", "Mean predicted versus actual points by position, with RMSE");

        niceTicks(0, max, 5).forEach(function (t) {
            var x = padL + (t / max) * plotW;
            el("line", { x1: x, y1: padT, x2: x, y2: padT + rows.length * groupH, class: "grid-line" }, s);
            text(s, x, H - padB + 20, fmt(t, 1), "tick", "middle");
        });
        text(s, padL + plotW / 2, H - padB + 38, "Mean points per player-gameweek", "axis-title", "middle");
        text(s, W - padR + 12, padT - 2, "RMSE", "axis-title");

        rows.forEach(function (row, i) {
            var y = padT + i * groupH;
            text(s, padL - 12, y + 27, row.pos, "tick-strong", "end");
            [["pred", PAL[0], 0], ["actual", PAL[1], 1]].forEach(function (pair) {
                var w = (row[pair[0]] / max) * plotW;
                bar(s, padL, y + 8 + pair[2] * 16, Math.max(2, w), 14, pair[1]);
            });
            text(s, W - padR + 12, y + 27, fmt(row.rmse, 2), "val-label");

            var hit = el("rect", { x: 0, y: y, width: W, height: groupH, class: "hit" }, s);
            bindTip(hit, "<b>" + row.pos + "</b>" +
                tipRow(PAL[0], "Mean predicted", fmt(row.pred, 3)) +
                tipRow(PAL[1], "Mean actual", fmt(row.actual, 3)) +
                tipRow(REF, "RMSE", fmt(row.rmse, 3)) +
                '<div class="row" style="color:#91849e">' + row.n.toLocaleString() + " player-gameweeks</div>");
        });
    }

    /* ================= Tables rendered from the same data ================= */

    function fillTables() {
        var holdout = document.getElementById("tbl-holdout");
        if (holdout) {
            D.holdout.forEach(function (row, i) {
                var tr = document.createElement("tr");
                if (i === 0) tr.className = "best";
                tr.innerHTML = "<td class=\"name\">" + row.label + "</td>" +
                    "<td class=\"num\">" + fmt(row.rmse, 4) + "</td>" +
                    "<td class=\"num\">" + fmt(row.mae, 4) + "</td>" +
                    "<td class=\"num\">" + fmt(row.r2, 4) + "</td>";
                holdout.appendChild(tr);
            });
        }

        var cv = document.getElementById("tbl-cv");
        if (cv) {
            D.cv_summary.slice().sort(function (a, b) { return a.rmse - b.rmse; }).forEach(function (row, i) {
                var tr = document.createElement("tr");
                if (i === 0) tr.className = "best";
                tr.innerHTML = "<td class=\"name\">" + row.label + "</td>" +
                    "<td class=\"num\">" + fmt(row.rmse, 4) + " ± " + fmt(row.std, 3) + "</td>" +
                    "<td class=\"num\">" + fmt(row.mae, 4) + "</td>" +
                    "<td class=\"num\">" + fmt(row.r2, 4) + "</td>" +
                    "<td class=\"num\">" + fmt(row.secs, 1) + "s</td>";
                cv.appendChild(tr);
            });
        }

        var bt = document.getElementById("tbl-backtest");
        if (bt) {
            var b = D.backtest;
            var mean = function (k) { return b.reduce(function (a, r) { return a + r[k]; }, 0) / b.length; };
            var wins = function (k) { return b.filter(function (r) { return r.ai > r[k]; }).length; };
            var hasCap = function (k) { return b[0][k + "_cap"] !== undefined; };
            [
                ["OpenFPL ensemble", "ai", null],
                ["Baseline: 5-gameweek form", "form5", "form5"],
                ["Baseline: FPL's own xP", "fpl_xp", "fpl_xp"],
                ["Baseline: last gameweek", "last", "last"],
                ["Random legal squad", "random", "random"],
                ["Perfect hindsight (ceiling)", "perfect", null]
            ].forEach(function (row, i) {
                var tr = document.createElement("tr");
                if (i === 0) tr.className = "best";
                tr.innerHTML = "<td class=\"name\">" + row[0] + "</td>" +
                    "<td class=\"num\">" + fmt(mean(row[1]), 1) + "</td>" +
                    "<td class=\"num\">" + Math.round(mean(row[1]) * 38).toLocaleString() + "</td>" +
                    "<td class=\"num\">" + (hasCap(row[1]) ? fmt(mean(row[1] + "_cap"), 2) : "—") + "</td>" +
                    "<td class=\"num\">" + (row[2] ? wins(row[2]) + " / 38" : "—") + "</td>";
                bt.appendChild(tr);
            });
        }

        var pos = document.getElementById("tbl-position");
        if (pos) {
            D.by_position.forEach(function (row) {
                var tr = document.createElement("tr");
                tr.innerHTML = "<td class=\"name\">" + row.pos + "</td>" +
                    "<td class=\"num\">" + row.n.toLocaleString() + "</td>" +
                    "<td class=\"num\">" + fmt(row.rmse, 3) + "</td>" +
                    "<td class=\"num\">" + fmt(row.mae, 3) + "</td>" +
                    "<td class=\"num\">" + fmt(row.pred, 3) + "</td>" +
                    "<td class=\"num\">" + fmt(row.actual, 3) + "</td>";
                pos.appendChild(tr);
            });
        }
    }

    /* ---------- boot ---------- */

    function boot() {
        holdoutChart();
        cvChart();
        calibrationChart();
        weeklyChart();
        backtestSummary();
        backtestWeekly();
        importanceChart();
        positionChart();
        fillTables();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();

// Tiny SVG charts for the pitcher dashboard: a line chart and a grouped bar chart.
// One y-axis each, thin marks, direct end labels, a hover tooltip, and a table view.

const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, style = {}) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    Object.assign(el.style, style);
    return el;
}

function niceTicks(min, max, count = 4) {
    if (min === max) {
        min -= 1;
        max += 1;
    }
    const span = max - min;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count + 0.5) || step0;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return ticks;
}

function chartShell(container, { title, note, legend }) {
    const box = document.createElement("div");
    box.className = "chart";
    const h = document.createElement("h4");
    h.textContent = title;
    box.appendChild(h);
    if (note) {
        const n = document.createElement("div");
        n.className = "note";
        n.textContent = note;
        box.appendChild(n);
    }
    if (legend && legend.length > 1) {
        const lg = document.createElement("div");
        lg.className = "legend";
        for (const s of legend) {
            const span = document.createElement("span");
            const i = document.createElement("i");
            i.style.background = s.color;
            span.append(i, s.name);
            lg.appendChild(span);
        }
        box.appendChild(lg);
    }
    const tt = document.createElement("div");
    tt.className = "tt";
    box.appendChild(tt);
    container.appendChild(box);
    return { box, tt };
}

function tableView(box, headers, rows) {
    const d = document.createElement("details");
    const s = document.createElement("summary");
    s.textContent = "Table view";
    const t = document.createElement("table");
    t.innerHTML = "<tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr>";
    for (const r of rows) {
        const tr = document.createElement("tr");
        for (const c of r) {
            const td = document.createElement("td");
            td.textContent = c ?? "–";
            tr.appendChild(td);
        }
        t.appendChild(tr);
    }
    d.append(s, t);
    box.appendChild(d);
}

function showTip(tt, box, html, px, py) {
    tt.innerHTML = html;
    tt.style.display = "block";
    const bw = box.clientWidth;
    const w = tt.offsetWidth;
    let left = px + 14;
    if (left + w > bw - 6) left = px - w - 14;
    tt.style.left = Math.max(4, left) + "px";
    tt.style.top = Math.max(4, py - 10) + "px";
}

/**
 * lineChart(container, {title, note, series: [{name, color, points: [{x, y}]}], xTicks: [{v, label}], fmt})
 * Points of one series are joined in x order; a gap in x (missing month) is drawn as a break.
 */
function lineChart(container, { title, note, series, xTicks, fmt = (v) => v, xLabel = (x) => x, gapOver = 1 }) {
    const live = series.filter((s) => s.points.length);
    const { box, tt } = chartShell(container, { title, note, legend: live });
    if (!live.length) {
        box.insertAdjacentHTML("beforeend", '<div class="empty">No data</div>');
        return;
    }
    const W = 520, H = 220, m = { l: 44, r: 52, t: 10, b: 26 };
    const xs = [...new Set(live.flatMap((s) => s.points.map((p) => p.x)))].sort((a, b) => a - b);
    const ys = live.flatMap((s) => s.points.map((p) => p.y)).filter((v) => v != null);
    const yt = niceTicks(Math.min(...ys), Math.max(...ys));
    const x0 = Math.min(...xs, ...(xTicks || []).map((t) => t.v)), x1 = Math.max(...xs, ...(xTicks || []).map((t) => t.v));
    const sx = (x) => m.l + ((x - x0) / Math.max(1e-9, x1 - x0)) * (W - m.l - m.r);
    const sy = (y) => H - m.b - ((y - yt[0]) / (yt[yt.length - 1] - yt[0])) * (H - m.t - m.b);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });

    for (const t of yt) {
        svg.appendChild(svgEl("line", { x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t) }, { stroke: "var(--grid)", strokeWidth: 1 }));
        const tx = svgEl("text", { x: m.l - 6, y: sy(t) + 4, "text-anchor": "end" });
        tx.textContent = fmt(t);
        svg.appendChild(tx);
    }
    svg.appendChild(svgEl("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }, { stroke: "var(--axis)", strokeWidth: 1 }));
    for (const t of xTicks || xs.map((v) => ({ v, label: xLabel(v) }))) {
        const tx = svgEl("text", { x: sx(t.v), y: H - 8, "text-anchor": "middle" });
        tx.textContent = t.label;
        svg.appendChild(tx);
    }

    // Lines, markers, and a label at the end of each line (identity is never color alone)
    const ends = [];
    for (const s of live) {
        const pts = [...s.points].filter((p) => p.y != null).sort((a, b) => a.x - b.x);
        let d = "";
        pts.forEach((p, i) => {
            const jump = i === 0 || p.x - pts[i - 1].x > gapOver;
            d += (jump ? "M" : "L") + sx(p.x).toFixed(1) + "," + sy(p.y).toFixed(1);
        });
        svg.appendChild(svgEl("path", { d, fill: "none" }, { stroke: s.color, strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" }));
        for (const p of pts) svg.appendChild(svgEl("circle", { cx: sx(p.x), cy: sy(p.y), r: 3.5 }, { fill: s.color, stroke: "var(--surface)", strokeWidth: 2 }));
        const last = pts[pts.length - 1];
        if (last) ends.push({ y: sy(last.y), x: sx(last.x), name: s.name });
    }
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13; // no collisions
    for (const e of ends) {
        const tx = svgEl("text", { x: e.x + 8, y: e.y + 4, class: "lbl" });
        tx.textContent = e.name;
        svg.appendChild(tx);
    }

    // Hover: a crosshair at the nearest x, every series' value there
    const cross = svgEl("line", { y1: m.t, y2: H - m.b, visibility: "hidden" }, { stroke: "var(--ink-2)", strokeWidth: 1, strokeDasharray: "3 3" });
    svg.appendChild(cross);
    const hit = svgEl("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.appendChild(hit);
    hit.addEventListener("pointermove", (ev) => {
        const r = svg.getBoundingClientRect();
        const vx = ((ev.clientX - r.left) / r.width) * W;
        const near = xs.reduce((a, b) => (Math.abs(sx(b) - vx) < Math.abs(sx(a) - vx) ? b : a));
        cross.setAttribute("x1", sx(near));
        cross.setAttribute("x2", sx(near));
        cross.setAttribute("visibility", "visible");
        const rows = live
            .map((s) => [s, s.points.find((p) => p.x === near)])
            .filter(([, p]) => p && p.y != null)
            .map(([s, p]) => `<div><i style="background:${s.color}"></i>${s.name}: <b style="display:inline">${fmt(p.y)}</b>${p.extra ? " · " + p.extra : ""}</div>`)
            .join("");
        const bb = box.getBoundingClientRect();
        showTip(tt, box, `<b>${xLabel(near)}</b>${rows}`, ev.clientX - bb.left, ev.clientY - bb.top);
    });
    hit.addEventListener("pointerleave", () => {
        cross.setAttribute("visibility", "hidden");
        tt.style.display = "none";
    });
    box.insertBefore(svg, tt);
    tableView(box, ["", ...xs.map(xLabel)], live.map((s) => [s.name, ...xs.map((x) => {
        const p = s.points.find((q) => q.x === x);
        return p && p.y != null ? fmt(p.y) : "–";
    })]));
}

/**
 * barChart(container, {title, note, categories: [...], groups: [{name, color}], value(cat, group) -> number|null, fmt})
 * Grouped vertical bars, 2px gap between bars, 4px rounded tops anchored to the baseline.
 */
function barChart(container, { title, note, categories, groups, value, fmt = (v) => v, detail = () => "" }) {
    const { box, tt } = chartShell(container, { title, note, legend: groups });
    if (!categories.length) {
        box.insertAdjacentHTML("beforeend", '<div class="empty">No data</div>');
        return;
    }
    const W = 520, H = 220, m = { l: 40, r: 8, t: 10, b: 26 };
    const vals = categories.flatMap((c) => groups.map((g) => value(c, g))).filter((v) => v != null);
    const yt = niceTicks(0, Math.max(...vals, 0.0001));
    const sy = (y) => H - m.b - (y / yt[yt.length - 1]) * (H - m.t - m.b);
    const band = (W - m.l - m.r) / categories.length;
    const inner = band * 0.78;
    const bw = Math.max(3, (inner - 2 * (groups.length - 1)) / groups.length);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
    for (const t of yt) {
        svg.appendChild(svgEl("line", { x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t) }, { stroke: "var(--grid)", strokeWidth: 1 }));
        const tx = svgEl("text", { x: m.l - 6, y: sy(t) + 4, "text-anchor": "end" });
        tx.textContent = fmt(t);
        svg.appendChild(tx);
    }
    categories.forEach((c, ci) => {
        const bx = m.l + ci * band + (band - inner) / 2;
        groups.forEach((g, gi) => {
            const v = value(c, g);
            if (v == null) return;
            const x = bx + gi * (bw + 2), y = sy(v), h = Math.max(0, H - m.b - y), r = Math.min(4, bw / 2, h);
            const d = `M${x},${H - m.b}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${H - m.b}Z`;
            const bar = svgEl("path", { d }, { fill: g.color, cursor: "default" });
            svg.appendChild(bar);
            // A hit target taller than the bar
            const hitR = svgEl("rect", { x: x - 1, y: m.t, width: bw + 2, height: H - m.t - m.b, fill: "transparent" });
            hitR.addEventListener("pointermove", (ev) => {
                const bb = box.getBoundingClientRect();
                showTip(tt, box, `<b>${c} · ${g.name}</b>${fmt(v)}${detail(c, g)}`, ev.clientX - bb.left, ev.clientY - bb.top);
                bar.style.opacity = 0.8;
            });
            hitR.addEventListener("pointerleave", () => {
                tt.style.display = "none";
                bar.style.opacity = 1;
            });
            svg.appendChild(hitR);
        });
        const tx = svgEl("text", { x: m.l + ci * band + band / 2, y: H - 8, "text-anchor": "middle", class: "lbl" });
        tx.textContent = c;
        svg.appendChild(tx);
    });
    svg.appendChild(svgEl("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }, { stroke: "var(--axis)", strokeWidth: 1 }));
    box.insertBefore(svg, tt);
    tableView(box, ["", ...groups.map((g) => g.name)], categories.map((c) => [c, ...groups.map((g) => {
        const v = value(c, g);
        return v == null ? "–" : fmt(v);
    })]));
}

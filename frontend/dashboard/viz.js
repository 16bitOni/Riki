/* viz.js — formatting helpers and hand-rolled SVG charts (no libraries). */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const NS = "http://www.w3.org/2000/svg";

/* Every piece of dynamic text goes through esc(): chat messages and memories are untrusted HTML. */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const C = { g: "#5dffa6", c: "#55d6ff", a: "#ffb347", r: "#ff5c74", v: "#b794ff", dim: "#7d91a1", mute: "#46586a" };

function fmtMs(v) {
  v = +v || 0;
  if (v >= 60000) return (v / 60000).toFixed(1) + "m";
  if (v >= 10000) return Math.round(v / 1000) + "s";
  if (v >= 1000) return (v / 1000).toFixed(1) + "s";
  return Math.round(v) + "ms";
}
function fmtN(v) {
  v = +v || 0;
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  if (a >= 1e3) return (v / 1e3).toFixed(2) + "k";
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
const fmtInt = (v) => Math.round(+v || 0).toLocaleString("en-US");
function fmtUsd(v) {
  v = +v || 0;
  if (v >= 100) return "$" + v.toFixed(0);
  if (v >= 1) return "$" + v.toFixed(2);
  if (v >= 0.01) return "$" + v.toFixed(3);
  return "$" + v.toFixed(4);
}
const fmtPct = (v) => ((+v || 0) * 100).toFixed((+v || 0) < 0.1 && v > 0 ? 1 : 0) + "%";
function fmtBytes(v) {
  v = +v || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(v >= 100 ? 0 : 1) : v) + " " + u[i];
}
function fmtDur(s) {
  s = +s || 0;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`;
}
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 5) return "just now";
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
const pad = (n) => String(n).padStart(2, "0");
const hms = (t) => { const d = new Date(t); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const stamp = (iso) => { const d = new Date(iso); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms(d)}`; };

function tickLabel(t, span) {
  const d = new Date(t);
  if (span <= 36 * 3600e3) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (span <= 14 * 86400e3) return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}h`;
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

/* ---- tooltip (one shared element) */
let tipEl;
function tip() {
  if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; document.body.appendChild(tipEl); }
  return tipEl;
}
function showTip(html, ev) {
  const t = tip();
  t.innerHTML = html;
  t.style.opacity = 1;
  const w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + 16, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 16;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  t.style.left = x + "px"; t.style.top = y + "px";
}
const hideTip = () => { if (tipEl) tipEl.style.opacity = 0; };

let uid = 0;

/* ---- time-series line / area chart.
   o: { xs:[ms], series:[{name,color,data,area,dash,width}], height, yfmt, ymax, stacked, animate, tipfmt } */
function lineChart(el, o) {
  const xs = o.xs || [];
  if (!xs.length || !o.series.some((s) => s.data.some((v) => v != null && v > 0)) && !o.showEmpty) {
    el.innerHTML = '<div class="empty">NO DATA IN RANGE</div>';
    return;
  }
  const W = Math.max(320, el.clientWidth || 640), H = o.height || 190;
  const m = { l: 46, r: 10, t: 10, b: 22 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b, n = xs.length;
  const yfmt = o.yfmt || fmtN;
  let stackTop = null;
  const series = o.series.map((s) => ({ ...s }));
  if (o.stacked) {
    stackTop = new Array(n).fill(0);
    series.forEach((s) => { s.base = stackTop.slice(); s.top = s.data.map((v, i) => (stackTop[i] += v ?? 0)); });
  } else series.forEach((s) => { s.base = new Array(n).fill(0); s.top = s.data; });
  let ymax = o.ymax ?? Math.max(...series.flatMap((s) => s.top.map((v) => v ?? 0)), 0);  // null = no data: drawn as a gap
  ymax = niceMax(ymax * 1.04);
  const X = (i) => m.l + (n === 1 ? pw / 2 : (i * pw) / (n - 1));
  const Y = (v) => m.t + ph - (Math.min(v, ymax) / ymax) * ph;
  const span = xs[n - 1] - xs[0];
  const id = "g" + ++uid;
  let out = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img"><defs>`;
  series.forEach((s, k) => {
    out += `<linearGradient id="${id}-${k}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".38"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`;
  });
  out += "</defs>";
  for (let k = 0; k <= 4; k++) {
    const v = (ymax * k) / 4, y = Y(v);
    out += `<line class="grid-l" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><g class="axis"><text x="${m.l - 7}" y="${y + 3}" text-anchor="end">${esc(yfmt(v))}</text></g>`;
  }
  const ticks = Math.min(6, n);
  for (let k = 0; k < ticks; k++) {
    const i = ticks === 1 ? 0 : Math.round((k * (n - 1)) / (ticks - 1));
    out += `<g class="axis"><text x="${X(i)}" y="${H - 5}" text-anchor="${k === 0 ? "start" : k === ticks - 1 ? "end" : "middle"}">${tickLabel(xs[i], o.daily ? Infinity : span)}</text></g>`;
  }
  series.forEach((s, k) => {
    const segments = [];
    let run = [];
    // connect:true joins the points on either side of an empty bucket (right for latency); otherwise it is a gap
    s.top.forEach((v, i) => { if (v == null) { if (run.length && !o.connect) { segments.push(run); run = []; } } else run.push(i); });
    if (run.length) segments.push(run);
    segments.forEach((seg) => {
      if (seg.length === 1) { out += `<circle cx="${X(seg[0]).toFixed(1)}" cy="${Y(s.top[seg[0]]).toFixed(1)}" r="2" fill="${s.color}"/>`; return; }
      const line = seg.map((i, j) => `${j ? "L" : "M"}${X(i).toFixed(1)},${Y(s.top[i]).toFixed(1)}`).join("");
      if (s.area !== false && (o.stacked || s.area)) {
        const back = seg.slice().reverse().map((i) => `L${X(i).toFixed(1)},${Y(s.base[i]).toFixed(1)}`).join("");
        out += `<path d="${line}${back}Z" fill="url(#${id}-${k})"/>`;
      }
      out += `<path class="${o.animate ? "draw" : ""}" pathLength="1" style="--len:1" d="${line}" fill="none" stroke="${s.color}" stroke-width="${s.width || 1.6}" stroke-linejoin="round" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/>`;
    });
  });
  out += `<line class="hover-line" y1="${m.t}" y2="${m.t + ph}"/><g class="dots"></g><rect class="hit" x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/></svg>`;
  el.innerHTML = out;
  const svg = el.firstElementChild, hit = $(".hit", svg), line = $(".hover-line", svg), dots = $(".dots", svg);
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), mx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((mx - m.l) / pw) * (n - 1))));
    line.setAttribute("x1", X(i)); line.setAttribute("x2", X(i)); line.style.opacity = 1;
    dots.innerHTML = series.map((s) => (s.top[i] == null ? "" : `<circle cx="${X(i)}" cy="${Y(s.top[i])}" r="3" fill="#050709" stroke="${s.color}" stroke-width="1.5"/>`)).join("");
    const when = new Date(xs[i]);
    showTip(
      `<div class="t">${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}</div>` +
        series.map((s) => `<div class="row"><span><i style="background:${s.color}"></i>${esc(s.name)}</span><b>${s.data[i] == null ? "—" : esc((s.fmt || o.tipfmt || yfmt)(s.data[i]))}</b></div>`).join(""),
      ev
    );
  });
  hit.addEventListener("mouseleave", () => { line.style.opacity = 0; dots.innerHTML = ""; hideTip(); });
}

/* ---- time-series bars (optionally stacked). Same options as lineChart. */
function barChart(el, o) {
  const xs = o.xs || [];
  if (!xs.length || !o.series.some((s) => s.data.some((v) => v > 0))) {
    el.innerHTML = '<div class="empty">NO DATA IN RANGE</div>';
    return;
  }
  const W = Math.max(320, el.clientWidth || 640), H = o.height || 170;
  const m = { l: 46, r: 10, t: 10, b: 22 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b, n = xs.length;
  const yfmt = o.yfmt || fmtN;
  const totals = xs.map((_, i) => o.series.reduce((a, s) => a + s.data[i], 0));
  const ymax = niceMax(Math.max(...(o.stacked === false ? o.series.flatMap((s) => s.data) : totals)) * 1.05);
  const bw = pw / n, gap = Math.min(2, bw * 0.2);
  const Y = (v) => m.t + ph - (v / ymax) * ph;
  const span = xs[n - 1] - xs[0];
  let out = `<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
  for (let k = 0; k <= 4; k++) {
    const v = (ymax * k) / 4, y = Y(v);
    out += `<line class="grid-l" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><g class="axis"><text x="${m.l - 7}" y="${y + 3}" text-anchor="end">${esc(yfmt(v))}</text></g>`;
  }
  const ticks = Math.min(6, n);
  for (let k = 0; k < ticks; k++) {
    const i = ticks === 1 ? 0 : Math.round((k * (n - 1)) / (ticks - 1));
    out += `<g class="axis"><text x="${m.l + i * bw + bw / 2}" y="${H - 5}" text-anchor="${k === 0 ? "start" : k === ticks - 1 ? "end" : "middle"}">${tickLabel(xs[i], o.daily ? Infinity : span)}</text></g>`;
  }
  const grouped = o.stacked === false, slot = grouped ? (bw - gap) / o.series.length : bw - gap;
  xs.forEach((_, i) => {
    let acc = 0;
    o.series.forEach((s, si) => {
      const v = s.data[i];
      if (v <= 0) return;
      const base = grouped ? 0 : acc, y0 = Y(base), y1 = Y(base + v);
      acc += v;
      const x = m.l + i * bw + gap / 2 + (grouped ? si * slot : 0);
      out += `<rect class="bar" data-i="${i}" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${Math.max(1, slot - (grouped ? 1 : 0)).toFixed(1)}" height="${Math.max(1, y0 - y1).toFixed(1)}" fill="${s.color}" opacity=".88"/>`;
    });
  });
  out += `<line class="hover-line" y1="${m.t}" y2="${m.t + ph}"/><rect class="hit" x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/></svg>`;
  el.innerHTML = out;
  const svg = el.firstElementChild, hit = $(".hit", svg), line = $(".hover-line", svg);
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), mx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.floor((mx - m.l) / bw)));
    const cx = m.l + i * bw + bw / 2;
    line.setAttribute("x1", cx); line.setAttribute("x2", cx); line.style.opacity = 1;
    const when = new Date(xs[i]);
    showTip(
      `<div class="t">${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}</div>` +
        o.series.map((s) => `<div class="row"><span><i style="background:${s.color}"></i>${esc(s.name)}</span><b>${esc((s.fmt || yfmt)(s.data[i]))}</b></div>`).join(""),
      ev
    );
  });
  hit.addEventListener("mouseleave", () => { line.style.opacity = 0; hideTip(); });
}

/* ---- categorical vertical bars (histograms). items: [{label, n, color?}] */
function histogram(el, items, o = {}) {
  if (!items.length || !items.some((i) => i.n > 0)) { el.innerHTML = '<div class="empty">NO DATA IN RANGE</div>'; return; }
  const W = Math.max(280, el.clientWidth || 520), H = o.height || 150;
  const m = { l: 30, r: 6, t: 16, b: 34 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b, n = items.length, bw = pw / n;
  const ymax = niceMax(Math.max(...items.map((i) => i.n)) * 1.05);
  let out = `<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
  for (let k = 0; k <= 2; k++) {
    const v = (ymax * k) / 2, y = m.t + ph - (v / ymax) * ph;
    out += `<line class="grid-l" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><g class="axis"><text x="${m.l - 6}" y="${y + 3}" text-anchor="end">${fmtN(v)}</text></g>`;
  }
  items.forEach((it, i) => {
    const h = (it.n / ymax) * ph, x = m.l + i * bw + 3, y = m.t + ph - h, color = it.color || o.color || C.c;
    out += `<rect class="bar" x="${x}" y="${y}" width="${Math.max(2, bw - 6)}" height="${Math.max(it.n ? 1 : 0, h)}" fill="${color}" opacity=".85"><title>${esc(it.label)}: ${it.n}</title></rect>`;
    if (it.n) out += `<g class="axis"><text x="${x + (bw - 6) / 2}" y="${y - 4}" text-anchor="middle" style="fill:#9bb0c0">${fmtN(it.n)}</text></g>`;
    out += `<g class="axis"><text transform="translate(${x + (bw - 6) / 2},${H - m.b + 12}) rotate(${n > 7 ? -28 : 0})" text-anchor="${n > 7 ? "end" : "middle"}">${esc(it.label)}</text></g>`;
  });
  el.innerHTML = out + "</svg>";
}

/* ---- sparkline for KPI cards: returns an svg string */
function spark(data, color = C.g) {
  if (!data || data.length < 2 || !data.some((v) => v > 0)) return "";
  const max = Math.max(...data, 1e-9), n = data.length;
  const pts = data.map((v, i) => [(i * 100) / (n - 1), 28 - (v / max) * 24]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const id = "s" + ++uid;
  return `<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${line}L100,30L0,30Z" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.2" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ---- horizontal bars (HTML). items: [{name, value, label?, color?}] */
function hbars(items, o = {}) {
  if (!items.length) return '<div class="empty">NOTHING YET</div>';
  const max = o.max ?? Math.max(...items.map((i) => i.value), 1e-9);
  return `<div class="hbars">${items
    .map(
      (i) => `<div class="hb"${i.click ? ` data-click="${esc(i.click)}"` : ""}><div class="name" title="${esc(i.name)}">${esc(i.name)}</div><div class="track"><div class="fill ${i.color || o.color || ""}" style="width:${Math.max(i.value ? 1.5 : 0, (i.value / max) * 100).toFixed(1)}%"></div></div><div class="v-n">${esc(i.label ?? fmtN(i.value))}</div></div>`
    )
    .join("")}</div>`;
}

/* ---- one stacked proportion bar with a legend. items: [{name, value, color}] */
function stackBar(items) {
  const total = items.reduce((a, i) => a + i.value, 0);
  if (!total) return '<div class="empty">NOTHING YET</div>';
  return (
    `<div class="stack">${items.filter((i) => i.value).map((i) => `<div style="flex:${i.value};background:${i.color}" title="${esc(i.name)}: ${i.value}"></div>`).join("")}</div>` +
    `<div class="stack-legend">${items.map((i) => `<span><i style="background:${i.color}"></i>${esc(i.name)} <b style="color:#fff">${fmtN(i.value)}</b> <span class="mute">${fmtPct(i.value / total)}</span></span>`).join("")}</div>`
  );
}

/* ---- trace waterfall. spans need offset_ms and duration_ms. */
function waterfall(spans, total) {
  total = Math.max(total, ...spans.map((s) => s.offset_ms + s.duration_ms), 1);
  return `<div class="wf">${spans
    .map((s, i) => {
      const left = (s.offset_ms / total) * 100, width = Math.max(0.4, (s.duration_ms / total) * 100);
      return `<div class="wf-row" data-span="${i}"><div class="wf-name" title="${esc(s.name)}"><span class="k">${esc(s.kind)}</span>${esc(s.name)}</div><div class="wf-track"><div class="wf-bar k-${esc(s.kind)} ${s.status === "error" ? "k-err" : ""}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"></div></div><div class="wf-ms">${fmtMs(s.duration_ms)}</div></div>`;
    })
    .join("")}</div>`;
}

/* ---- count-up animation for KPI numbers (first paint only) */
function countUp(root) {
  $$("[data-count]", root).forEach((el) => {
    const to = +el.dataset.count, kind = el.dataset.kind || "n";
    const f = { n: fmtN, ms: fmtMs, usd: fmtUsd, pct: fmtPct, int: fmtInt }[kind] || fmtN;
    const t0 = performance.now(), dur = 700;
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.firstChild.nodeValue = f(to * e);
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

/* ---- KPI card */
function kpi(o) {
  // o: {label, value, kind, unit, tone, delta:{pct, upBad}, spark:[...], sparkColor, sub, i}
  let delta = "";
  if (o.delta && isFinite(o.delta.pct)) {
    const up = o.delta.pct > 0.0005, down = o.delta.pct < -0.0005;
    const cls = !up && !down ? "" : up ? (o.delta.upBad ? "up-bad" : o.delta.neutral ? "" : "up-good") : (o.delta.upBad ? "down-good" : o.delta.neutral ? "" : "down-good-r");
    delta = `<span class="delta ${cls}" title="vs the previous period of the same length">${up ? "▲" : down ? "▼" : "•"} ${Math.abs(o.delta.pct * 100).toFixed(Math.abs(o.delta.pct) < 0.1 ? 1 : 0)}%</span>`;
  }
  const f = { n: fmtN, ms: fmtMs, usd: fmtUsd, pct: fmtPct, int: fmtInt, raw: (v) => v }[o.kind || "n"];
  const raw = typeof o.value === "number";
  return `<div class="kpi ${o.tone || ""}" style="--i:${o.i || 0}"><div class="lab">${esc(o.label)}</div><div class="val" ${raw && o.kind !== "raw" ? `data-count="${o.value}" data-kind="${o.kind || "n"}"` : ""}>${esc(raw ? f(o.value) : o.value)}${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</div><div class="sub"><span>${esc(o.sub || "")}</span>${delta}</div>${spark(o.spark, o.sparkColor || C.g)}</div>`;
}

const pctChange = (now, before) => (before ? (now - before) / before : now ? 1 : 0);
const sum = (a) => a.reduce((x, y) => x + y, 0);
const statusChip = (s) => (s === "ok" ? '<span class="chip g">ok</span>' : '<span class="chip r">error</span>');
const kindChip = (k) => `<span class="chip ${{ chat: "c", extraction: "v", operator: "a" }[k] || ""}">${esc(k)}</span>`;
const latencyClass = (ms) => (ms > 30000 ? "r" : ms > 10000 ? "a" : "");

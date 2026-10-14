/* pages-a.js — Overview, Traces, Retrieval. */
"use strict";

const PAGES = [];
let _i = 0;
const resetIdx = () => { _i = 0; };

function panel(title, body, o = {}) {
  const legend = o.legend ? `<div class="legend">${o.legend.map((l) => `<span><i style="background:${l[1]}"></i>${esc(l[0])}</span>`).join("")}</div>` : "";
  const note = o.note ? `<div class="note">${o.note}</div>` : "";
  return `<section class="panel span-${o.span || 12} ${o.cls || ""}" style="--i:${_i++}"><div class="ph"><h2>${esc(title)}</h2>${legend}${note}</div>${body}</section>`;
}

const CHANNEL_COLOR = { keyword: "c", semantic: "v", graph: "g", time: "a" };
const CHANNEL_HEX = { keyword: C.c, semantic: C.v, graph: C.g, time: C.a };
const CHANNEL_TAG = { keyword: "K", semantic: "S", graph: "G", time: "T" };

/* =====================================================================  OVERVIEW */
PAGES.push({
  id: "overview", n: "01", title: "Overview", sub: "system pulse", ranged: true,
  async load() {
    const [o, alerts] = await Promise.all([api("/obs/overview", { hours: App.hours }), api("/obs/alerts")]);
    return { o, alerts };
  },
  render(root, { o, alerts }, ctx) {
    const cur = o.current, prev = o.previous, S = o.series, xs = S.map((s) => s.t);
    const col = (k) => S.map((s) => s[k]);
    const idle = cur.requests === 0;
    const er = cur.error_rate;
    const kpis = [
      kpi({ label: "Requests", value: cur.requests, kind: "int", sub: `${o.by_source.map((s) => s.source + " " + s.n).join(" · ") || "no traffic"}`, delta: { pct: pctChange(cur.requests, prev.requests), neutral: true }, spark: col("requests"), sparkColor: C.c, i: 0 }),
      kpi({ label: "Error rate", value: idle ? "—" : fmtPct(er), kind: "raw", tone: idle ? "" : er > 0.25 ? "bad" : er > 0.08 ? "warn" : "good", sub: `${cur.errors} failed of ${cur.requests}`, delta: { pct: pctChange(er, prev.error_rate), upBad: true }, spark: col("errors"), sparkColor: C.r, i: 1 }),
      kpi({ label: "Latency p50", value: cur.p50, kind: "ms", sub: `p99 ${fmtMs(cur.p99)}`, delta: { pct: pctChange(cur.p50, prev.p50), upBad: true }, spark: col("p50"), sparkColor: C.g, i: 2 }),
      kpi({ label: "Latency p95", value: cur.p95, kind: "ms", tone: cur.p95 > 60000 ? "bad" : cur.p95 > 30000 ? "warn" : "", sub: "tail latency", delta: { pct: pctChange(cur.p95, prev.p95), upBad: true }, spark: col("p95"), sparkColor: C.a, i: 3 }),
      kpi({ label: "Tokens", value: cur.tokens_in + cur.tokens_out, kind: "n", sub: `in ${fmtN(cur.tokens_in)} · out ${fmtN(cur.tokens_out)}`, delta: { pct: pctChange(cur.tokens_in + cur.tokens_out, prev.tokens_in + prev.tokens_out), upBad: true }, spark: S.map((s) => s.tokens_in + s.tokens_out), sparkColor: C.v, i: 4 }),
      kpi({ label: "Est. cost", value: cur.cost, kind: "usd", sub: cur.requests ? `${fmtUsd(cur.cost / cur.requests)} per turn` : "chat turns only", delta: { pct: pctChange(cur.cost, prev.cost), upBad: true }, spark: col("cost"), sparkColor: C.a, i: 5 }),
      kpi({ label: "Model calls", value: cur.llm_calls, kind: "int", sub: cur.requests ? `${(cur.llm_calls / cur.requests).toFixed(1)} per turn` : "", i: 6 }),
      kpi({ label: "Tool calls", value: cur.tool_calls, kind: "int", sub: cur.requests ? `${(cur.tool_calls / cur.requests).toFixed(2)} per turn` : "", i: 7 }),
      kpi({ label: "Time to first token", value: o.ttft_p50, kind: "ms", sub: "median, streamed", i: 8 }),
      kpi({ label: "Memories recalled", value: cur.recalled, kind: "raw", sub: "average per turn", i: 9 }),
    ].join("");

    const crit = alerts.filter((a) => a.severity !== "info").length;
    const alertHtml = alerts.length
      ? `<div class="alerts">${alerts.map((a) => `<div class="alert ${esc(a.severity)}"><span class="sev">${esc(a.severity)}</span><span><span class="ttl">${esc(a.title)}</span><span class="det">${esc(a.detail)}</span></span></div>`).join("")}</div>`
      : `<div class="allclear"><span class="pulse"></span>all systems nominal · no active alerts</div>`;

    const okSeries = S.map((s) => Math.max(0, s.requests - s.errors));
    const topErrors = o.top_errors.length
      ? `<div class="tablewrap" style="max-height:260px"><table><thead><tr><th>Where</th><th>Type</th><th class="num">×</th></tr></thead><tbody>${o.top_errors.map((e) => `<tr title="${esc(e.message)}"><td class="clip"><span class="chip">${esc(e.kind)}</span> ${esc(e.name)}</td><td class="r">${esc(e.error_type)}</td><td class="num">${e.n}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO ERRORS IN RANGE</div>';
    const slowest = o.slowest.length
      ? `<div class="tablewrap" style="max-height:260px"><table><tbody>${o.slowest.map((t) => `<tr class="click" data-trace="${esc(t.id)}"><td class="clip" style="max-width:190px">${esc(t.user_text || "(empty)")}</td><td class="num ${latencyClass(t.duration_ms)}">${fmtMs(t.duration_ms)}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO TURNS</div>';
    const bg = o.background.map((b) => `<div class="hb"><div class="name">${kindChip(b.kind)}</div><div class="track"><div class="fill ${b.errors ? "a" : "v"}" style="width:100%;opacity:.35"></div></div><div class="v-n">${b.runs} runs${b.errors ? ` · <span class="r">${b.errors} err</span>` : ""} · ${fmtUsd(b.cost)}</div></div>`).join("") || '<div class="empty">NO BACKGROUND WORK</div>';
    const inflight = o.in_flight.length
      ? o.in_flight.map((t) => `<div class="hb"><div class="name">${kindChip(t.kind)} ${esc(t.source)}</div><div class="track"><div class="fill g" style="width:60%"></div></div><div class="v-n">${fmtMs(t.age_ms)}</div></div>`).join("")
      : '<div class="dim" style="padding:4px 0">nothing running right now</div>';

    root.innerHTML =
      alertHtml + `<div class="kpis c5">${kpis}</div><div class="grid">` +
      panel("Traffic", '<div class="chart" id="c-traffic"></div>', { span: 7, legend: [["ok", C.c], ["failed", C.r]], note: `bucket ${fmtMs(o.bucket * 1000)}` }) +
      panel("Latency", '<div class="chart" id="c-lat"></div>', { span: 5, legend: [["p50", C.g], ["p95", C.a]] }) +
      panel("Tokens", '<div class="chart" id="c-tok"></div>', { span: 7, legend: [["input", C.c], ["output", C.v]] }) +
      panel("Spend", '<div class="chart" id="c-cost"></div>', { span: 5, legend: [["est. cost", C.a]], note: "estimate from token counts" }) +
      panel("Top errors", topErrors, { span: 4, cls: o.top_errors.length ? "bad" : "" }) +
      panel("Slowest turns", slowest, { span: 4, note: "click to open the trace" }) +
      panel("Right now", `<div class="h4" style="margin-top:0">In flight</div><div class="hbars">${inflight}</div><div class="h4">Background work</div><div class="hbars">${bg}</div>`, { span: 4 }) +
      "</div>";

    barChart($("#c-traffic"), { xs, series: [{ name: "ok", color: C.c, data: okSeries }, { name: "failed", color: C.r, data: col("errors") }], height: 190 });
    lineChart($("#c-lat"), { xs, height: 190, yfmt: fmtMs, animate: ctx.first, connect: true, series: [{ name: "p50", color: C.g, data: col("p50"), area: true }, { name: "p95", color: C.a, data: col("p95") }] });
    lineChart($("#c-tok"), { xs, height: 180, stacked: true, animate: ctx.first, series: [{ name: "input", color: C.c, data: col("tokens_in") }, { name: "output", color: C.v, data: col("tokens_out") }] });
    lineChart($("#c-cost"), { xs, height: 180, yfmt: fmtUsd, animate: ctx.first, series: [{ name: "est. cost", color: C.a, data: col("cost"), area: true }] });
    $$("[data-trace]", root).forEach((r) => r.addEventListener("click", () => openTrace(r.dataset.trace)));
    setBadge("overview", crit);
  },
});

/* =====================================================================  TRACES */
const TraceState = { status: "", kind: "", source: "", q: "", min_ms: 0, offset: 0, limit: 50 };

PAGES.push({
  id: "traces", n: "02", title: "Traces", sub: "every unit of work", ranged: true,
  load: () => api("/obs/traces", { hours: App.hours, ...TraceState }),
  render(root, d) {
    const S = TraceState;
    const opt = (v, cur, label) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(label)}</option>`;
    const maxMs = Math.max(1, ...d.rows.map((r) => r.duration_ms));
    const rows = d.rows.map((r) => `<tr class="click" data-trace="${esc(r.id)}"><td class="dim" style="white-space:nowrap">${stamp(r.started_at)}</td><td>${kindChip(r.kind)}</td><td class="dim">${esc(r.source || "")}</td><td class="clip" style="max-width:340px">${esc(r.user_text || "(empty)")}${r.error ? `<div class="r clip" style="font-size:10px">${esc(r.error)}</div>` : ""}</td><td class="num ${latencyClass(r.duration_ms)}" style="white-space:nowrap"><span class="minibar"><b style="width:${(r.duration_ms / maxMs) * 100}%"></b></span>${fmtMs(r.duration_ms)}</td><td class="num dim">${fmtN(r.input_tokens)}/${fmtN(r.output_tokens)}</td><td class="num">${fmtUsd(r.cost_usd)}</td><td class="num dim">${r.llm_calls}·${r.tool_calls}</td><td class="num dim">${r.recalled}</td><td>${statusChip(r.status)}</td></tr>`).join("");
    const page = Math.floor(S.offset / S.limit) + 1, pages = Math.max(1, Math.ceil(d.total / S.limit));
    root.innerHTML =
      `<div class="bar"><input class="in" id="f-q" placeholder="search messages, replies, errors   ( / )" value="${esc(S.q)}">` +
      `<select class="in" id="f-status">${opt("", S.status, "any status")}${opt("ok", S.status, "ok")}${opt("error", S.status, "errors only")}</select>` +
      `<select class="in" id="f-kind">${opt("", S.kind, "any kind")}${opt("chat", S.kind, "chat")}${opt("extraction", S.kind, "extraction")}${opt("operator", S.kind, "operator")}</select>` +
      `<select class="in" id="f-source">${opt("", S.source, "any source")}${opt("telegram", S.source, "telegram")}${opt("web", S.source, "web")}${opt("api", S.source, "api")}${opt("background", S.source, "background")}</select>` +
      `<select class="in" id="f-min">${[0, 2000, 5000, 10000, 30000].map((v) => opt(String(v), String(S.min_ms), v ? `slower than ${fmtMs(v)}` : "any duration")).join("")}</select>` +
      `<span class="dim" style="margin-left:auto">${fmtInt(d.total)} traces</span></div>` +
      `<div class="panel" style="--i:0"><div class="tablewrap tall"><table><thead><tr><th>Time</th><th>Kind</th><th>Source</th><th>Input</th><th class="num">Duration</th><th class="num">Tokens in/out</th><th class="num">Cost</th><th class="num" title="model calls · tool calls">LLM·Tool</th><th class="num" title="memories recalled">Mem</th><th>Status</th></tr></thead><tbody>${rows || '<tr><td colspan="10"><div class="empty">NO TRACES MATCH</div></td></tr>'}</tbody></table></div>` +
      `<div class="pager"><button class="tool-btn" id="prev" ${S.offset ? "" : "disabled"}>‹ prev</button><span>page ${page} / ${pages}</span><button class="tool-btn" id="next" ${page >= pages ? "disabled" : ""}>next ›</button></div></div>`;
    const reload = () => App.reload();
    let timer;
    $("#f-q").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => { S.q = e.target.value; S.offset = 0; reload(); }, 300); });
    [["#f-status", "status"], ["#f-kind", "kind"], ["#f-source", "source"]].forEach(([sel, key]) => $(sel).addEventListener("change", (e) => { S[key] = e.target.value; S.offset = 0; reload(); }));
    $("#f-min").addEventListener("change", (e) => { S.min_ms = +e.target.value; S.offset = 0; reload(); });
    $("#prev").addEventListener("click", () => { S.offset = Math.max(0, S.offset - S.limit); reload(); });
    $("#next").addEventListener("click", () => { S.offset += S.limit; reload(); });
    $$("[data-trace]", root).forEach((r) => r.addEventListener("click", () => openTrace(r.dataset.trace)));
  },
});

/* ---- trace drawer: summary, waterfall, per-span detail */
async function openTrace(id) {
  openDrawer("Trace", '<div class="loading">loading</div>');
  history.replaceState(null, "", "#/traces/" + id); // a shareable link to this trace
  let d;
  try { d = await api("/obs/traces/" + id); } catch (e) { return setDrawer(`<div class="err-box">${esc(e.message)}</div>`); }
  const t = d.trace, spans = d.spans;
  const kv = [
    ["id", t.id], ["started", new Date(t.started_at).toLocaleString()], ["duration", fmtMs(t.duration_ms)], ["kind / source", `${t.kind} / ${t.source || "-"}`],
    ["status", t.status], ["tokens", `${fmtInt(t.input_tokens)} in · ${fmtInt(t.output_tokens)} out`], ["est. cost", fmtUsd(t.cost_usd)],
    ["model / tool calls", `${t.llm_calls} / ${t.tool_calls}`], ["memories recalled", t.recalled], ["conversation", t.conversation_id || "-"],
  ];
  if (t.error) kv.push(["error", t.error]);
  const meta = Object.entries(t.meta || {}).filter(([k]) => k !== "synthetic");
  if (meta.length) kv.push(["meta", meta.map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ")]);
  const synthetic = t.meta && t.meta.synthetic ? '<span class="chip a" style="margin-left:6px">synthetic demo data</span>' : "";
  setDrawer(
    `<div class="chips" style="margin-bottom:14px">${kindChip(t.kind)} ${statusChip(t.status)}${synthetic}</div>` +
      `<div class="kv">${kv.map(([k, v]) => `<div>${esc(k)}</div><div class="${k === "error" ? "r" : ""}">${esc(v)}</div>`).join("")}</div>` +
      (t.user_text ? `<div class="h4">${t.kind === "operator" ? "Goal" : "Input"}</div><div class="block">${esc(t.user_text)}</div>` : "") +
      (t.reply_text ? `<div class="h4">${t.kind === "operator" ? "Outcome" : t.kind === "extraction" ? "Saved" : "Reply"}</div><div class="block">${esc(t.reply_text)}</div>` : "") +
      `<div class="h4">Timeline · ${spans.length} spans</div>${spans.length ? waterfall(spans, t.duration_ms) : '<div class="empty">NO SPANS RECORDED</div>'}` +
      '<div id="span-detail"></div>',
    `Trace · ${fmtMs(t.duration_ms)}`
  );
  $$(".wf-row", $(".drawer")).forEach((row) => row.addEventListener("click", () => {
    const s = spans[+row.dataset.span], open = row.nextElementSibling && row.nextElementSibling.classList.contains("span-detail");
    $$(".span-detail", $(".drawer")).forEach((n) => n.remove());
    if (open) return;
    const box = document.createElement("div");
    box.className = "span-detail";
    box.innerHTML = spanDetail(s);
    row.after(box);
    const btn = $("[data-inspect]", box);
    if (btn) btn.addEventListener("click", () => openRetrieval({ ...s.attrs, started_at: s.started_at, duration_ms: s.duration_ms, plan: s.attrs.plan || {}, channels: s.attrs.channels || {}, results: s.attrs.results || [] }));
  }));
}

function spanDetail(s) {
  const a = s.attrs || {}, rows = [["started", new Date(s.started_at).toLocaleTimeString() + `  (+${fmtMs(s.offset_ms)})`], ["duration", fmtMs(s.duration_ms)]];
  if (s.model) rows.push(["model", s.model]);
  if (s.input_tokens || s.output_tokens) rows.push(["tokens", `${fmtInt(s.input_tokens)} in · ${fmtInt(s.output_tokens)} out · ${fmtUsd(s.cost_usd)}`]);
  if (a.ttft_ms) rows.push(["time to first token", fmtMs(a.ttft_ms)]);
  if (s.error) rows.push(["error", s.error]);
  let body = `<div class="kv" style="margin:0 0 8px">${rows.map(([k, v]) => `<div>${esc(k)}</div><div class="${k === "error" ? "r" : ""}">${esc(v)}</div>`).join("")}</div>`;
  const pre = (label, text) => (text ? `<div class="h4" style="margin:8px 0 5px">${esc(label)}</div><div class="block" style="max-height:170px">${esc(text)}</div>` : "");
  if (s.kind === "llm") body += pre("last input", a.last_input) + pre("output", a.output) + (a.requested_tools && a.requested_tools.length ? `<div class="chips">${a.requested_tools.map((t) => `<span class="chip a">→ ${esc(t)}</span>`).join("")}</div>` : "");
  else if (s.kind === "tool") body += pre("arguments", a.args) + pre("result", a.result);
  else if (s.kind === "retrieval") body += `<button class="tool-btn" data-inspect>inspect ranking ›</button>`;
  else if (s.kind === "step") body += pre("observation", a.observation) + pre("reasoning", a.reasoning) + pre("action", JSON.stringify(a.action)) + pre("result", a.result) + `<div class="chips"><span class="chip ${a.verdict === "allowed" || a.verdict === "approved" ? "g" : "r"}">${esc(a.verdict)}</span></div>`;
  else body += pre("details", JSON.stringify(Object.fromEntries(Object.entries(a).filter(([k]) => k !== "synthetic")), null, 1));
  return body;
}

/* =====================================================================  RETRIEVAL */
PAGES.push({
  id: "retrieval", n: "03", title: "Retrieval", sub: "recall → rank", ranged: true,
  load: () => api("/obs/retrieval", { hours: App.hours }),
  render(root, d, ctx) {
    const s = d.search, rc = d.recall, xs = d.series.map((p) => p.t);
    const skipRate = rc.requests ? rc.skipped / rc.requests : 0;
    const kpis = [
      kpi({ label: "Memory searches", value: s.searches, kind: "int", sub: `${rc.requests} recall requests`, spark: d.series.map((p) => p.searches), sparkColor: C.g, i: 0 }),
      kpi({ label: "Empty results", value: s.searches ? fmtPct(s.empty_rate) : "—", kind: "raw", tone: s.searches >= 10 && s.empty_rate > 0.8 ? "warn" : "", sub: `${s.empty} of ${s.searches} found nothing`, spark: d.series.map((p) => p.empty), sparkColor: C.a, i: 1 }),
      kpi({ label: "Router skipped", value: rc.requests ? fmtPct(skipRate) : "—", kind: "raw", sub: `${rc.skipped} messages needed no memory`, i: 2 }),
      kpi({ label: "Avg returned", value: s.avg_returned, kind: "raw", sub: `of ${s.avg_candidates} candidates fused`, i: 3 }),
      kpi({ label: "Search p50", value: s.p50, kind: "ms", sub: `p95 ${fmtMs(s.p95)}`, spark: d.series.map((p) => p.p50), sparkColor: C.c, i: 4 }),
      kpi({ label: "Reranked", value: s.reranked, kind: "int", sub: d.rerank_moves.count ? `moved ${d.rerank_moves.count} results · avg ${d.rerank_moves.avg_abs} places` : "RERANK=off or no calls", i: 5 }),
    ].join("");

    const chRows = d.channels.map((c) => `<tr><td><span class="chip ${CHANNEL_COLOR[c.channel] || ""}">${esc(c.channel)}</span></td><td class="num">${c.used}</td><td class="num">${c.avg_hits}</td><td class="num ${c.zero_rate > 0.5 ? "a" : "dim"}">${fmtPct(c.zero_rate)}</td><td class="num">${fmtMs(c.avg_ms)}</td><td class="num dim">${fmtMs(c.p95_ms)}</td><td class="num ${c.errors ? "r" : "dim"}">${c.errors}</td><td class="num">${c.results}</td><td class="num g">${c.unique}</td><td class="num">${c.rank1}</td><td class="num dim">${c.avg_rank || "-"}</td></tr>`).join("");
    const contrib = stackBar(d.channels.filter((c) => c.results).map((c) => ({ name: c.channel, value: c.results, color: CHANNEL_HEX[c.channel] || C.dim })));
    const planUse = hbars(Object.entries(d.plan_use).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ name: k.replace("_", " "), value: v, label: `${v} · ${fmtPct(v / Math.max(1, s.searches))}`, color: "c" })));
    const kinds = stackBar(Object.entries(d.kinds).map(([k, v], i) => ({ name: k, value: v, color: [C.c, C.v, C.g, C.a][i % 4] })));
    const imps = stackBar(Object.entries(d.importances).map(([k, v]) => ({ name: "imp " + k, value: v, color: ["#3d5366", "#4d7a96", C.c, C.g, C.a][Math.max(0, Math.min(4, (+k || 1) - 1))] })));
    const hot = d.hot_memories.length
      ? `<div class="tablewrap" style="max-height:300px"><table><tbody>${d.hot_memories.map((m) => `<tr><td class="num g" style="width:44px">${m.times}×</td><td class="wrap">${esc(m.content)}<div class="mute" style="font-size:10px">${esc(m.kind || "")} · importance ${m.importance ?? "?"}${m.is_active === false ? " · retired" : ""}</div></td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO RECALLS YET</div>';
    const latest = d.latest.length
      ? `<div class="tablewrap tall" style="max-height:440px"><table><thead><tr><th>Time</th><th>Query / plan</th><th>Channels (hits)</th><th class="num">Cand.</th><th class="num">Returned</th><th class="num">Time</th></tr></thead><tbody>${d.latest.map((r, i) => `<tr class="click" data-i="${i}"><td class="dim" style="white-space:nowrap">${stamp(r.started_at)}</td><td class="clip" style="max-width:300px">${esc(r.query)}</td><td><div class="chips">${Object.entries(r.channels).map(([k, v]) => `<span class="chip ${v.error ? "r" : CHANNEL_COLOR[k] || ""}" title="${v.error ? esc(v.error) : ""}">${CHANNEL_TAG[k] || k} ${v.error ? "!" : v.hits}</span>`).join("")}</div></td><td class="num dim">${r.candidates}</td><td class="num ${r.returned ? "" : "a"}">${r.returned}</td><td class="num dim">${fmtMs(r.duration_ms)}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO SEARCHES YET. TALK TO THE AGENT.</div>';

    root.innerHTML =
      `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("Channel health", `<div class="tablewrap" style="max-height:230px"><table><thead><tr><th>Channel</th><th class="num">Used</th><th class="num">Avg hits</th><th class="num" title="runs that found nothing">Zero-hit</th><th class="num">Avg time</th><th class="num">p95</th><th class="num">Errors</th><th class="num" title="results that reached the final list">In final</th><th class="num" title="results found by this channel only">Unique</th><th class="num" title="ranked first by this channel">Rank 1</th><th class="num">Avg rank</th></tr></thead><tbody>${chRows || '<tr><td colspan="11"><div class="empty">NO CHANNEL DATA</div></td></tr>'}</tbody></table></div>`, { span: 8, note: "K keyword · S semantic · G graph · T time" }) +
      panel("Who delivered the final results", contrib + '<div class="h4">What the router plans to use</div>' + planUse, { span: 4 }) +
      panel("Search latency", '<div class="chart" id="c-rlat"></div>', { span: 6, legend: [["p50", C.g], ["p95", C.a]] }) +
      panel("Searches and empty results", '<div class="chart" id="c-rcount"></div>', { span: 6, legend: [["with results", C.c], ["empty", C.a]] }) +
      panel("Fusion score of returned memories", '<div class="chart" id="c-score"></div>', { span: 5, note: "RRF × importance prior" }) +
      panel("What gets returned", '<div class="h4" style="margin-top:0">By kind</div>' + kinds + '<div class="h4">By importance</div>' + imps, { span: 3 }) +
      panel("Memory coverage", `<div style="font:700 40px/1 var(--display);color:#fff">${d.coverage.distinct_recalled}<small style="font:500 13px var(--mono);color:var(--dim)"> / ${d.coverage.active_memories} active</small></div><div class="dim" style="margin:6px 0 12px">distinct memories ever recalled in the last ${d.coverage.searches_sampled} searches. The rest are stored but never used.</div><div class="hbars"><div class="hb"><div class="name">coverage</div><div class="track"><div class="fill c" style="width:${d.coverage.active_memories ? (d.coverage.distinct_recalled / d.coverage.active_memories) * 100 : 0}%"></div></div><div class="v-n">${d.coverage.active_memories ? fmtPct(d.coverage.distinct_recalled / d.coverage.active_memories) : "-"}</div></div></div>`, { span: 4 }) +
      panel("Most recalled memories", hot, { span: 12 }) +
      panel("Latest searches", latest, { span: 12, note: "click a row to inspect the ranking" }) +
      "</div>";

    lineChart($("#c-rlat"), { xs, height: 170, yfmt: fmtMs, animate: ctx.first, connect: true, series: [{ name: "p50", color: C.g, data: d.series.map((p) => p.p50), area: true }, { name: "p95", color: C.a, data: d.series.map((p) => p.p95) }] });
    barChart($("#c-rcount"), { xs, height: 170, series: [{ name: "with results", color: C.c, data: d.series.map((p) => Math.max(0, p.searches - p.empty)) }, { name: "empty", color: C.a, data: d.series.map((p) => p.empty) }] });
    histogram($("#c-score"), d.score_hist, { color: C.g, height: 170 });
    $$("[data-i]", root).forEach((r) => r.addEventListener("click", () => openRetrieval(d.latest[+r.dataset.i])));
  },
});

/* ---- retrieval inspector: plan, per-channel results, final ranking */
function openRetrieval(r) {
  const plan = r.plan || {}, channels = r.channels || {}, results = r.results || [];
  const planChips = [];
  (plan.keywords || []).forEach((k) => planChips.push(`<span class="chip c">kw: ${esc(k)}</span>`));
  if (plan.semantic_query) planChips.push(`<span class="chip v">semantic: ${esc(plan.semantic_query)}</span>`);
  (plan.entities || []).forEach((k) => planChips.push(`<span class="chip g">entity: ${esc(k)}</span>`));
  (plan.relation_words || []).forEach((k) => planChips.push(`<span class="chip g">relation: ${esc(k)}</span>`));
  if (plan.start || plan.end) planChips.push(`<span class="chip a">time: ${esc((plan.start || "…").slice(0, 10))} → ${esc((plan.end || "…").slice(0, 10))}</span>`);
  const chRows = Object.entries(channels).map(([k, v]) => `<tr><td><span class="chip ${CHANNEL_COLOR[k] || ""}">${esc(k)}</span></td><td class="num">${v.hits}</td><td class="num">${fmtMs(v.ms)}</td><td class="num dim">${v.weight}</td><td class="${v.error ? "r" : "dim"}">${esc(v.error || "ok")}</td></tr>`).join("");
  const ranked = results.map((m) => `<div class="rank"><div class="n">${m.rank}</div><div><div class="tx">${esc(m.text)}</div><div class="chips">${Object.entries(m.found_by || {}).map(([k, pos]) => `<span class="chip ${CHANNEL_COLOR[k] || ""}">${CHANNEL_TAG[k] || k} #${pos}</span>`).join("")}<span class="chip">${esc(m.kind)}</span><span class="chip">imp ${m.importance}</span><span class="chip">rrf ${m.rrf}</span><span class="chip g">score ${m.score}</span>${m.moved_by_rerank ? `<span class="chip a">rerank ${m.moved_by_rerank > 0 ? "▲" : "▼"}${Math.abs(m.moved_by_rerank)}</span>` : ""}</div></div></div>`).join("");
  openDrawer("Retrieval", "", "Retrieval inspector");
  setDrawer(
    `<div class="kv"><div>time</div><div>${r.started_at ? new Date(r.started_at).toLocaleString() : "-"}</div><div>duration</div><div>${fmtMs(r.duration_ms)}</div><div>candidates fused</div><div>${r.candidates ?? "-"}</div><div>returned</div><div>${r.returned ?? results.length}</div><div>reranked</div><div>${r.reranked ? "yes" : "no"}</div></div>` +
      `<div class="h4">Plan from the memory router</div><div class="chips">${planChips.join("") || '<span class="dim">empty plan</span>'}</div>` +
      `<div class="h4">Channels</div><div class="tablewrap" style="max-height:200px"><table><thead><tr><th>Channel</th><th class="num">Hits</th><th class="num">Time</th><th class="num">Weight</th><th>Status</th></tr></thead><tbody>${chRows || '<tr><td colspan="5" class="dim">no channel ran</td></tr>'}</tbody></table></div>` +
      `<div class="h4">Final ranking handed to the model</div><div class="wf">${ranked || '<div class="empty">NOTHING RETURNED</div>'}</div>` +
      '<div class="mute" style="margin-top:10px">K/S/G/T #n = the memory\'s rank inside that channel before fusion. rrf = weighted reciprocal-rank fusion. score = rrf × importance prior.</div>',
    "Retrieval inspector"
  );
}

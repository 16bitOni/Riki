/* pages-b.js — Models, Tools, Memory, Operator. */
"use strict";

const shortModel = (m) => (m || "?").replace(/^(us|eu|apac)\./, "").replace("anthropic.", "").replace(/-\d{8}.*$/, "").replace(/-v\d.*$/, "");

/* =====================================================================  MODELS */
PAGES.push({
  id: "models", n: "04", title: "Models", sub: "tokens · latency · spend", ranged: true,
  load: () => api("/obs/llm", { hours: App.hours }),
  render(root, d, ctx) {
    const G = d.groups, S = d.series, xs = S.map((s) => s.t);
    const calls = sum(G.map((g) => g.calls)), errors = sum(G.map((g) => g.errors));
    const tin = sum(G.map((g) => g.tokens_in)), tout = sum(G.map((g) => g.tokens_out));
    const worstP95 = Math.max(0, ...G.map((g) => g.p95));
    const kpis = [
      kpi({ label: "Model calls", value: calls, kind: "int", sub: `${G.length} agents / purposes`, spark: S.map((s) => s.calls), sparkColor: C.c, i: 0 }),
      kpi({ label: "Tokens in", value: tin, kind: "n", sub: calls ? `${fmtN(tin / calls)} per call` : "", spark: S.map((s) => s.tokens_in), sparkColor: C.c, i: 1 }),
      kpi({ label: "Tokens out", value: tout, kind: "n", sub: tin ? `out/in ${(tout / tin).toFixed(2)}` : "", spark: S.map((s) => s.tokens_out), sparkColor: C.v, i: 2 }),
      kpi({ label: "Est. spend", value: d.total_cost, kind: "usd", sub: "models in range", spark: S.map((s) => s.cost), sparkColor: C.a, i: 3 }),
      kpi({ label: "Failure rate", value: calls ? fmtPct(errors / calls) : "—", kind: "raw", tone: calls && errors / calls > 0.05 ? "bad" : calls && errors ? "warn" : calls ? "good" : "", sub: `${errors} failed calls`, spark: S.map((s) => s.errors), sparkColor: C.r, i: 4 }),
      kpi({ label: "Worst p95", value: worstP95, kind: "ms", sub: "slowest agent's tail", tone: worstP95 > 20000 ? "warn" : "", i: 5 }),
    ].join("");
    const maxCost = Math.max(...G.map((g) => g.cost), 1e-9);
    const rows = G.map((g) => `<tr><td><span class="chip ${g.name === "supervisor" ? "g" : "c"}">${esc(g.name)}</span></td><td class="dim" title="${esc(g.model)}">${esc(shortModel(g.model))}</td><td class="num">${fmtInt(g.calls)}</td><td class="num dim">${fmtN(g.tokens_in)}</td><td class="num dim">${fmtN(g.tokens_out)}</td><td class="num" title="avg input / max input tokens">${fmtN(g.avg_in)}<span class="mute"> /${fmtN(g.max_in)}</span></td><td class="num">${fmtMs(g.p50)}</td><td class="num ${latencyClass(g.p95)}">${fmtMs(g.p95)}</td><td class="num dim">${fmtMs(g.p99)}</td><td class="num dim">${g.ttft ? fmtMs(g.ttft) : "-"}</td><td class="num ${g.errors ? "r" : "dim"}">${g.errors}</td><td class="num"><span class="minibar"><b style="width:${(g.cost / maxCost) * 100}%;background:${C.a}"></b></span>${fmtUsd(g.cost)}</td></tr>`).join("");
    const errs = d.errors.length
      ? `<div class="tablewrap" style="max-height:240px"><table><thead><tr><th>Error</th><th>Agent</th><th class="num">×</th><th>Last seen</th></tr></thead><tbody>${d.errors.map((e) => `<tr title="${esc(e.message)}"><td class="r">${esc(e.error_type)}<div class="mute clip" style="font-size:10px;max-width:260px">${esc(e.message)}</div></td><td>${esc(e.name)}</td><td class="num">${e.n}</td><td class="dim">${ago(e.last_seen)}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO MODEL ERRORS</div>';
    const emb = d.embeddings;
    const recent = `<div class="tablewrap" style="max-height:420px"><table><thead><tr><th>Time</th><th>Agent</th><th>Input → output</th><th class="num">Tokens</th><th class="num">TTFT</th><th class="num">Time</th><th class="num">Cost</th></tr></thead><tbody>${d.recent.map((r, i) => `<tr class="click" data-r="${i}"><td class="dim" style="white-space:nowrap">${stamp(r.started_at)}</td><td>${esc(r.name)}${r.status === "error" ? ' <span class="chip r">err</span>' : ""}</td><td class="clip" style="max-width:380px"><span class="dim">${esc(r.last_input)}</span>${r.output ? ` <span class="g">→</span> ${esc(r.output)}` : ""}</td><td class="num dim">${fmtN(r.input_tokens)}/${fmtN(r.output_tokens)}</td><td class="num dim">${r.ttft ? fmtMs(+r.ttft) : "-"}</td><td class="num ${latencyClass(r.duration_ms)}">${fmtMs(r.duration_ms)}</td><td class="num">${fmtUsd(r.cost_usd)}</td></tr>`).join("") || '<tr><td colspan="7"><div class="empty">NO CALLS</div></td></tr>'}</tbody></table></div>`;

    root.innerHTML =
      `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("By agent / purpose", `<div class="tablewrap" style="max-height:340px"><table><thead><tr><th>Agent</th><th>Model</th><th class="num">Calls</th><th class="num">In</th><th class="num">Out</th><th class="num">Avg in / max</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num" title="time to first token">TTFT</th><th class="num">Err</th><th class="num">Cost</th></tr></thead><tbody>${rows || '<tr><td colspan="12"><div class="empty">NO MODEL CALLS IN RANGE</div></td></tr>'}</tbody></table></div>`, { span: 12 }) +
      panel("Calls and failures", '<div class="chart" id="c-calls"></div>', { span: 6, legend: [["ok", C.c], ["failed", C.r]] }) +
      panel("p95 model latency", '<div class="chart" id="c-mlat"></div>', { span: 6 }) +
      panel("Latency distribution", '<div class="chart" id="c-hist"></div>', { span: 5, note: "all model calls" }) +
      panel("Spend, last 30 days", '<div class="chart" id="c-daily"></div>', { span: 4, legend: [["est. cost / day", C.a]] }) +
      panel("Embeddings", `<div class="hbars"><div class="hb"><div class="name">calls</div><div class="track"><div class="fill c" style="width:100%"></div></div><div class="v-n">${fmtInt(emb.calls)}</div></div><div class="hb"><div class="name">tokens</div><div class="track"><div class="fill v" style="width:100%"></div></div><div class="v-n">${fmtN(emb.tokens)}</div></div><div class="hb"><div class="name">p50 / p95</div><div class="track"><div class="fill g" style="width:100%"></div></div><div class="v-n">${fmtMs(emb.p50)} / ${fmtMs(emb.p95)}</div></div><div class="hb"><div class="name">failures</div><div class="track"><div class="fill ${emb.errors ? "r" : "g"}" style="width:100%"></div></div><div class="v-n">${emb.errors}</div></div><div class="hb"><div class="name">cost</div><div class="track"><div class="fill a" style="width:100%"></div></div><div class="v-n">${fmtUsd(emb.cost)}</div></div></div>`, { span: 3 }) +
      panel("Failures by type", errs, { span: 5, cls: d.errors.length ? "bad" : "" }) +
      panel("Latest model calls", recent, { span: 7, note: "click for the full call" }) +
      "</div>";

    barChart($("#c-calls"), { xs, height: 170, series: [{ name: "ok", color: C.c, data: S.map((s) => Math.max(0, s.calls - s.errors)) }, { name: "failed", color: C.r, data: S.map((s) => s.errors) }] });
    lineChart($("#c-mlat"), { xs, height: 170, yfmt: fmtMs, animate: ctx.first, connect: true, series: [{ name: "p95", color: C.a, data: S.map((s) => s.p95), area: true }] });
    histogram($("#c-hist"), d.histogram.map((h, i) => ({ ...h, color: i < 4 ? C.g : i < 7 ? C.a : C.r })), { height: 170 });
    barChart($("#c-daily"), { xs: d.daily.map((x) => x.t), daily: true, height: 170, yfmt: fmtUsd, series: [{ name: "est. cost", color: C.a, data: d.daily.map((x) => x.cost) }] });
    $$("[data-r]", root).forEach((r) => r.addEventListener("click", () => {
      const c = d.recent[+r.dataset.r];
      openDrawer("Model call", `<div class="kv"><div>agent</div><div>${esc(c.name)} ${c.purpose ? `(${esc(c.purpose)})` : ""}</div><div>model</div><div>${esc(c.model)}</div><div>time</div><div>${new Date(c.started_at).toLocaleString()}</div><div>duration</div><div>${fmtMs(c.duration_ms)}${c.ttft ? ` · first token ${fmtMs(+c.ttft)}` : ""}</div><div>tokens</div><div>${fmtInt(c.input_tokens)} in · ${fmtInt(c.output_tokens)} out</div><div>est. cost</div><div>${fmtUsd(c.cost_usd)}</div><div>status</div><div class="${c.status === "error" ? "r" : ""}">${esc(c.status)} ${esc(c.error || "")}</div></div><div class="h4">Last input</div><div class="block">${esc(c.last_input || "-")}</div><div class="h4">Output</div><div class="block">${esc(c.output || "-")}</div>${c.trace_id ? `<button class="tool-btn" id="to-trace">open whole trace ›</button>` : ""}`);
      const b = $("#to-trace");
      if (b) b.addEventListener("click", () => openTrace(c.trace_id));
    }));
  },
});

/* =====================================================================  TOOLS */
PAGES.push({
  id: "tools", n: "05", title: "Tools", sub: "calls and failures", ranged: true,
  load: () => api("/obs/tools", { hours: App.hours }),
  render(root, d, ctx) {
    const S = d.series, xs = S.map((s) => s.t), st = d.stats;
    const calls = sum(st.map((t) => t.calls)), errors = sum(st.map((t) => t.errors));
    const kpis = [
      kpi({ label: "Tool calls", value: calls, kind: "int", sub: `${st.length} distinct tools`, spark: S.map((s) => s.calls), sparkColor: C.a, i: 0 }),
      kpi({ label: "Success rate", value: calls ? fmtPct(1 - errors / calls) : "—", kind: "raw", tone: calls ? (errors / calls > 0.2 ? "bad" : errors ? "warn" : "good") : "", sub: `${errors} failures`, spark: S.map((s) => s.errors), sparkColor: C.r, i: 1 }),
      kpi({ label: "Slowest p95", value: Math.max(0, ...st.map((t) => t.p95)), kind: "ms", sub: (st.slice().sort((a, b) => b.p95 - a.p95)[0] || {}).name || "", i: 2 }),
      kpi({ label: "Busiest tool", value: (st[0] || {}).name || "—", kind: "raw", sub: st[0] ? `${st[0].calls} calls` : "", i: 3 }),
    ].join("");
    const rows = st.map((t) => {
      const ok = t.calls - t.errors, rate = t.calls ? ok / t.calls : 1;
      return `<tr><td><span class="chip a">${esc(t.name)}</span></td><td class="num">${t.calls}</td><td style="min-width:150px"><span class="minibar" style="width:90px"><b style="width:${rate * 100}%;background:${rate < 0.8 ? C.r : rate < 1 ? C.a : C.g}"></b></span>${fmtPct(rate)}</td><td class="num ${t.errors ? "r" : "dim"}">${t.errors}</td><td class="num">${fmtMs(t.p50)}</td><td class="num ${latencyClass(t.p95)}">${fmtMs(t.p95)}</td><td class="num dim">${fmtMs(t.max_ms)}</td><td class="dim">${ago(t.last_call)}</td><td class="${t.last_error_at ? "r" : "mute"}">${t.last_error_at ? ago(t.last_error_at) : "-"}</td></tr>`;
    }).join("");
    const recent = d.recent.map((r, i) => `<tr class="click" data-r="${i}"><td class="dim" style="white-space:nowrap">${stamp(r.started_at)}</td><td><span class="chip a">${esc(r.name)}</span></td><td class="dim">${esc(r.agent || "")}</td><td class="clip" style="max-width:260px">${esc(r.args || "")}</td><td class="clip ${r.status === "error" ? "r" : "dim"}" style="max-width:300px">${esc(r.status === "error" ? r.error : r.result || "")}</td><td class="num ${latencyClass(r.duration_ms)}">${fmtMs(r.duration_ms)}</td><td>${statusChip(r.status)}</td></tr>`).join("");
    root.innerHTML =
      `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("Per tool", `<div class="tablewrap" style="max-height:340px"><table><thead><tr><th>Tool</th><th class="num">Calls</th><th>Success</th><th class="num">Failed</th><th class="num">p50</th><th class="num">p95</th><th class="num">Max</th><th>Last call</th><th>Last failure</th></tr></thead><tbody>${rows || '<tr><td colspan="9"><div class="empty">NO TOOL CALLS IN RANGE</div></td></tr>'}</tbody></table></div>`, { span: 12 }) +
      panel("Calls over time", '<div class="chart" id="c-tools"></div>', { span: 12, legend: [["ok", C.a], ["failed", C.r]] }) +
      panel("Latest tool calls", `<div class="tablewrap tall" style="max-height:480px"><table><thead><tr><th>Time</th><th>Tool</th><th>Agent</th><th>Arguments</th><th>Result</th><th class="num">Time</th><th>Status</th></tr></thead><tbody>${recent || '<tr><td colspan="7"><div class="empty">NO CALLS</div></td></tr>'}</tbody></table></div>`, { span: 12, note: "click a row for the full arguments and result" }) +
      "</div>";
    barChart($("#c-tools"), { xs, height: 170, series: [{ name: "ok", color: C.a, data: S.map((s) => Math.max(0, s.calls - s.errors)) }, { name: "failed", color: C.r, data: S.map((s) => s.errors) }] });
    $$("[data-r]", root).forEach((r) => r.addEventListener("click", () => {
      const c = d.recent[+r.dataset.r];
      openDrawer("Tool call", `<div class="chips" style="margin-bottom:12px"><span class="chip a">${esc(c.name)}</span>${statusChip(c.status)}<span class="chip">${fmtMs(c.duration_ms)}</span></div><div class="h4">Arguments</div><div class="block">${esc(c.args || "-")}</div><div class="h4">${c.status === "error" ? "Error" : "Result"}</div><div class="block ${c.status === "error" ? "r" : ""}">${esc((c.status === "error" ? c.error : c.result) || "-")}</div>${c.trace_id ? `<button class="tool-btn" id="to-trace">open whole trace ›</button>` : ""}`);
      const b = $("#to-trace");
      if (b) b.addEventListener("click", () => openTrace(c.trace_id));
    }));
  },
});

/* =====================================================================  MEMORY */
PAGES.push({
  id: "memory", n: "06", title: "Memory", sub: "what it knows", ranged: false,
  load: () => api("/obs/memory"),
  render(root, d, ctx) {
    const T = d.totals, H = d.health, E = d.extraction, ch = d.chat;
    const kpis = [
      kpi({ label: "Active memories", value: T.active, kind: "int", sub: `${T.total} ever stored`, spark: d.growth.map((g) => g.total), sparkColor: C.g, i: 0 }),
      kpi({ label: "Superseded", value: T.superseded, kind: "int", sub: `${T.forgotten} forgotten by request`, i: 1 }),
      kpi({ label: "Embedded", value: `${T.embedded}/${T.wants_embedding}`, kind: "raw", tone: H.healthy ? "good" : "warn", sub: H.healthy ? "all searchable by meaning" : `${H.needs_embedding} need re-embedding`, i: 2 }),
      kpi({ label: "Unlinked", value: T.active ? fmtPct(T.unlinked / T.active) : "—", kind: "raw", sub: `${T.unlinked} memories with no entity (invisible to the graph)`, tone: T.active && T.unlinked / T.active > 0.6 ? "warn" : "", i: 3 }),
      kpi({ label: "Extraction runs", value: E.runs, kind: "int", sub: `last 7 days · ${fmtPct(E.empty_rate)} saved nothing`, spark: E.daily.map((x) => x.runs), sparkColor: C.v, i: 4 }),
      kpi({ label: "Extraction failures", value: E.errors, kind: "int", tone: E.errors ? "bad" : E.runs ? "good" : "", sub: `avg ${E.avg_saved} saved / run`, i: 5 }),
    ].join("");
    const kindColors = { fact: C.c, preference: C.v, event: C.a, episode: C.g };
    const kinds = stackBar(d.by_kind.map((k) => ({ name: k.kind, value: k.n, color: kindColors[k.kind] || C.dim })));
    const ents = hbars(d.entities.map((e) => ({ name: e.type, value: e.n, color: "v" })));
    const embedBy = Object.entries(H.embedded_by_model || {}).map(([m, n]) => ({ name: shortModel(m || "none"), value: n, color: m === H.current_model ? "g" : "r", label: `${n}${m === H.current_model ? " · current" : " · STALE"}` }));
    const top = d.top_entities.length
      ? `<div class="tablewrap" style="max-height:300px"><table><thead><tr><th>Entity</th><th>Type</th><th class="num">Memories</th><th class="num">Relations</th></tr></thead><tbody>${d.top_entities.map((e) => `<tr><td>${esc(e.name)}</td><td><span class="chip">${esc(e.type)}</span></td><td class="num">${e.memories}</td><td class="num dim">${e.relations}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO ENTITIES</div>';
    const recent = `<div class="tablewrap" style="max-height:380px"><table><thead><tr><th>Saved</th><th>Kind</th><th class="num">Imp</th><th>Memory</th><th>Flags</th></tr></thead><tbody>${d.recent.map((m) => `<tr><td class="dim" style="white-space:nowrap">${ago(m.created_at)}</td><td><span class="chip" style="color:${kindColors[m.kind] || C.dim}">${esc(m.kind)}</span></td><td class="num">${m.importance}</td><td class="wrap">${esc(m.content)}</td><td><div class="chips">${m.is_active ? "" : '<span class="chip r">retired</span>'}${m.embedded ? '<span class="chip v">vec</span>' : m.wants_embedding ? '<span class="chip a">needs vec</span>' : ""}${m.event_at ? `<span class="chip c">${esc(String(m.event_at).slice(0, 10))}</span>` : ""}</div></td></tr>`).join("")}</tbody></table></div>`;

    root.innerHTML =
      `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("Memory growth, last 30 days", '<div class="chart" id="c-growth"></div>', { span: 7, legend: [["active total", C.g], ["created / day", C.c]] }) +
      panel("Composition", '<div class="h4" style="margin-top:0">By kind</div>' + kinds + '<div class="h4">By importance</div><div class="chart" id="c-imp"></div>', { span: 5 }) +
      panel("Extraction pipeline", `<div class="chart" id="c-extract"></div><div class="stack-legend"><span>proposed / run <b style="color:#fff">${E.avg_proposed}</b></span><span>saved / run <b style="color:#fff">${E.avg_saved}</b></span><span>median time <b style="color:#fff">${fmtMs(E.p50)}</b></span><span>spend <b style="color:#fff">${fmtUsd(E.cost)}</b></span></div>`, { span: 6, legend: [["runs", C.v], ["memories saved", C.g], ["failed", C.r]], note: "background job after every turn" }) +
      panel("Index health", `<div class="hbars">${embedBy.length ? hbars(embedBy) : '<div class="dim">no vectors stored yet</div>'}</div><div class="kv" style="margin-top:14px"><div>current model</div><div>${esc(H.current_model)}</div><div>needing embedding</div><div class="${H.needs_embedding ? "a" : "g"}">${H.needs_embedding}</div><div>status</div><div class="${H.healthy ? "g" : "a"}">${H.healthy ? "healthy" : "run reembed"}</div></div>`, { span: 3, cls: H.healthy ? "" : "warn" }) +
      panel("Knowledge graph", `<div class="kv" style="margin-bottom:10px"><div>entities</div><div>${sum(d.entities.map((e) => e.n))}</div><div>relations</div><div>${d.relations.n}</div><div>predicates</div><div>${d.relations.predicates}</div></div>${ents}`, { span: 3 }) +
      panel("Most connected entities", top, { span: 5 }) +
      panel("Conversation store", `<div class="kv"><div>conversations</div><div>${fmtInt(ch.conversations)}</div><div>messages</div><div>${fmtInt(ch.messages)}</div><div>avg user message</div><div>${Math.round(ch.avg_user_len || 0)} chars</div><div>avg reply</div><div>${Math.round(ch.avg_reply_len || 0)} chars</div><div>open tasks</div><div>${ch.tasks_open}</div><div>done tasks</div><div>${ch.tasks_done}</div><div>notes</div><div>${ch.notes}</div><div>profile</div><div>${ch.profile_chars || 0} chars · updated ${ago(ch.profile_updated_at)}</div></div><div class="chart" id="c-msgs"></div>`, { span: 4 }) +
      panel("Most recent memories", recent, { span: 12 }) +
      "</div>";

    lineChart($("#c-growth"), { xs: d.growth.map((g) => g.t), daily: true, height: 200, animate: ctx.first, showEmpty: true, series: [{ name: "active total", color: C.g, data: d.growth.map((g) => g.total), area: true }, { name: "created / day", color: C.c, data: d.growth.map((g) => g.created) }] });
    histogram($("#c-imp"), d.by_importance.map((i) => ({ label: "imp " + i.importance, n: i.n, color: [C.mute, "#4d7a96", C.c, C.g, C.a][i.importance - 1] })), { height: 130 });
    barChart($("#c-extract"), { xs: E.daily.map((x) => x.t), daily: true, height: 160, series: [{ name: "runs", color: C.v, data: E.daily.map((x) => x.runs) }, { name: "memories saved", color: C.g, data: E.daily.map((x) => x.saved) }, { name: "failed", color: C.r, data: E.daily.map((x) => x.errors) }], stacked: false });
    barChart($("#c-msgs"), { xs: d.messages_daily.map((x) => x.t), daily: true, height: 120, series: [{ name: "messages", color: C.c, data: d.messages_daily.map((x) => x.n) }] });
  },
});

/* =====================================================================  OPERATOR */
PAGES.push({
  id: "operator", n: "07", title: "Operator", sub: "computer use", ranged: true,
  load: () => api("/obs/operator", { hours: Math.max(App.hours, 24) }),
  render(root, d) {
    const S = d.summary;
    const kpis = [
      kpi({ label: "Runs", value: S.runs, kind: "int", sub: d.enabled ? "operator enabled" : "OPERATOR_ENABLED is off", i: 0 }),
      kpi({ label: "Success rate", value: S.runs ? fmtPct(S.success_rate) : "—", kind: "raw", tone: S.runs ? (S.success_rate < 0.5 ? "bad" : S.success_rate < 0.8 ? "warn" : "good") : "", sub: `${S.ok} succeeded`, i: 1 }),
      kpi({ label: "Avg steps", value: S.avg_steps, kind: "raw", sub: "per run", i: 2 }),
      kpi({ label: "Median duration", value: S.p50, kind: "ms", i: 3 }),
      kpi({ label: "Est. spend", value: S.cost, kind: "usd", sub: "screenshots are expensive", i: 4 }),
      kpi({ label: "Live now", value: d.live.length, kind: "int", tone: d.live.length ? "warn" : "", sub: d.live.length ? "run in progress" : "idle", i: 5 }),
    ].join("");
    const verdictColor = { allowed: "g", approved: "c", declined: "a", denied: "r", invalid: "r" };
    const verdicts = hbars(d.verdicts.map((v) => ({ name: v.verdict, value: v.n, color: verdictColor[v.verdict] || "" })));
    const actions = hbars(d.actions.map((a) => ({ name: a.name, value: a.n, color: a.failed > a.n / 2 ? "r" : a.failed ? "a" : "c", label: `${a.n}${a.failed ? ` · ${a.failed} blocked` : ""}` })));
    const stopped = hbars(d.stopped.map((s) => ({ name: s.why, value: s.n, color: ["finished", "confirmed by the user"].includes(s.why) ? "g" : s.why === "cancelled" ? "a" : "r" })));
    const live = d.live.length ? d.live.map((r) => `<div class="alert warn"><span class="sev">live</span><span><span class="ttl">${esc(r.goal)}</span><span class="det">${esc(r.status)} · ${r.steps} steps${r.pending ? " · waiting: " + esc(r.pending) : ""}</span></span></div>`).join("") : "";
    const runs = d.runs.length
      ? `<div class="tablewrap" style="max-height:420px"><table><thead><tr><th>Started</th><th>Goal</th><th>Ended because</th><th class="num">Steps</th><th class="num">Duration</th><th class="num">Cost</th><th>Result</th></tr></thead><tbody>${d.runs.map((r) => `<tr class="click" data-trace="${esc(r.id)}"><td class="dim" style="white-space:nowrap">${stamp(r.started_at)}</td><td class="clip" style="max-width:300px">${esc(r.goal)}</td><td>${esc((r.meta || {}).stopped_because || (r.meta || {}).run_status || "-")}</td><td class="num">${(r.meta || {}).steps ?? "-"}</td><td class="num">${fmtMs(r.duration_ms)}</td><td class="num">${fmtUsd(r.cost_usd)}</td><td>${statusChip(r.status)}</td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">NO OPERATOR RUNS YET</div>';
    root.innerHTML =
      (live ? `<div class="alerts">${live}</div>` : "") + `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("Step verdicts", verdicts, { span: 4, note: "policy decisions per step" }) +
      panel("Actions used", actions, { span: 4 }) +
      panel("Why runs ended", stopped, { span: 4 }) +
      panel("Runs", runs, { span: 12, note: "click a run for its step-by-step timeline" }) +
      "</div>";
    $$("[data-trace]", root).forEach((r) => r.addEventListener("click", () => openTrace(r.dataset.trace)));
  },
});

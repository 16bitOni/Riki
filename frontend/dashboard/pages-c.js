/* pages-c.js — Database browser, Logs, System. */
"use strict";

/* =====================================================================  DATABASE */
const DbState = { table: "memories", q: "", offset: 0, limit: 50, sort: "", desc: true };

function dbCell(v, col) {
  if (v === null || v === undefined) return '<span class="mute">null</span>';
  if (typeof v === "boolean") return `<span class="chip ${v ? "g" : ""}">${v}</span>`;
  if (typeof v === "number") return `<span class="c">${v}</span>`;
  const s = String(v);
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/.test(s)) return `<span class="v" title="${esc(s)}">${esc(s.slice(0, 8))}…</span>`;
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) return `<span class="dim" title="${esc(s)}">${esc(s.slice(0, 19).replace("T", " "))}</span>`;
  return esc(s.length > 110 ? s.slice(0, 110) + "…" : s);
}

PAGES.push({
  id: "database", n: "08", title: "Database", sub: "raw tables, read-only", ranged: false,
  async load() {
    const [tables, rows] = await Promise.all([api("/obs/db"), api("/obs/db/" + DbState.table, { limit: DbState.limit, offset: DbState.offset, q: DbState.q, sort: DbState.sort, desc: DbState.desc })]);
    return { tables, rows };
  },
  render(root, { tables, rows }) {
    const S = DbState;
    const list = tables.map((t) => `<a data-t="${esc(t.name)}" class="${t.name === S.table ? "on" : ""}"><span>${esc(t.name)}</span><small>${fmtInt(t.rows)} · ${fmtBytes(t.bytes)}</small></a>`).join("");
    const head = rows.columns.map((c) => `<th class="sort" data-s="${esc(c)}">${esc(c)}${c === rows.sort ? (S.desc ? " ▾" : " ▴") : ""}</th>`).join("");
    const body = rows.rows.map((r, i) => `<tr class="click" data-r="${i}">${rows.columns.map((c) => `<td class="clip" style="max-width:300px">${dbCell(r[c], c)}</td>`).join("")}</tr>`).join("");
    const page = Math.floor(S.offset / S.limit) + 1, pages = Math.max(1, Math.ceil(rows.total / S.limit));
    root.innerHTML =
      `<div class="dbgrid"><div class="panel" style="--i:0;padding:0"><div class="ph" style="padding:14px 16px 0"><h2>Tables</h2></div><div class="tlist">${list}</div></div>` +
      `<div class="panel" style="--i:1"><div class="ph"><h2>${esc(S.table)}</h2><div class="note">${fmtInt(rows.total)} rows · read-only · click a row for the full record</div></div>` +
      `<div class="bar"><input class="in" id="db-q" placeholder="filter: matches any column" value="${esc(S.q)}" style="flex:1"></div>` +
      `<div class="tablewrap tall"><table><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${rows.columns.length}"><div class="empty">NO ROWS</div></td></tr>`}</tbody></table></div>` +
      `<div class="pager"><button class="tool-btn" id="prev" ${S.offset ? "" : "disabled"}>‹ prev</button><span>page ${page} / ${pages}</span><button class="tool-btn" id="next" ${page >= pages ? "disabled" : ""}>next ›</button></div></div></div>`;
    $$("[data-t]", root).forEach((a) => a.addEventListener("click", () => { Object.assign(S, { table: a.dataset.t, offset: 0, q: "", sort: "", desc: true }); App.reload(); }));
    $$("[data-s]", root).forEach((th) => th.addEventListener("click", () => { S.desc = S.sort === th.dataset.s ? !S.desc : true; S.sort = th.dataset.s; S.offset = 0; App.reload(); }));
    let timer;
    $("#db-q").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => { S.q = e.target.value; S.offset = 0; App.reload(); }, 350); });
    $("#prev").addEventListener("click", () => { S.offset = Math.max(0, S.offset - S.limit); App.reload(); });
    $("#next").addEventListener("click", () => { S.offset += S.limit; App.reload(); });
    $$("[data-r]", root).forEach((tr) => tr.addEventListener("click", () => {
      const r = rows.rows[+tr.dataset.r];
      openDrawer(S.table + " · row", `<div class="kv">${rows.columns.map((c) => `<div>${esc(c)}</div><div>${r[c] === null ? '<span class="mute">null</span>' : esc(typeof r[c] === "object" ? JSON.stringify(r[c], null, 1) : r[c])}</div>`).join("")}</div>`);
    }));
  },
});

/* =====================================================================  LOGS */
const LogState = { level: "INFO", q: "", follow: true };

function logLines(lines) {
  if (!lines.length) return '<div class="empty">NO LOG LINES MATCH</div>';
  return lines.map((l) => `<div class="ll ${esc(l.level)}"><span class="ts">${esc(l.ts.slice(11, 23))}</span><span class="lv">${esc(l.level.slice(0, 4))}</span><span class="lg" title="${esc(l.logger)}">${esc(l.logger)}</span><span class="msg">${esc(l.message)}</span>${l.exc ? `<div class="exc">${esc(l.exc)}</div>` : ""}</div>`).join("");
}

PAGES.push({
  id: "logs", n: "09", title: "Logs", sub: "live tail", ranged: false, poll: 2000,
  load: () => api("/obs/logs", { level: LogState.level, q: LogState.q, limit: 400 }),
  render(root, d) {
    const S = LogState, c = d.counts;
    root.innerHTML =
      `<div class="bar"><div class="seg" id="lv">${["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => `<button data-l="${l}" class="${S.level === l ? "on" : ""}">${l.toLowerCase()}</button>`).join("")}</div>` +
      `<input class="in" id="lq" placeholder="search messages, loggers, tracebacks" value="${esc(S.q)}" style="min-width:300px">` +
      `<button class="tool-btn ${S.follow ? "on" : ""}" id="follow">${S.follow ? "● following" : "○ paused"}</button>` +
      `<span class="dim" style="margin-left:auto"><span class="a">${c.WARNING} warn</span> · <span class="r">${c.ERROR + c.CRITICAL} error</span> in buffer <span class="mute">(last 4000 lines, in memory)</span></span></div>` +
      `<div class="logbox" id="logbox">${logLines(d.lines)}</div>`;
    const box = $("#logbox");
    box.scrollTop = box.scrollHeight;
    box.addEventListener("scroll", () => {
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      if (atBottom !== S.follow) { S.follow = atBottom; const b = $("#follow"); b.className = "tool-btn" + (atBottom ? " on" : ""); b.textContent = atBottom ? "● following" : "○ paused"; }
    });
    $("#follow").addEventListener("click", () => { S.follow = !S.follow; if (S.follow) box.scrollTop = box.scrollHeight; const b = $("#follow"); b.className = "tool-btn" + (S.follow ? " on" : ""); b.textContent = S.follow ? "● following" : "○ paused"; });
    $$("#lv button").forEach((b) => b.addEventListener("click", () => { S.level = b.dataset.l; App.reload(); }));
    let timer;
    $("#lq").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => { S.q = e.target.value; App.reload(); }, 300); });
  },
  /* live refresh: only the log box changes, so the search box keeps focus */
  refresh(root, d) {
    const box = $("#logbox", root);
    if (!box) return false;
    const keep = box.scrollTop;
    box.innerHTML = logLines(d.lines);
    box.scrollTop = LogState.follow ? box.scrollHeight : keep;
    return true;
  },
});

/* =====================================================================  SYSTEM */
PAGES.push({
  id: "system", n: "10", title: "System", sub: "process · database · config", ranged: false,
  async load() {
    const [s, tables] = await Promise.all([api("/obs/system"), api("/obs/db")]);
    return { s, tables };
  },
  render(root, { s, tables }, ctx) {
    const lag = s.loop_lag, tg = s.telegram, cfg = s.config;
    const kpis = [
      kpi({ label: "Uptime", value: fmtDur(s.uptime_s), kind: "raw", sub: `pid ${s.pid} · python ${s.python}`, i: 0 }),
      kpi({ label: "Process memory", value: s.process_mb ? s.process_mb : "—", unit: s.process_mb ? "MB" : "", kind: "raw", sub: "resident set", i: 1 }),
      kpi({ label: "DB round trip", value: s.db_ping_ms, unit: "ms", kind: "raw", tone: s.db_ping_ms > 250 ? "bad" : s.db_ping_ms > 80 ? "warn" : "good", sub: `pool ${s.pool.checked_out ?? "?"} busy / ${s.pool.size ?? "?"}`, i: 2 }),
      kpi({ label: "Database size", value: fmtBytes(s.database.bytes), kind: "raw", sub: `pgvector ${s.database.pgvector || "n/a"}`, i: 3 }),
      kpi({ label: "Event-loop lag p95", value: lag.samples ? lag.p95 : "—", unit: lag.samples ? "ms" : "", kind: "raw", tone: !lag.samples ? "" : lag.p95 > 250 ? "bad" : lag.p95 > 60 ? "warn" : "good", sub: lag.samples ? `max ${lag.max}ms · ${lag.samples} samples` : "sampler idle", spark: lag.series.map((p) => p.v), sparkColor: C.g, i: 4 }),
      kpi({ label: "In flight", value: s.in_flight, kind: "int", sub: `${s.operator_live} operator run(s)`, i: 5 }),
    ].join("");
    const tg_html = tg.running
      ? `<div class="kv"><div>state</div><div class="g">polling</div><div>last poll</div><div>${ago(tg.last_poll_at)}</div><div>messages handled</div><div>${tg.messages}</div><div>polls / errors</div><div>${tg.polls} / <span class="${tg.poll_errors ? "r" : ""}">${tg.poll_errors}</span></div><div>unauthorised</div><div class="${tg.unauthorised ? "a" : ""}">${tg.unauthorised}</div><div>in progress</div><div>${tg.in_progress}</div><div>allowed chats</div><div>${tg.allowed_chats}</div>${tg.last_error ? `<div>last error</div><div class="r">${esc(tg.last_error)}</div>` : ""}</div>`
      : `<div class="dim">${cfg.telegram_set ? "token set but the bot is not running in this process" : "TELEGRAM_BOT_TOKEN is not set"}</div>`;
    const flat = [
      ["main model", cfg.main_model], ["fast model", cfg.fast_model], ["embedding", `${cfg.embedding_model} · ${cfg.embedding_dim}d`], ["region", cfg.aws_region],
      ["rerank", cfg.rerank], ["recall limit", cfg.recall_limit], ["history messages", cfg.history_messages], ["min similarity", cfg.min_similarity],
      ["channel weights", Object.entries(cfg.weights).map(([k, v]) => `${k}=${v}`).join("  ")], ["graph hops", cfg.graph_max_hops], ["importance weight", cfg.importance_weight],
      ["dedup thresholds", `trigram ${cfg.dedup.trigram} · vector ${cfg.dedup.vector}`], ["storage", cfg.storage], ["timezone", cfg.timezone],
      ["operator", cfg.operator_enabled ? `enabled · max ${cfg.operator_max_steps} steps` : "disabled"],
      ["telemetry", `${cfg.telemetry.enabled ? "on" : "off"} · keep ${cfg.telemetry.retention_days}d · content ${cfg.telemetry.store_content ? "stored" : "NOT stored"}`],
      ["API key", cfg.api_key_set ? "set" : "NOT SET (open)"], ["web search key", cfg.tavily_set ? "set" : "missing"], ["Telegram token", cfg.telegram_set ? "set" : "missing"],
    ];
    const maxB = Math.max(...tables.map((t) => t.bytes), 1);
    root.innerHTML =
      `<div class="kpis">${kpis}</div><div class="grid">` +
      panel("Event-loop lag", lag.series.length ? '<div class="chart" id="c-lag"></div>' : '<div class="empty">NO SAMPLES YET</div>', { span: 7, note: "a 0.5 s timer's lateness: high = something blocks the server" }) +
      panel("Telegram bot", tg_html, { span: 5, cls: tg.running && tg.poll_errors ? "warn" : "" }) +
      panel("Table sizes", hbars(tables.slice().sort((a, b) => b.bytes - a.bytes).map((t) => ({ name: t.name, value: t.bytes, label: `${fmtBytes(t.bytes)} · ${fmtInt(t.rows)} rows`, color: "c" })), { max: maxB }), { span: 5 }) +
      panel("Log buffer", `<div class="kv"><div>warnings</div><div class="a">${s.log_counts.WARNING}</div><div>errors</div><div class="r">${s.log_counts.ERROR + s.log_counts.CRITICAL}</div></div><div class="dim">see the Logs tab for the lines</div><div class="h4">Database</div><div class="kv"><div>version</div><div>${esc(s.database.version)}</div><div>pgvector</div><div>${esc(s.database.pgvector || "n/a")}</div><div>platform</div><div>${esc(s.platform)}</div></div>`, { span: 3 }) +
      panel("Effective configuration", `<div class="kv" style="margin:0">${flat.map(([k, v]) => `<div>${esc(k)}</div><div class="${v === "NOT SET (open)" ? "a" : ""}">${esc(v)}</div>`).join("")}</div>`, { span: 4, note: "secrets are never shown" }) +
      "</div>";
    if (lag.series.length) lineChart($("#c-lag"), { xs: lag.series.map((p) => p.t), height: 170, yfmt: (v) => v.toFixed(0) + "ms", animate: ctx.first, series: [{ name: "lag", color: C.g, data: lag.series.map((p) => p.v), area: true }] });
  },
});

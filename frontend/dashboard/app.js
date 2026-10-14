/* app.js — router, API client, drawer, refresh loop, keyboard. Loaded last. */
"use strict";

const App = {
  hours: +(localStorage.getItem("obs_hours") || 24),
  refresh: +(localStorage.getItem("obs_refresh") ?? 15), // seconds, 0 = off
  key: sessionStorage.getItem("obs_key") || "",
  page: null, rendered: null, seq: 0, timer: null, painted: new Set(), cache: {},
  reload() { return show(App.page); },
};

/* ---------------------------------------------------------------- API */
async function api(path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined)).toString();
  const res = await fetch(path + (qs ? "?" + qs : ""), { headers: App.key ? { "X-API-Key": App.key } : {} });
  if (res.status === 401) { showLock(); throw new Error("API key required"); }
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 180)}`);
  setLive(true);
  return res.json();
}

function setLive(ok) {
  const dot = $("#live"), label = $("#live-label");
  dot.className = "pulse" + (ok ? "" : " bad");
  label.textContent = ok ? "live" : "offline";
}

function showLock(msg = "") {
  $("#lock").classList.add("on");
  $("#lock-err").textContent = msg;
  $("#lock-key").focus();
}

/* ---------------------------------------------------------------- drawer */
function openDrawer(title, html) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.add("on");
  $("#veil").classList.add("on");
}
function setDrawer(html, title) {
  if (title) $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
}
function closeDrawer() {
  $("#drawer").classList.remove("on");
  $("#veil").classList.remove("on");
  if (App.page && location.hash.split("/").length > 2) history.replaceState(null, "", "#/" + App.page);
}
const drawerOpen = () => $("#drawer").classList.contains("on");

/* ---------------------------------------------------------------- navigation badges */
function setBadge(id, n) {
  const a = $(`.nav a[data-p="${id}"]`);
  if (!a) return;
  const old = $(".badge", a);
  if (old) old.remove();
  if (n > 0) a.insertAdjacentHTML("beforeend", `<span class="badge">${n}</span>`);
}

async function pollAlerts() {
  try {
    const alerts = await api("/obs/alerts");
    const serious = alerts.filter((a) => a.severity !== "info").length;
    setBadge("overview", serious);
    if (alerts.some((a) => a.severity === "critical")) $("#live").className = "pulse bad";
  } catch (e) { /* the page load reports connection problems */ }
}

/* ---------------------------------------------------------------- showing a page */
async function show(id, { silent = false } = {}) {
  const page = PAGES.find((p) => p.id === id);
  if (!page) return;
  const root = $("#view"), token = ++App.seq;
  if (!silent) root.innerHTML = '<div class="loading">acquiring signal</div>';
  let data;
  try {
    data = await page.load();
  } catch (e) {
    if (token !== App.seq) return;
    setLive(false);
    if (!silent && !$("#lock").classList.contains("on")) root.innerHTML = `<div class="err-box">Could not load ${esc(page.title)}: ${esc(e.message)}</div>`;
    return;
  }
  if (token !== App.seq) return; // the user navigated elsewhere while this was loading
  if (silent && App.rendered === id && page.refresh && page.refresh(root, data) !== false) return;
  const keyed = id + ":" + (page.ranged ? App.hours : "");
  const first = !silent && !App.painted.has(keyed);
  const scroll = root.scrollTop;
  resetIdx();
  root.classList.toggle("no-anim", !first);
  try {
    page.render(root, data, { first });
  } catch (e) {
    console.error(e);
    root.innerHTML = `<div class="err-box">Render error in ${esc(page.title)}: ${esc(e.message)}</div>`;
    return;
  }
  if (first) countUp(root);
  App.cache[id] = data;
  App.painted.add(keyed);
  App.rendered = id;
  if (silent) root.scrollTop = scroll;
  else root.scrollTop = 0;
}

async function go(id, arg) {
  if (!PAGES.some((p) => p.id === id)) id = "overview";
  App.page = id;
  if (!arg && location.hash !== "#/" + id) history.replaceState(null, "", "#/" + id);
  const page = PAGES.find((p) => p.id === id);
  $("#title").innerHTML = `${esc(page.title)}<small>${esc(page.sub)}</small>`;
  $$(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.p === id));
  $("#range").style.visibility = page.ranged ? "visible" : "hidden";
  App.rendered = null;
  closeDrawer();
  await show(id);
  schedule();
  // deep links: #/traces/<trace id> opens that trace, #/retrieval/<n> opens the n-th latest search
  if (arg && id === "traces") openTrace(arg);
  if (arg !== undefined && id === "retrieval" && App.cache.retrieval && App.cache.retrieval.latest[+arg]) openRetrieval(App.cache.retrieval.latest[+arg]);
}

/* ---------------------------------------------------------------- refresh loop */
function schedule() {
  clearInterval(App.timer);
  const btn = $("#refresh");
  btn.textContent = App.refresh ? `⟳ every ${App.refresh}s` : "⟳ paused";
  btn.classList.toggle("on", App.refresh > 0);
  if (!App.refresh) return;
  const page = PAGES.find((p) => p.id === App.page);
  const every = Math.min(App.refresh * 1000, page.poll || Infinity);
  App.timer = setInterval(() => {
    const busy = document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName) && !page.refresh;
    if (document.hidden || drawerOpen() || busy) return;
    show(App.page, { silent: true });
  }, every);
}

/* ---------------------------------------------------------------- boot */
function build() {
  $("#nav").innerHTML = PAGES.map((p) => `<a data-p="${p.id}"><i>${p.n}</i>${esc(p.title)}</a>`).join("");
  $$(".nav a").forEach((a) => a.addEventListener("click", () => go(a.dataset.p)));
  $$("#range button").forEach((b) => {
    b.classList.toggle("on", +b.dataset.h === App.hours);
    b.addEventListener("click", () => {
      App.hours = +b.dataset.h;
      localStorage.setItem("obs_hours", App.hours);
      $$("#range button").forEach((x) => x.classList.toggle("on", x === b));
      show(App.page);
    });
  });
  $("#refresh").addEventListener("click", () => {
    const steps = [0, 5, 15, 30, 60];
    App.refresh = steps[(steps.indexOf(App.refresh) + 1) % steps.length];
    localStorage.setItem("obs_refresh", App.refresh);
    schedule();
  });
  $("#now").addEventListener("click", () => show(App.page));
  $("#veil").addEventListener("click", closeDrawer);
  $("#drawer-x").addEventListener("click", closeDrawer);
  $("#lock-go").addEventListener("click", () => {
    App.key = $("#lock-key").value.trim();
    sessionStorage.setItem("obs_key", App.key);
    $("#lock").classList.remove("on");
    show(App.page);
    pollAlerts();
  });
  $("#lock-key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#lock-go").click(); });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "Escape") return closeDrawer();
    if (e.key === "r") return show(App.page);
    if (e.key === "/") { const i = $(".in", $("#view")); if (i) { e.preventDefault(); i.focus(); } return; }
    const n = e.key === "0" ? 10 : +e.key;
    if (n >= 1 && n <= PAGES.length) go(PAGES[n - 1].id);
  });
  window.addEventListener("hashchange", () => { const [id, arg] = location.hash.replace("#/", "").split("/"); if (id !== App.page || arg) go(id, arg); });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (!drawerOpen()) show(App.page, { silent: true }); }, 250); });
  setInterval(() => { const d = new Date(); $("#clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }, 1000);
  setInterval(pollAlerts, 30000);
}

if (/[?&]still/.test(location.search)) document.documentElement.classList.add("still"); // for screenshots
build();
{ const [id, arg] = location.hash.replace("#/", "").split("/"); go(id || "overview", arg); }
pollAlerts();

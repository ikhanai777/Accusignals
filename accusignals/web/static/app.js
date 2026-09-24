"use strict";
// Accusignals dashboard: vanilla JS, no build step, no external requests.
// All market data comes from the local server, which reads the Binance API.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const REASONS = {
  htf_trend: "Higher-TF trend", ms_trend: "Market structure", ema_stack: "EMA 9/21 stack",
  vwap_side: "VWAP side", vwap_pullback: "VWAP pullback", rsi_reset: "RSI reset",
  stoch_cross: "Stoch-RSI cross", volume: "Volume spike", delta: "Aggressive delta",
  cvd_slope: "CVD slope", sweep: "Liquidity sweep", sr_bounce: "S/R bounce",
  candle: "Rejection candle", cvd_div: "CVD divergence", absorption: "Absorption",
};
const OUTCOME = {
  pending: ["Awaiting fill", "fresh"], open: ["In trade", "open"], tp1: ["TP1 hit · running", "open"],
  tp2: ["TP2 hit", "win"], "tp1+be": ["TP1 + breakeven", "win"], "tp1+time": ["TP1 + time exit", "win"],
  sl: ["Stopped out", "loss"], time: ["Time exit", ""], skipped: ["Not filled", ""], unknown: ["Tracking unavailable", ""],
};
const IV_MS = { "1m": 6e4, "3m": 18e4, "5m": 3e5, "15m": 9e5, "30m": 18e5, "1h": 36e5, "2h": 72e5, "4h": 144e5 };

const state = { status: null, signals: [], summary: null, prices: {}, filter: "all", view: "signals", open: new Set(), job: null };

// ---------- API ----------
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4000);
}

// ---------- formatting ----------
function dp(p) { return p >= 1000 ? 2 : p >= 10 ? 3 : p >= 1 ? 4 : p >= 0.01 ? 5 : 7; }
const fmt = (v, p) => (v == null || !isFinite(v) ? "–" : Number(v).toFixed(dp(p ?? v)));
const pct = (a, b) => ((a / b - 1) * 100);
const signed = (v, d = 2) => (v > 0 ? "+" : "") + Number(v).toFixed(d);
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const utc = (iso) => new Date(iso).toISOString().slice(5, 16).replace("T", " ");
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// ---------- views ----------
function show(view) {
  state.view = view;
  $$(".tab").forEach((b) => { const on = b.dataset.view === view; b.classList.toggle("active", on); on ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"); });
  $$(".view").forEach((v) => { const on = v.id === `view-${view}`; v.hidden = !on; v.classList.toggle("active", on); });
  if (view === "logs") loadLogs();
  if (view === "performance") renderPerformance();
  if (view === "settings") renderSettings();
  window.scrollTo({ top: 0 });
}
$$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// ---------- status ----------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const st = s.settings;
    $("#status-dot").className = "dot " + (s.last_error ? "err" : s.running ? "on" : "");
    $("#status-text").textContent = s.last_error ? "Binance error: retrying"
      : s.running ? `Scanning ${st.market} ${st.interval}/${st.htf_interval}` : "Scanner stopped";
    const btn = $("#btn-toggle");
    btn.textContent = s.running ? "Stop scanner" : "Start scanner";
    btn.classList.toggle("primary", !s.running); btn.classList.toggle("danger", s.running);
    const parts = [];
    if (s.running && s.next_scan_in_s != null) parts.push(`Next scan in ${s.next_scan_in_s}s`);
    if (s.last_scan) parts.push(`last scan ${ago(s.last_scan)} (${s.last_scan_signals} new)`);
    if (s.symbols && s.symbols.length) parts.push(`${s.symbols.length} pairs`);
    if (Math.abs(s.clock_offset_ms) > 1000) parts.push(`PC clock off by ${(s.clock_offset_ms / 1000).toFixed(1)}s (corrected)`);
    if (s.last_error) parts.push(`Error: ${s.last_error}`);
    $("#scan-meta").textContent = parts.length ? parts.join(" · ") : "Real Binance data. Start the scanner to receive signals at each candle close.";
  } catch (e) {
    $("#status-dot").className = "dot err";
    $("#status-text").textContent = "Dashboard offline";
  }
}

// ---------- signals ----------
async function loadSignals() {
  try {
    const d = await api("/api/signals?limit=60");
    state.signals = d.signals; state.summary = d.summary;
    renderSignals(); if (state.view === "performance") renderPerformance();
    loadPrices();
  } catch (e) { toast(`Signals: ${e.message}`); }
}
async function loadPrices() {
  const live = state.signals.filter(isLive);
  if (!live.length) return;
  const byMarket = {};
  live.forEach((s) => (byMarket[s.market] ||= new Set()).add(s.symbol));
  const cur = state.status && state.status.settings.market;
  if (!byMarket[cur]) return;
  try {
    const p = await api(`/api/prices?symbols=${[...byMarket[cur]].join(",")}`);
    Object.assign(state.prices, p);
    renderSignals();
  } catch (_) { /* transient */ }
}
function trackingStatus(s) { return (s.tracking && s.tracking.status) || "pending"; }
function isLive(s) { return ["pending", "open", "tp1"].includes(trackingStatus(s)); }
function isFresh(s) { return trackingStatus(s) === "pending" || (Date.now() - new Date(s.bar_close_time).getTime() < IV_MS[s.interval]); }

function renderKpis(el, items) {
  el.innerHTML = items.map(([k, v, sub]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${v}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div>`).join("");
}
function renderSignals() {
  const sm = state.summary || {};
  const today = state.signals.filter((s) => s.bar_close_time.slice(0, 10) === new Date().toISOString().slice(0, 10)).length;
  renderKpis($("#signal-kpis"), [
    ["Signals today (UTC)", today],
    ["Active", (sm.open ?? 0) + (sm.pending ?? 0), "awaiting fill or in trade"],
    ["Win rate", sm.win_rate_pct != null ? `${sm.win_rate_pct}%` : "–", `${sm.closed || 0} closed`],
    ["Avg R / signal", sm.avg_r != null ? signed(sm.avg_r) : "–", sm.total_r != null ? `total ${signed(sm.total_r)} R` : ""],
  ]);
  let list = state.signals;
  if (state.filter === "live") list = list.filter(isLive);
  if (state.filter === "closed") list = list.filter((s) => !isLive(s));
  const el = $("#signal-list");
  if (!list.length) {
    el.innerHTML = `<div class="empty">${state.signals.length ? "Nothing in this filter." : "No signals yet. Start the scanner. Setups are only posted when enough confluence lines up, so quiet periods are normal."}</div>`;
    return;
  }
  el.innerHTML = list.map(cardHTML).join("");
  $$(".card", el).forEach((c) => {
    c.addEventListener("click", () => toggleCard(c));
    c.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleCard(c); } });
  });
  $$(".card", el).forEach((c) => { if (state.open.has(c.dataset.key)) drawCardChart(c); });
}
function cardHTML(s) {
  const long = s.direction === 1, t = s.tracking || {}, st = trackingStatus(s);
  const [label, cls] = OUTCOME[st] || [st, ""];
  const fresh = st === "pending" && isFresh(s);
  const key = `${s.symbol}|${s.bar_close_time}|${s.direction}`;
  const px = state.prices[s.symbol];
  const risk = Math.abs(s.entry - s.sl);
  const lv = (k, v, sub) => `<div class="lv"><div class="k">${k}</div><div class="v">${fmt(v, s.entry)}</div>${sub ? `<div class="p">${sub}</div>` : ""}</div>`;
  let track = "";
  if (isLive(s) && px) {
    const lo = Math.min(s.sl, s.tp2), hi = Math.max(s.sl, s.tp2), span = hi - lo;
    const pos = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
    const e = pos(s.entry), r = ((px - (t.entry_fill || s.entry)) * s.direction) / risk;
    track = `<div class="track" aria-label="Price position between stop and target">
      <div class="rail ${long ? "" : "inv"}" style="--e:${e}%"></div>
      <div class="tick" style="left:${e}%" title="Entry"></div><div class="tick" style="left:${pos(s.tp1)}%" title="TP1"></div>
      <div class="now" style="left:${pos(px)}%"></div></div>
      <div class="track-label"><span>${long ? "SL" : "TP2"}</span><span>Now <b>${fmt(px, s.entry)}</b> (${signed(r)} R)</span><span>${long ? "TP2" : "SL"}</span></div>`;
  }
  const rTxt = t.r != null ? ` · <b class="${t.r > 0 ? "pos" : "neg"}">${signed(t.r)} R</b>` : "";
  return `<article class="card ${long ? "long" : "short"}" data-key="${esc(key)}" data-symbol="${esc(s.symbol)}" data-interval="${esc(s.interval)}" tabindex="0" aria-expanded="${state.open.has(key)}">
    <div class="card-top">
      <span class="sym">${esc(s.symbol)}</span>
      <span class="side">${long ? "▲ LONG" : "▼ SHORT"}</span>
      <span class="meta">${esc(s.market)} · ${esc(s.interval)}</span>
      <span class="state ${fresh ? "fresh" : cls}">${fresh ? "New · act near entry" : esc(label)}</span>
    </div>
    <div class="conf"><span>Confidence</span><span class="meter"><i style="width:${Math.min(100, s.confidence)}%"></i></span><b>${Math.round(s.confidence)}%</b></div>
    <div class="levels">
      ${lv("Entry", s.entry)}
      ${lv("Stop", s.sl, `${signed(pct(s.sl, s.entry))}%`)}
      ${lv(`TP1 · ${Math.round(s.tp1_fraction * 100)}%`, s.tp1, `${signed(pct(s.tp1, s.entry))}%`)}
      ${lv("TP2", s.tp2, `${signed(pct(s.tp2, s.entry))}%`)}
    </div>
    ${track}
    <div class="reasons">${(s.reasons || []).map((r) => `<span class="reason">${esc(REASONS[r] || r)}</span>`).join("")}</div>
    <div class="meta" style="margin-top:8px">${utc(s.bar_close_time)} UTC · ${ago(s.bar_close_time)}${rTxt}${s.footprint ? ` · footprint Δ ${signed(s.footprint.delta, 1)}` : ""}</div>
    ${state.open.has(key) ? `<div class="chart-wrap"><canvas height="200" role="img" aria-label="${esc(s.symbol)} price chart with entry, stop and targets"></canvas><div class="tip" hidden></div></div>` : ""}
  </article>`;
}
function toggleCard(c) {
  const k = c.dataset.key;
  state.open.has(k) ? state.open.delete(k) : state.open.add(k);
  renderSignals();
}
const candleCache = new Map();
async function drawCardChart(card) {
  const s = state.signals.find((x) => `${x.symbol}|${x.bar_close_time}|${x.direction}` === card.dataset.key);
  const canvas = $("canvas", card);
  if (!s || !canvas) return;
  try {
    const url = `/api/candles?symbol=${s.symbol}&interval=${s.interval}&limit=90`;
    const hit = candleCache.get(url);
    const d = hit && Date.now() - hit.t < 20000 ? hit.d : await api(url);
    candleCache.set(url, { t: Date.now(), d });
    candleChart(canvas, $(".tip", card), d.candles, s);
  } catch (e) { toast(`Chart: ${e.message}`); }
}

// ---------- charts (canvas) ----------
function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.height / (canvas._dpr || 1);
  canvas._dpr = dpr; canvas.width = w * dpr; canvas.height = h * dpr; canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}
function candleChart(canvas, tip, rows, s) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.font = "11px system-ui, sans-serif";
  const labelW = Math.max(...[s.entry, s.sl, s.tp1, s.tp2].map((v) => ctx.measureText(`T2 ${fmt(v, s.entry)}`).width));
  const pad = { l: 8, r: Math.ceil(labelW) + 10, t: 10, b: 18 };
  const lows = rows.map((r) => r[3]).concat([s.sl, s.tp2]), highs = rows.map((r) => r[2]).concat([s.sl, s.tp2]);
  const lo = Math.min(...lows), hi = Math.max(...highs), span = hi - lo || 1;
  const y = (v) => pad.t + (1 - (v - lo) / span) * (h - pad.t - pad.b);
  const bw = (w - pad.l - pad.r) / rows.length, x = (i) => pad.l + i * bw + bw / 2;
  const C = { long: cssVar("--long"), short: cssVar("--short"), grid: cssVar("--grid"), muted: cssVar("--muted"), text: cssVar("--text-2"), accent: cssVar("--accent") };
  ctx.clearRect(0, 0, w, h);
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle";
  const sigOpen = new Date(s.bar_close_time).getTime() - IV_MS[s.interval];
  const si = rows.findIndex((r) => new Date(r[0]).getTime() === sigOpen);
  if (si >= 0) { ctx.fillStyle = C.grid; ctx.fillRect(x(si) - bw / 2, pad.t, bw, h - pad.t - pad.b); }
  rows.forEach((r, i) => {
    const up = r[4] >= r[1]; ctx.strokeStyle = ctx.fillStyle = up ? C.long : C.short;
    ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x(i), y(r[2])); ctx.lineTo(x(i), y(r[3])); ctx.stroke();
    const top = y(Math.max(r[1], r[4])), bh = Math.max(1, Math.abs(y(r[1]) - y(r[4])));
    ctx.fillRect(x(i) - Math.max(1, bw * 0.35), top, Math.max(2, bw * 0.7), bh);
  });
  const level = (v, label, color, dash) => {
    ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(w - pad.r, y(v)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.fillText(`${label} ${fmt(v, s.entry)}`, w - pad.r + 4, y(v));
  };
  level(s.entry, "E", C.accent, [4, 3]); level(s.sl, "SL", C.short, []); level(s.tp1, "T1", C.long, [2, 3]); level(s.tp2, "T2", C.long, []);
  canvas.onpointermove = (ev) => {
    const b = canvas.getBoundingClientRect(), i = Math.floor((ev.clientX - b.left - pad.l) / bw);
    if (i < 0 || i >= rows.length) { tip.hidden = true; return; }
    const r = rows[i];
    tip.innerHTML = `${utc(r[0])} UTC<br>O ${fmt(r[1], s.entry)} · H ${fmt(r[2], s.entry)}<br>L ${fmt(r[3], s.entry)} · C ${fmt(r[4], s.entry)}`;
    tip.style.left = Math.min(Math.max(x(i), 80), w - 80) + "px"; tip.style.top = y(r[2]) + "px"; tip.hidden = false;
  };
  canvas.onpointerleave = () => (tip.hidden = true);
  canvas.onclick = (ev) => ev.stopPropagation();
}
function lineChart(canvas, tip, pts, { unit = "", base = null, label = "" } = {}) {
  // pts: [[isoTime, value], ...]  single series, with crosshair tooltip
  const { ctx, w, h } = setupCanvas(canvas);
  const pad = { l: 8, r: 56, t: 12, b: 20 };
  ctx.clearRect(0, 0, w, h);
  if (pts.length < 2) { ctx.fillStyle = cssVar("--muted"); ctx.font = "13px system-ui"; ctx.fillText("Not enough data yet", pad.l + 4, h / 2); return; }
  const vals = pts.map((p) => p[1]).concat(base != null ? [base] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals); if (hi === lo) { hi += 1; lo -= 1; }
  const m = (hi - lo) * 0.08; lo -= m; hi += m;
  const x = (i) => pad.l + (i / (pts.length - 1)) * (w - pad.l - pad.r), y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
  const grid = cssVar("--grid"), muted = cssVar("--muted"), line = cssVar("--line");
  ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle"; ctx.lineWidth = 1;
  for (let k = 0; k <= 3; k++) {
    const v = lo + ((hi - lo) * k) / 3; ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(w - pad.r, y(v)); ctx.stroke();
    ctx.fillStyle = muted; ctx.fillText(v.toFixed(Math.abs(hi - lo) < 10 ? 2 : 0) + unit, w - pad.r + 6, y(v));
  }
  if (base != null) { ctx.strokeStyle = muted; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(pad.l, y(base)); ctx.lineTo(w - pad.r, y(base)); ctx.stroke(); ctx.setLineDash([]); }
  ctx.strokeStyle = line; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1])))); ctx.stroke();
  const shortSpan = new Date(pts[pts.length - 1][0]) - new Date(pts[0][0]) < 2 * 864e5;
  const tlabel = (iso) => (shortSpan ? utc(iso).slice(6) : utc(iso).slice(0, 5));
  ctx.fillStyle = muted; ctx.fillText(tlabel(pts[0][0]), pad.l, h - 8);
  const endTxt = tlabel(pts[pts.length - 1][0]); ctx.fillText(endTxt, w - pad.r - ctx.measureText(endTxt).width, h - 8);
  canvas.onpointermove = (ev) => {
    const b = canvas.getBoundingClientRect();
    const i = Math.round(((ev.clientX - b.left - pad.l) / (w - pad.l - pad.r)) * (pts.length - 1));
    if (i < 0 || i >= pts.length) { tip.hidden = true; return; }
    tip.textContent = `${utc(pts[i][0])} UTC · ${label}${pts[i][1].toFixed(2)}${unit}`;
    tip.style.left = Math.min(Math.max(x(i), 90), w - 90) + "px"; tip.style.top = y(pts[i][1]) + "px"; tip.hidden = false;
  };
  canvas.onpointerleave = () => (tip.hidden = true);
}

// ---------- performance ----------
function renderPerformance() {
  const sm = state.summary || {};
  renderKpis($("#perf-kpis"), [
    ["Closed signals", sm.closed ?? 0, `${(sm.open ?? 0) + (sm.pending ?? 0)} still active`],
    ["Win rate", sm.win_rate_pct != null ? `${sm.win_rate_pct}%` : "–", `${sm.wins ?? 0} W / ${sm.losses ?? 0} L`],
    ["Avg R", sm.avg_r != null ? signed(sm.avg_r) : "–", "per closed signal, after fees"],
    ["Total R", sm.total_r != null ? signed(sm.total_r) : "–", "1R = your risk per trade"],
  ]);
  const closed = state.signals.filter((s) => s.tracking && s.tracking.r != null && s.tracking.exit_time)
    .sort((a, b) => new Date(a.tracking.exit_time) - new Date(b.tracking.exit_time));
  let cum = 0; const pts = closed.map((s) => [s.tracking.exit_time, (cum += s.tracking.r)]);
  if (pts.length) pts.unshift([closed[0].bar_close_time, 0]);
  lineChart($("#perf-chart"), $("#perf-tip"), pts, { unit: " R", base: 0 });
  $("#perf-table tbody").innerHTML = closed.slice().reverse().map((s) => {
    const r = s.tracking.r; const [label] = OUTCOME[s.tracking.status] || [s.tracking.status];
    return `<tr><td>${utc(s.bar_close_time)}</td><td>${esc(s.symbol)}</td><td>${s.direction === 1 ? "▲ Long" : "▼ Short"}</td><td>${esc(label)}</td><td class="num ${r > 0 ? "pos" : "neg"}">${signed(r)}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="meta">No closed signals yet.</td></tr>`;
}

// ---------- backtest jobs ----------
$("#job-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target), btn = $("button[type=submit]", ev.target);
  btn.disabled = true;
  try {
    const { id } = await api("/api/jobs", Object.fromEntries(f));
    state.job = id; pollJob();
  } catch (e) { toast(e.message); btn.disabled = false; }
});
async function pollJob() {
  if (!state.job) return;
  const j = await api(`/api/jobs/${state.job}`).catch((e) => ({ state: "error", progress: e.message }));
  const btn = $("#job-form button[type=submit]");
  const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
  $("#job-status").textContent = j.state === "running" ? `Running (${secs}s): ${j.progress}…` : j.state === "error" ? `Failed: ${j.progress}` : "";
  if (j.state === "running") { setTimeout(pollJob, 2000); return; }
  btn.disabled = false;
  if (j.state !== "done") return;
  const m = j.metrics || {}, oos = j.type === "walkforward";
  $("#job-status").textContent = `${oos ? "Walk-forward OUT-OF-SAMPLE" : "Backtest (in-sample)"} · ${j.symbols.join(", ")} · ${j.params.days} days · ${j.params.interval}`;
  const k = $("#job-kpis"); k.classList.add("six");
  renderKpis(k, m.trades ? [
    ["Trades", m.trades, `${m.trades_per_day}/day`],
    ["Win rate", `${m.win_rate_pct}%`, `L ${m.long_win_rate_pct ?? "–"}% · S ${m.short_win_rate_pct ?? "–"}%`],
    ["Profit factor", m.profit_factor, "> 1.3 is solid"],
    ["Avg R", signed(m.avg_r, 3), "per trade after fees"],
    ["Avg day", `${signed(m.avg_daily_return_pct)}%`, `${m.pct_green_days}% green days`],
    ["Max drawdown", `${m.max_drawdown_pct}%`, `total ${signed(m.total_return_pct)}%`],
  ] : [["Trades", 0, "No setups qualified. Try more days or pairs."]]);
  $("#job-chart-panel").hidden = !(j.equity && j.equity.length > 1);
  $("#job-chart-title").textContent = `Equity from 1,000 (${oos ? "out-of-sample" : "in-sample"})`;
  if (j.equity) lineChart($("#job-chart"), $("#job-tip"), j.equity, { base: 1000 });
}

// ---------- settings ----------
function renderSettings() {
  const s = state.status; if (!s) return;
  const f = $("#settings-form"), st = s.settings;
  for (const [k, v] of Object.entries(st)) {
    const el = f.elements[k]; if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = Array.isArray(v) ? v.join(",") : v;
  }
  const r = s.risk, rows = [
    ["Risk per trade", `${r.risk_per_trade_pct}% of equity`], ["Daily profit lock", `+${r.daily_profit_target_pct}%`],
    ["Daily loss limit", `-${r.daily_loss_limit_pct}%`], ["Pause after", `${r.max_consecutive_losses} losses in a row`],
    ["Max concurrent", r.max_concurrent], ["Max leverage", `${r.max_leverage}x`], ["Fee per side", `${(r.fee_rate * 100).toFixed(3)}%`],
    ["Targets", `TP1 ${s.strategy.tp1_r}R · TP2 ${s.strategy.tp2_r}R · max hold ${s.strategy.max_hold_bars} candles`],
  ];
  $("#risk-list").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, body = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  try { await api("/api/settings", body); toast("Saved"); await loadStatus(); renderSettings(); }
  catch (e) { toast(e.message); }
});

// ---------- logs ----------
async function loadLogs() {
  try { const d = await api("/api/logs?lines=300"); const el = $("#logs"); el.textContent = d.lines.join("\n") || "No log lines yet."; el.scrollTop = el.scrollHeight; }
  catch (e) { toast(e.message); }
}
$("#btn-logs").addEventListener("click", loadLogs);

// ---------- controls ----------
$("#btn-toggle").addEventListener("click", async (ev) => {
  const b = ev.currentTarget; b.disabled = true;
  try { await api(state.status && state.status.running ? "/api/scanner/stop" : "/api/scanner/start", {}); await loadStatus(); }
  catch (e) { toast(e.message); } finally { b.disabled = false; }
});
$("#btn-scan").addEventListener("click", async (ev) => {
  const b = ev.currentTarget; b.disabled = true; b.textContent = "Scanning…";
  try { const d = await api("/api/scan", {}); toast(d.signals.length ? `${d.signals.length} new signal(s)` : "No qualifying setups right now"); await loadSignals(); await loadStatus(); }
  catch (e) { toast(e.message); } finally { b.disabled = false; b.textContent = "Scan now"; }
});
$$(".chip-btn").forEach((b) => b.addEventListener("click", () => {
  state.filter = b.dataset.filter; $$(".chip-btn").forEach((x) => x.classList.toggle("active", x === b)); renderSignals();
}));

// ---------- notifications for new signals ----------
let seen = null;
function notifyNew() {
  const keys = new Set(state.signals.map((s) => `${s.symbol}|${s.bar_close_time}|${s.direction}`));
  if (seen) {
    const fresh = state.signals.filter((s) => !seen.has(`${s.symbol}|${s.bar_close_time}|${s.direction}`));
    fresh.forEach((s) => {
      const msg = `${s.symbol} ${s.side} @ ${fmt(s.entry)} · SL ${fmt(s.sl, s.entry)} · TP1 ${fmt(s.tp1, s.entry)}`;
      toast(`New signal: ${msg}`);
      if ("Notification" in window && Notification.permission === "granted") new Notification("Accusignals", { body: msg, tag: msg });
    });
  }
  seen = keys;
}
document.addEventListener("click", () => { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); }, { once: true });

// ---------- loop ----------
async function tick() { await loadStatus(); }
async function slowTick() { await loadSignals(); notifyNew(); }
tick(); slowTick();
setInterval(tick, 5000);
setInterval(slowTick, 15000);
setInterval(() => { if (state.view === "signals") loadPrices(); if (state.view === "logs") loadLogs(); }, 5000);
window.addEventListener("resize", () => { if (state.view === "performance") renderPerformance(); });

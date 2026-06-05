// Gold Trader control panel — SPA frontend
// All data is REAL: backed by /api/* endpoints reading repo files + live MT5 bridge.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const LOG_DETAIL_ITEMS = new Map();
let logDetailSeq = 0;

async function fetchJSON(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp.json();
}
async function postJSON(url, body) {
  return fetchJSON(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function fmtNum(v, d = 2) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return n.toFixed(d);
}
function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return (Number(v) * 100).toFixed(1) + "%";
}
function fmtR(v, d = 3) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  const s = (n >= 0 ? "+" : "") + n.toFixed(d);
  return s + "R";
}
function fmtTS(s) {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return String(s).replace("T", " ").replace(/\.\d+/, "");
  const pad = (n) => String(n).padStart(2, "0");
  const tz = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
    .formatToParts(d).find((part) => part.type === "timeZoneName")?.value || "local";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${tz}`;
}
function classOfR(v) {
  const n = Number(v);
  if (!isFinite(n)) return "muted";
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "muted";
}
function escapeHTML(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function logLineText(item) {
  return typeof item === "object" && item !== null ? String(item.line || "") : String(item || "");
}

function logDisplayText(item) {
  return logLineText(item)
    .replace(/^\[[^\]]+\]\s*/, "")
    .replace(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*/, "");
}

function extractLogTimestamp(text) {
  const s = String(text || "");
  const jsonMatch = s.match(/"ts"\s*:\s*"([^"]+)"/);
  const bracketMatch = s.match(/\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]/);
  const isoMatch = s.match(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)/);
  return (jsonMatch || bracketMatch || isoMatch || [null, null])[1];
}

function logTimestamp(item) {
  if (typeof item === "object" && item !== null && item.timestamp) return fmtTS(item.timestamp);
  const ts = extractLogTimestamp(logLineText(item));
  return ts ? fmtTS(ts) : fmtTS(new Date().toISOString());
}

function logTimeValue(item) {
  const raw = (typeof item === "object" && item !== null && item.timestamp) || extractLogTimestamp(logLineText(item));
  const n = raw ? Date.parse(raw) : Date.now();
  return Number.isFinite(n) ? n : Date.now();
}

function recentFirst(rows) {
  return [...(rows || [])].sort((a, b) => logTimeValue(b) - logTimeValue(a));
}

function logActionSummary(item) {
  const s = logLineText(item);
  const lo = s.toLowerCase();
  const priceMatch = s.match(/(?:entry|price|at|@)[=: ]+([0-9]{3,}(?:\.[0-9]+)?)/i);
  const signalEntry = s.match(/\b(long|buy|short|sell)\b.*?(?:entry|@|at)[=: ]+([0-9]{3,}(?:\.[0-9]+)?)/i);
  if (/active_families=/i.test(s)) return "SCANNING";
  if (/starting broker=paper/i.test(s)) return "SIGNAL ONLY";
  if (/starting broker=mt5_/i.test(s)) return "LIVE BROKER";
  if (/decision:\s*reject/i.test(s)) return "DO NOT ENTER";
  if (/decision:\s*accept/i.test(s)) return "ENTRY OK";
  if (/snapshot\s+\d+\s+@/i.test(s)) return "MARKET READ";
  if (/higher_timeframe_bias:\s*bullish/i.test(s)) return "HTF BULLISH";
  if (/higher_timeframe_bias:\s*bearish/i.test(s)) return "HTF BEARISH";
  if (/counter to higher-timeframe/i.test(s)) return "AGAINST HTF";
  if (/top candidate score/i.test(s)) return "SCORE";
  if (/already journaled|no-op/i.test(s)) return "OLD SIGNAL";
  if (/paper_equity:/i.test(s)) return "SIM STATE";
  if (/entry_candidates:\s*none/i.test(s) || /no active entry candidates|no current entry candidates/i.test(s)) return "NO ENTRY";
  if (/entry_candidates:/i.test(s)) return "SETUPS FOUND";
  if (/warnings:/i.test(s)) return "CAUTION";
  if (/decision:\s*hold/i.test(s)) return "HOLD";
  if (/order_filled/i.test(s) && /"side"\s*:\s*"sell"/i.test(s) && priceMatch) return `SELL FILLED @ ${priceMatch[1]}`;
  if (/order_filled/i.test(s) && /"side"\s*:\s*"buy"/i.test(s) && priceMatch) return `BUY FILLED @ ${priceMatch[1]}`;
  if (signalEntry) return /long|buy/i.test(signalEntry[1]) ? `BUY AT ${signalEntry[2]}` : `SELL AT ${signalEntry[2]}`;
  const levels = s.match(/short zone\s+([0-9.]+)-([0-9.]+),\s*breakdown\s+([0-9.]+),\s*long reclaim\s+([0-9.]+)/i);
  if (levels) return `SELL ZONE ${levels[1]}-${levels[2]} | SELL BELOW ${levels[3]} | BUY ABOVE ${levels[4]}`;
  const shortBreak = s.match(/short breakdown.*?below\s+([0-9.]+)/i);
  if (shortBreak) return `SELL BELOW ${shortBreak[1]}`;
  const longReclaim = s.match(/long reclaim.*?above\s+([0-9.]+)/i);
  if (longReclaim) return `BUY ABOVE ${longReclaim[1]}`;
  if (/short rejection/i.test(s)) return "WATCH SELL ZONE";
  if (/macro_long=allow_with_warning/i.test(s) && /macro_short=allow/i.test(s)) return "SHORT FAVORED / LONG CAUTION";
  if (/bridge_error|divergence_alert|kill_switch|failed|error/i.test(s)) return "CHECK RISK";
  if (/pending/i.test(lo)) return "CHECK LIMIT";
  return "";
}

function logMeaning(item) {
  const s = logLineText(item);
  if (/active_families=/i.test(s)) return "These are the strategy families included in this scan.";
  if (/starting broker=paper/i.test(s)) return "Safe mode: it is analysing and logging only, not placing live orders.";
  if (/starting broker=mt5_/i.test(s)) return "Live broker path is active. Watch risk and open-position state closely.";
  if (/decision:\s*reject/i.test(s)) return "The best setup is not strong enough for a trade signal right now.";
  if (/decision:\s*hold/i.test(s)) return "No clean entry. Wait for better confirmation.";
  if (/decision:\s*accept/i.test(s)) return "The system sees a valid entry candidate.";
  if (/entry_candidates:\s*none/i.test(s) || /no active entry candidates|no current entry candidates/i.test(s)) return "Nothing meets the scan rules at the latest candle.";
  if (/entry_candidates:/i.test(s)) return "Possible setups below. They still need the decision and warnings checked.";
  if (/counter to higher-timeframe bullish bias/i.test(s)) return "Short idea is fighting the larger bullish structure.";
  if (/counter to higher-timeframe bearish bias/i.test(s)) return "Long idea is fighting the larger bearish structure.";
  if (/higher_timeframe_bias:\s*bullish/i.test(s)) return "Bigger timeframes lean upward; shorts need extra confirmation.";
  if (/higher_timeframe_bias:\s*bearish/i.test(s)) return "Bigger timeframes lean downward; longs need extra confirmation.";
  if (/top candidate score/i.test(s)) return "Low scores are weak; higher scores are cleaner.";
  if (/warnings:/i.test(s)) return "Read these before acting. They explain what is wrong or risky.";
  if (/allow_with_warning/i.test(s)) return "Allowed, but with caution from the macro/risk filter.";
  if (/already journaled|no-op/i.test(s)) return "Not new. The signal was seen before, so no fresh action is needed.";
  if (/paper_equity:/i.test(s)) return "Paper/simulation account state for this cycle.";
  if (/broker:\s*paper/i.test(s)) return "The cycle completed against paper mode.";
  if (/done/i.test(s) && /agent-cycle/i.test(s)) return "Run completed successfully.";
  if (/error|failed|traceback|exception/i.test(s)) return "Something failed and needs attention.";
  if (/kill_switch|divergence_alert|equity_guard/i.test(s)) return "Risk guard message. Do not ignore this.";
  return "";
}

function logDetail(item) {
  const raw = logLineText(item);
  const text = logDisplayText(item);
  const action = logActionSummary(item) || "INFO";
  const meaning = logMeaning(item) || "This is an informational log row from the live system.";
  const lower = raw.toLowerCase();
  const make = (verdict, summary, why, next) => ({ action, verdict, summary, meaning: why || meaning, next, raw, text });
  const candidate = raw.match(/\b([a-z_]+)\s+(\d+m|\d+h)?\s*(long|short)\s+score=([0-9.]+).*?entry=([0-9.]+)\s+stop=([0-9.]+)\s+target=([0-9.]+)/i);
  if (candidate) {
    const side = candidate[3].toUpperCase();
    const isCounterHtf = /counter to higher-timeframe/i.test(raw);
    const verdict = isCounterHtf ? `WAIT / DO NOT CHASE ${side}` : `${side} SETUP ONLY`;
    const why = `${side} candidate from ${candidate[1]}. Entry ${candidate[5]}, stop ${candidate[6]}, target ${candidate[7]}, score ${candidate[4]}. ${isCounterHtf ? "Problem: it is fighting the higher-timeframe bias." : "No higher-timeframe conflict was highlighted on this row."}`;
    const next = isCounterHtf ? "Do not enter just because this level exists. Wait for stronger confirmation, or for the higher-timeframe bias to stop fighting the trade." : "Only consider this if the overall decision row says accept and the warnings do not contradict the trade.";
    return make(verdict, `${side} trade level found.`, why, next);
  }
  if (/decision:\s*reject/i.test(raw)) return make("DO NOT ENTER", "The agent rejected the setup.", "The scan found something, but the final decision says it is not good enough to trade now.", "Stand aside. Wait for an accept decision, better alignment, or a cleaner retest.");
  if (/decision:\s*hold/i.test(raw)) return make("NO TRADE", "The agent is holding.", "There is no clean setup at the latest candle.", "Keep watching. Do not force an entry from this row.");
  if (/decision:\s*accept/i.test(raw)) return make("TRADE IDEA VALID", "The agent accepted a setup.", "The scan found a setup that passed its current filters.", "Review entry, stop, target, warnings, and your own manual read before acting.");
  if (/active_families=/i.test(raw)) return make("SCAN INFO", "These are the strategies being checked.", "This tells you what the agent included in the run. It is not a buy or sell signal.", "Use it only to confirm the scan covered the expected strategies.");
  if (/starting broker=paper/i.test(raw)) return make("SIGNALS ONLY", "The run is safe and paper-only.", "The agent is analysing and logging, but it should not place live orders.", "You remain the one executing trades manually.");
  if (/starting broker=mt5_/i.test(raw)) return make("LIVE BROKER ACTIVE", "The run can talk to the live broker.", "This is more sensitive because live order routing is available.", "Check auto-trade state, open positions, and risk controls immediately.");
  if (/entry_candidates:/i.test(raw)) return make("SETUPS LISTED - NOT A VERDICT", "This section lists possible setups.", "A candidate is only a possibility. The decision and warning rows decide whether it is usable.", "Do not enter from this header. Open the candidate rows and the final decision row.");
  if (/warnings:/i.test(raw)) return make("CAUTION", "Warning section starts here.", "These are the problems with the current setup, such as weak score, wrong bias, or mixed regime.", "If warnings contradict the trade, reduce confidence or wait.");
  if (/counter to higher-timeframe/i.test(raw)) return make("AGAINST THE BIGGER MOVE", "The trade idea fights higher-timeframe bias.", "Counter-bias trades can work, but they need stronger confirmation and usually fail faster if wrong.", "Avoid chasing. Watch invalidation and require a clean break/retest.");
  if (/already journaled|no-op/i.test(raw)) return make("OLD SIGNAL", "This is not new.", "The system already recorded this signal earlier.", "Do not treat it as a fresh alert.");
  if (/kill_switch|divergence_alert|equity_guard/i.test(lower)) return make("RISK WARNING", "Risk controls are speaking.", "This can mean account divergence, drawdown, or a protection rule.", "Pause. Inspect account/risk before trusting new entries.");
  if (/done/i.test(raw) && /agent-cycle/i.test(raw)) return make("RUN COMPLETE", "The cycle finished successfully.", "The findings shown above are complete for this run.", "Use the latest decision and warnings as the current system read.");
  if (/error|failed|traceback|exception/i.test(lower)) return make("FIX REQUIRED", "Something failed.", "The agent output may be incomplete or unreliable.", "Open the raw log and fix the failure before relying on the signal.");
  return make("CONTEXT ONLY", text.slice(0, 160) || "Informational log row.", meaning, "Use this as supporting context. The actionable rows are decisions, candidates, warnings, and risk messages.");
}

function logDetailAttrs(item) {
  const id = `log-detail-${++logDetailSeq}`;
  LOG_DETAIL_ITEMS.set(id, item);
  return `data-log-detail-id="${id}" role="button" tabindex="0" title="Open log explanation"`;
}

function classifyLogLine(line) {
  const s = logLineText(line);
  const lo = s.toLowerCase();
  const classes = ["log-line"];
  const killOk = /kill_switch["']?\s*[=:]\s*false|kill:\s*ok/i.test(s);
  const journaledNoop = /already journaled|no-op/i.test(s);
  if (!journaledNoop && /alert|>>>|setup|entry~|entry≈|entry:|entry_candidates|signal|decision: accept|position_opened/i.test(s)) classes.push("log-entry");
  if (/error|failed|traceback|exception|trip|block/i.test(s) || (/kill_switch/i.test(s) && !killOk)) classes.push("log-error");
  else if (/warn|warning|caution|allow_with_warning|hold|insufficient/i.test(s)) classes.push("log-warn");
  else if (/allow|online|accepted|started|done|pass|ok|200 -/i.test(s)) classes.push("log-ok");
  if (/short/i.test(s)) classes.push("log-short");
  if (/long/i.test(s)) classes.push("log-long");
  if (/decision:\s*hold/i.test(s)) classes.push("log-hold");
  let badge = "INFO";
  if (classes.includes("log-error")) badge = "RISK";
  else if (classes.includes("log-entry")) badge = "ENTRY";
  else if (classes.includes("log-warn")) badge = "WAIT";
  else if (classes.includes("log-ok")) badge = "OK";
  return { classes: classes.join(" "), badge, text: s };
}

function renderLogLines(lines) {
  const arr = recentFirst(lines || []);
  if (!arr.length) return "<div class='muted'>no log lines</div>";
  return arr.map((line) => {
    const c = classifyLogLine(line);
    const action = logActionSummary(line);
    return `<div class="${c.classes}" ${logDetailAttrs(line)}><span class="log-time">${escapeHTML(logTimestamp(line))}</span><span class="log-action">${escapeHTML(action)}</span><span class="log-badge">${c.badge}</span><span class="log-text">${escapeHTML(logDisplayText(line))}</span></div>`;
  }).join("");
}

function renderCompactLogLines(lines, maxRows = 10) {
  const arr = recentFirst(lines || []).slice(0, maxRows);
  if (!arr.length) return "<div class='muted'>no log lines</div>";
  return arr.map((line) => {
    const c = classifyLogLine(line);
    const action = logActionSummary(line) || c.badge;
    const source = typeof line === "object" && line !== null && line.source ? line.source : "log";
    const text = logDisplayText(line).slice(0, 120);
    return `<div class="${c.classes} compact-log-line" ${logDetailAttrs(line)}><span class="log-time">${escapeHTML(logTimestamp(line))}</span><span class="log-action">${escapeHTML(action)}</span><span class="log-source">${escapeHTML(source)}</span><span class="log-text">${escapeHTML(text)}</span></div>`;
  }).join("");
}

function renderCycleLogLines(lines) {
  if (!lines || !lines.length) return "<div class='muted'>Waiting for process output...</div>";
  return lines.map((line) => {
    const c = classifyLogLine(line);
    const action = logActionSummary(line) || c.badge;
    const text = logDisplayText(line);
    const meaning = logMeaning(line);
    return `<div class="${c.classes} cycle-log-line" ${logDetailAttrs(line)}><span class="log-time">${escapeHTML(logTimestamp(line))}</span><span class="log-action">${escapeHTML(action)}</span><span class="log-badge">${c.badge}</span><span class="log-text">${escapeHTML(text)}${meaning ? `<small>${escapeHTML(meaning)}</small>` : ""}</span></div>`;
  }).join("");
}

function openLogDetail(item) {
  const detail = logDetail(item);
  const c = classifyLogLine(item);
  const modal = $("#log-detail-modal");
  if (!modal) return;
  $("#log-detail-title").textContent = detail.action || c.badge || "Log detail";
  $("#log-detail-meta").textContent = `${logTimestamp(item)} · ${c.badge}`;
  $("#log-detail-body").innerHTML = `
    <section class="log-detail-verdict"><strong>Direct verdict</strong><p>${escapeHTML(detail.verdict || detail.action || "CONTEXT ONLY")}</p></section>
    <section><strong>Simple explanation</strong><p>${escapeHTML(detail.meaning)}</p></section>
    <section><strong>What to do now</strong><p>${escapeHTML(detail.next)}</p></section>
    <section><strong>Readable line</strong><pre>${escapeHTML(detail.text)}</pre></section>
    <section><strong>Raw log</strong><pre>${escapeHTML(detail.raw)}</pre></section>
  `;
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
}

function closeLogDetail() {
  const modal = $("#log-detail-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function entryPointLines(lines) {
  return (lines || []).filter((line) => {
    const text = logLineText(line);
    return !/already journaled|no-op/i.test(text) && /alert|>>>|setup|entry~|entry≈|decision: accept|position_opened|entry_candidates:/i.test(text);
  });
}

function renderLogNotes(notes) {
  const arr = notes || [];
  if (!arr.length) return [];
  return arr.map((note) => {
    if (typeof note === "object" && note !== null) {
      return { ...note, line: `${note.source ? `[${note.source}] ` : ""}${note.line || ""}` };
    }
    return note;
  }).slice(0, 24);
}

// ---------- Tabs ----------------------------------------------------------

const TAB_LOADERS = {
  live: loadLive,
  dashboard: loadDashboard,
  charts: initChartsTab,
  bridge: loadBridge,
  lab: loadLab,
  miner: loadMiner,
  macro: loadMacro,
  journal: loadJournal,
  performance: loadPerformance,
  risk: loadRisk,
  replay: loadReplay,
  logs: loadLogs,
  controls: loadControls,
};

const TAB_INIT = new Set();

function switchTab(name) {
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${name}`));
  const fn = TAB_LOADERS[name];
  if (fn) fn().catch((e) => toast(`${name}: ${e.message}`, "err"));
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".nav button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#btn-refresh").addEventListener("click", () => {
    if (currentTab() === "live") {
      Promise.all([refreshLiveBanner(), refreshLiveChart(), refreshLiveSide(), refreshLiveTracker(), refreshIFVGAssistant(true)]);
    } else {
      switchTab(currentTab());
    }
  });
  $("#btn-run-cycle").addEventListener("click", runAgentCycle);
  document.addEventListener("click", (event) => {
    const row = event.target.closest("[data-log-detail-id]");
    if (row) {
      const item = LOG_DETAIL_ITEMS.get(row.dataset.logDetailId);
      if (item) openLogDetail(item);
      return;
    }
    if (event.target.id === "log-detail-modal") closeLogDetail();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLogDetail();
    if ((event.key === "Enter" || event.key === " ") && event.target.matches?.("[data-log-detail-id]")) {
      event.preventDefault();
      const item = LOG_DETAIL_ITEMS.get(event.target.dataset.logDetailId);
      if (item) openLogDetail(item);
    }
  });
  const closeDetail = $("#btn-close-log-detail");
  if (closeDetail) closeDetail.addEventListener("click", closeLogDetail);
  setInterval(updateClock, 1000);
  updateClock();
  bootstrap();
});

function currentTab() {
  const a = $(".nav button.active");
  return a ? a.dataset.tab : "live";
}

function updateClock() {
  $("#clock").textContent = new Date().toISOString().slice(0, 19).replace("T", " ") + "Z";
}

// ---------- Status pills (always-on) -------------------------------------

async function refreshPills() {
  try {
    const b = await fetchJSON("/api/bridge/status");
    setBrokerMode(!!b.online);
    // Use explicit broker label for local authoritative UI
    $("#pill-bridge").textContent = "Broker: MT5 bridge local · cTrader pending";
    $("#pill-bridge").className = "pill " + (b.online ? "ok" : "warn");
    if (!b.online) return;
  } catch (e) {
    setBrokerMode(false);
    $("#pill-bridge").textContent = "Broker: MT5 bridge local · cTrader pending";
    $("#pill-bridge").className = "pill warn";
    return;
  }
  try {
    const s = await fetchJSON("/api/summary");
    const eq = s.states && s.states[0] ? s.states[0].paper_equity : null;
    const totalTrades = (s.states || []).reduce((a, x) => a + (x.total_trades || 0), 0);
    const filterMode = s.config && s.config.macro_filter_mode;
    $("#pill-equity").textContent = `equity: ${fmtNum(eq, 2)}`;
    $("#pill-trades").textContent = `trades: ${totalTrades}`;
    $("#pill-filter").textContent = `filter: ${filterMode || "?"}`;
    $("#pill-filter").className = "pill " + (filterMode === "hard" ? "warn" : filterMode === "soft" ? "ok" : "");
    const killed = (s.states || []).some((x) => x.open_position && x.open_position.kill_switch);
    $("#pill-kill").textContent = killed ? "kill: ARMED" : "kill: ok";
    $("#pill-kill").className = "pill " + (killed ? "bad" : "ok");
  } catch (e) { /* keep last */ }
}

async function runAgentCycle() {
  try {
    showCycleJob({ status: "queued", id: "starting", findings: ["Starting agent-cycle..."], output: "" });
    const r = await postJSON("/api/run-cycle");
    toast(`agent-cycle started (${r.job_id})`);
    pollJob(r.job_id, (j) => {
      showCycleJob(j);
      toast(`agent-cycle ${j.status}`, j.status === "done" ? "ok" : "err");
      loadDashboard().catch(() => {});
    }, showCycleJob);
  } catch (e) { toast(`run-cycle: ${e.message}`, "err"); }
}

function cycleStatusClass(status) {
  if (status === "done") return "ok";
  if (status === "failed" || status === "error" || status === "timeout") return "bad";
  return "warn";
}

function showCycleJob(job) {
  const panel = $("#cycle-run-panel");
  if (!panel) return;
  if (!job) job = { status: "idle" };
  const status = job.status || "idle";
  const active = status === "queued" || status === "running" || status === "starting";
  if (!active) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const statusEl = $("#cycle-run-status");
  statusEl.textContent = status;
  statusEl.className = `pill ${cycleStatusClass(status)}`;
  const started = job.started_at ? fmtTS(job.started_at) : "queued";
  const finished = job.finished_at ? ` · finished ${fmtTS(job.finished_at)}` : "";
  $("#cycle-run-meta").textContent = `${job.id || "agent-cycle"} · ${started}${finished}`;
  const findings = Array.isArray(job.findings) && job.findings.length ? job.findings : ["Waiting for findings... decision, candidates, warnings, and sidecar signals will appear here."];
  $("#cycle-run-findings").innerHTML = renderCycleLogLines(findings);
  const output = String(job.output || "").trim();
  const lines = output ? output.split(/\r?\n/).slice(-90) : ["Waiting for process output..."];
  $("#cycle-run-output").innerHTML = renderCycleLogLines(lines);
}

async function pollJob(jobId, onDone, onUpdate) {
  for (let i = 0; i < 240; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const j = await fetchJSON(`/api/jobs?id=${encodeURIComponent(jobId)}`);
      onUpdate && onUpdate(j);
      if (j.status === "done" || j.status === "failed" || j.status === "error" || j.status === "timeout") {
        onDone && onDone(j);
        return j;
      }
    } catch (e) { /* keep polling */ }
  }
  return null;
}

// ---------- Bootstrap (cached lookups used by multiple tabs) -------------

const CACHE = { datasets: null, families: null };

async function loadDatasets() {
  if (!CACHE.datasets) CACHE.datasets = (await fetchJSON("/api/datasets")).datasets || [];
  return CACHE.datasets;
}
async function loadFamilies() {
  if (!CACHE.families) CACHE.families = (await fetchJSON("/api/strategies/families")).families || [];
  return CACHE.families;
}

async function bootstrap() {
  // Sidebar advanced toggle
  const advBtn = document.getElementById("btn-toggle-adv");
  if (advBtn) {
    advBtn.addEventListener("click", () => {
      const adv = document.getElementById("nav-advanced");
      adv.classList.toggle("hidden");
      advBtn.textContent = adv.classList.contains("hidden") ? "Advanced ▾" : "Advanced ▴";
    });
  }
  refreshPills();
  refreshLiveBanner();
  setInterval(refreshPills, 8000);
  setInterval(refreshLiveBanner, 6000);
  switchTab("live");
  setBrokerMode(false);
}

function setBrokerMode(online) {
  const wasOnline = LIVE.bridgeOnline;
  LIVE.bridgeOnline = !!online;
  document.body.classList.toggle("broker-online", !!online);
  document.body.classList.toggle("broker-offline", !online);
  const card = document.getElementById("broker-connection-card");
  if (card) card.classList.toggle("hidden", !!online);
  updateConnectionCard(online);
  if (!wasOnline && online) {
    refreshLiveChart();
    refreshIFVGAssistant();
  }
}

function updateConnectionCard(online, detail) {
  const detailEl = document.getElementById("connection-detail");
  if (detailEl && detail) detailEl.textContent = detail;
  const s1 = document.getElementById("conn-step-mt5");
  const s2 = document.getElementById("conn-step-bridge");
  const s3 = document.getElementById("conn-step-ready");
  if (s1) s1.classList.toggle("done", !!online);
  if (s2) s2.classList.toggle("done", !!online);
  if (s3) s3.classList.toggle("done", !!online);
}

let bridgeAutoStartDone = false;

async function autoConnectBroker() {
  if (bridgeAutoStartDone || LIVE.bridgeOnline) return;
  bridgeAutoStartDone = true;
  updateConnectionCard(false, "Auto-starting MT5 bridge…");
  try {
    await postJSON("/api/bridge/start", {});
  } catch (e) { /* start.sh is the full path; this is a best-effort retry */ }
  for (let i = 0; i < 15; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const b = await fetchJSON("/api/bridge/status");
      if (b.online) {
        await refreshLiveBanner();
        await refreshLiveSide();
        await refreshIFVGAssistant();
        return;
      }
    } catch (e) {}
  }
  updateConnectionCard(false, "Still offline — run ./start from the project folder (one command brings everything up).");
}

async function startBridge() {
  const btn = document.getElementById("btn-start-bridge");
  if (btn) btn.disabled = true;
  updateConnectionCard(false, "Retrying MT5 + bridge…");
  try {
    const r = await postJSON("/api/bridge/start", {});
    toast(r.message || (r.ok ? "Bridge starting" : r.error), r.ok ? "ok" : "err");
    updateConnectionCard(false, r.message || r.error || "");
    setTimeout(async () => {
      await refreshLiveBanner();
      await refreshLiveSide();
      await refreshIFVGAssistant();
    }, 8000);
  } catch (e) {
    toast(e.message, "err");
    updateConnectionCard(false, e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refreshLiveBanner() {
  const el = document.getElementById("live-banner");
  const text = document.getElementById("banner-text");
  const detail = document.getElementById("banner-detail");
  if (!el || !text) return;
  try {
    const b = await fetchJSON("/api/bridge/status");
    setBrokerMode(!!b.online);
    if (b.online) {
      el.className = "live-banner banner-ok";
      const acct = b.account || {};
      text.textContent = `LIVE · ${b.symbol || "XAUUSD"} connected`;
      detail.textContent = acct.equity != null
        ? `Equity ${fmtNum(acct.equity, 2)} · balance ${fmtNum(acct.balance, 2)}`
        : "";
      updateConnectionCard(true, b.next_action || "");
    } else {
      el.className = "live-banner banner-warn";
      text.textContent = "Paper mode — live orders locked";
      detail.textContent = "Connect broker below to approve live trades";
      updateConnectionCard(false, b.next_action || "Run ./start from the project folder.");
      if (!bridgeAutoStartDone) autoConnectBroker();
    }
  } catch (e) {
    setBrokerMode(false);
    el.className = "live-banner banner-warn";
    text.textContent = "Paper mode — broker status unknown · live orders locked";
    detail.textContent = "Run ./start from the project folder";
    if (!bridgeAutoStartDone) autoConnectBroker();
  }
}

// ---------- Live (home tab) ---------------------------------------------

const LIVE = {
  tf: 15,
  chart: null,
  candleSeries: null,
  emaSeries: [],
  poller: null,
  zoneLines: [],
  ifvgZoneLines: [],
  planLines: [],
  showZones: false,
  currentSetup: null,
  bridgeOnline: false,
  lastBridgeRefresh: 0,
  lastBars: [],
  detectedZones: [],
  overlayBound: false,
  scoutReadyNotified: false,
  lastBrief: null,
};

async function loadLive() {
  if (!TAB_INIT.has("live")) {
    document.querySelectorAll("#live-tf-pills button").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#live-tf-pills button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        LIVE.tf = parseInt(b.dataset.tf, 10);
        refreshLiveChart();
        refreshLiveSide();
        refreshLiveTracker();
        refreshIFVGAssistant();
      });
    });
    document.getElementById("live-ind-ema").addEventListener("change", refreshLiveChart);
    const zonesEl = document.getElementById("live-ind-zones");
    if (zonesEl) {
      zonesEl.addEventListener("change", () => {
        LIVE.showZones = zonesEl.checked;
        refreshLiveChart();
      });
    }
    document.getElementById("live-autoref").addEventListener("change", () => {
      stopLivePoller();
      if (document.getElementById("live-autoref").checked) startLivePoller();
    });
    document.getElementById("btn-toggle-auto").addEventListener("click", toggleAuto);
    const startBridgeBtn = document.getElementById("btn-start-bridge");
    if (startBridgeBtn) startBridgeBtn.addEventListener("click", startBridge);
    document.addEventListener("click", (event) => {
      if (event.target.id === "btn-ifvg-approve") approveIFVGTrade();
    });
    const levelNav = document.getElementById("live-level-nav");
    const ifvgPanel = document.getElementById("live-ifvg-assistant");
    if (levelNav) levelNav.addEventListener("click", handleLevelNavClick);
    if (ifvgPanel) ifvgPanel.addEventListener("click", handleLevelNavClick);
    TAB_INIT.add("live");
  }
  await refreshLiveBanner();
  await Promise.all([refreshLiveChart(), refreshLiveSide(), refreshLiveTracker(), refreshIFVGAssistant()]);
  if (document.getElementById("live-autoref").checked) startLivePoller();
}

function startLivePoller() {
  stopLivePoller();
  LIVE.poller = setInterval(async () => {
    if (currentTab() !== "live") return;
    try {
      await refreshLiveChart();
      await refreshLiveSide();
      await refreshLiveTracker();
      await refreshIFVGAssistant();
    } catch (e) {}
  }, 5000);
}
function stopLivePoller() {
  if (LIVE.poller) { clearInterval(LIVE.poller); LIVE.poller = null; }
}

function fmtBarAge(sec) {
  if (sec == null || Number.isNaN(sec)) return "";
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  return `${Math.round(sec / 3600)}h ago`;
}

function levelRow(label, price, kind, extra) {
  const n = Number(price);
  if (price == null || !isFinite(n)) return "";
  return `<button type="button" class="level-row level-${kind}" data-price="${n}" data-level="${kind}" title="Click: focus chart · Double-click: copy">
    <span class="level-lbl">${escapeHTML(label)}</span>
    <span class="level-val ${extra || ""}">${fmtNum(n, 2)}</span>
  </button>`;
}

function zoneSideBandClass(side, historical = false) {
  const isLong = String(side || "").toLowerCase() === "long";
  if (historical) return isLong ? "ifvg-hist-bull" : "ifvg-hist-bear";
  return isLong ? "ifvg-bull" : "ifvg-bear";
}

function zoneSideBandTag(side) {
  const s = String(side || "").toLowerCase();
  if (s === "long") return "BUY · IFVG support";
  if (s === "short") return "SELL · IFVG resistance";
  return "IFVG zone";
}

function tradeActionLabel(side) {
  const s = String(side || "").toLowerCase();
  if (s === "long") return "BUY / long";
  if (s === "short") return "SELL / short";
  return (side || "IFVG").toString().toUpperCase();
}

function buildIfvgPlanHtml(plan, zone, side) {
  const sideNorm = String(side || "").toLowerCase();
  const sideCls = sideNorm === "long" ? "side-long" : "side-short";
  const top = Number(zone.top);
  const bot = Number(zone.bot);
  const zoneRows = [];
  if (isFinite(top) && isFinite(bot)) {
    const hi = Math.max(top, bot);
    const lo = Math.min(top, bot);
    zoneRows.push(levelRow("IFVG top", hi, "zone-top"));
    zoneRows.push(levelRow("IFVG bot", lo, "zone-bot"));
  }
  const entryMid = isFinite(Number(plan.entry))
    ? Number(plan.entry)
    : (isFinite(Number(plan.entry_low)) && isFinite(Number(plan.entry_high)))
      ? (Number(plan.entry_low) + Number(plan.entry_high)) / 2
      : null;
  const entryLabel = isFinite(Number(plan.entry_low)) && isFinite(Number(plan.entry_high))
    ? `${fmtNum(plan.entry_low, 2)} – ${fmtNum(plan.entry_high, 2)}`
    : fmtNum(plan.entry, 2);
  const tpChips = ["tp1", "tp2", "tp3"].map((k, i) => {
    const p = Number(plan[k]);
    if (!isFinite(p)) return "";
    return `<button type="button" class="level-chip kind-tp" data-price="${p}" data-level="tp${i + 1}" title="Click: focus · Double-click: copy"><span class="chip-lbl">TP${i + 1}</span> ${fmtNum(p, 2)}</button>`;
  }).join("");
  return `
    <div class="ifvg-plan">
      <div class="ifvg-zone-block">
        <div class="ifvg-zone-title">${escapeHTML(tradeActionLabel(sideNorm))} · inversion zone · high → low</div>
        ${zoneRows.join("")}
      </div>
      <div class="ifvg-trade-block">
        <div class="ifvg-trade-title">Trade plan</div>
        ${entryMid != null ? `<button type="button" class="level-row level-entry" data-price="${entryMid}" data-level="entry" title="Click: focus · Double-click: copy"><span class="level-lbl">entry</span><span class="level-val ${sideCls}">${entryLabel}</span></button>` : ""}
        ${levelRow("stop", plan.stop, "sl")}
        <div class="tp-row">${tpChips}</div>
      </div>
      <div class="level-hint">Click any level to jump on chart · double-click to copy price</div>
    </div>`;
}

function collectSetupLevels(setup) {
  if (!setup) return [];
  const plan = setup.entry_plan || {};
  const zone = setup.zone || {};
  const out = [];
  const push = (label, price, kind) => {
    const n = Number(price);
    if (isFinite(n)) out.push({ label, price: n, kind });
  };
  if (isFinite(Number(zone.top)) && isFinite(Number(zone.bot))) {
    push("IFVG-T", Math.max(zone.top, zone.bot), "zone");
    push("IFVG-B", Math.min(zone.top, zone.bot), "zone");
  }
  if (isFinite(Number(plan.entry))) push("Entry", plan.entry, "entry");
  push("SL", plan.stop, "sl");
  push("TP1", plan.tp1, "tp");
  push("TP2", plan.tp2, "tp");
  push("TP3", plan.tp3, "tp");
  return out;
}

function updateLevelNav(setup) {
  const nav = document.getElementById("live-level-nav");
  if (!nav) return;
  const levels = collectSetupLevels(setup);
  if (!levels.length) {
    nav.classList.add("hidden");
    nav.innerHTML = "";
    return;
  }
  nav.classList.remove("hidden");
  nav.innerHTML = levels.map(({ label, price, kind }) =>
    `<button type="button" class="level-chip kind-${kind}" data-price="${price}" data-level="${label}" title="Click: focus chart · Double-click: copy">
      <span class="chip-lbl">${escapeHTML(label)}</span> ${fmtNum(price, 2)}
    </button>`
  ).join("");
}

function focusChartPrice(price) {
  if (!LIVE.chart || !LIVE.lastBars?.length) return;
  const p = Number(price);
  if (!isFinite(p)) return;
  const lows = LIVE.lastBars.map((b) => b.low);
  const highs = LIVE.lastBars.map((b) => b.high);
  const dataMin = Math.min(...lows);
  const dataMax = Math.max(...highs);
  const span = Math.max(dataMax - dataMin, Math.abs(p) * 0.002, 8);
  const mid = (dataMin + dataMax) / 2;
  const dist = Math.abs(p - mid) / span;
  LIVE.chart.priceScale("right").applyOptions({
    autoScale: true,
    scaleMargins: {
      top: p > mid ? Math.min(0.35, 0.08 + dist * 0.25) : 0.08,
      bottom: p <= mid ? Math.min(0.35, 0.08 + dist * 0.25) : 0.08,
    },
  });
  LIVE.chart.timeScale().scrollToRealTime();
  syncZoneOverlayPositions();
}

function handleLevelNavClick(event) {
  const el = event.target.closest("[data-price]");
  if (!el) return;
  const price = Number(el.dataset.price);
  if (!isFinite(price)) return;
  if (event.detail >= 2) {
    event.preventDefault();
    navigator.clipboard.writeText(String(price)).then(
      () => toast(`Copied ${fmtNum(price, 2)}`),
      () => toast("Copy failed", "err"),
    );
    return;
  }
  focusChartPrice(price);
  document.querySelectorAll(".level-row.is-active, .level-chip.is-active").forEach((n) => n.classList.remove("is-active"));
  el.classList.add("is-active");
}

// ---------- Chart zone overlays (TradingView-style bands, no axis label spam) ---

function clearZoneOverlay() {
  const el = document.getElementById("live-zone-overlay");
  if (el) el.innerHTML = "";
}

function addZoneBand(top, bot, className, tag) {
  const overlay = document.getElementById("live-zone-overlay");
  if (!overlay) return;
  const hi = Math.max(Number(top), Number(bot));
  const lo = Math.min(Number(top), Number(bot));
  if (!isFinite(hi) || !isFinite(lo)) return;
  const div = document.createElement("div");
  div.className = `zone-band ${className}`;
  div.dataset.top = String(hi);
  div.dataset.bot = String(lo);
  if (tag) div.innerHTML = `<span class="zone-tag">${escapeHTML(tag)}</span>`;
  overlay.appendChild(div);
}

function syncZoneOverlayPositions() {
  const overlay = document.getElementById("live-zone-overlay");
  if (!overlay || !LIVE.candleSeries || !LIVE.chart) return;
  overlay.querySelectorAll(".zone-band").forEach((band) => {
    const hi = Number(band.dataset.top);
    const lo = Number(band.dataset.bot);
    const yHi = LIVE.candleSeries.priceToCoordinate(hi);
    const yLo = LIVE.candleSeries.priceToCoordinate(lo);
    if (yHi == null || yLo == null) {
      band.style.display = "none";
      return;
    }
    const topPx = Math.min(yHi, yLo);
    const heightPx = Math.max(Math.abs(yLo - yHi), 3);
    band.style.display = "block";
    band.style.top = `${topPx}px`;
    band.style.height = `${heightPx}px`;
  });
}

function ensureOverlaySync() {
  if (!LIVE.chart || LIVE.overlayBound) return;
  LIVE.overlayBound = true;
  LIVE.chart.timeScale().subscribeVisibleLogicalRangeChange(() => syncZoneOverlayPositions());
  const stage = document.querySelector(".chart-stage");
  if (stage && typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => syncZoneOverlayPositions()).observe(stage);
  }
  window.addEventListener("resize", syncZoneOverlayPositions);
}

function analyzeZoneBehavior(bars, lo, hi, lookback = 150) {
  const slice = (bars || []).slice(-lookback);
  if (!slice.length || !isFinite(lo) || !isFinite(hi)) {
    return { touchCount: 0, last: null, recent: [], inZoneNow: false };
  }
  const zoneMid = (lo + hi) / 2;
  const touches = [];
  let inZoneNow = false;
  for (const b of slice) {
    const touched = b.high >= lo && b.low <= hi;
    if (!touched) continue;
    inZoneNow = slice[slice.length - 1] === b && b.close >= lo && b.close <= hi;
    let reaction = "testing";
    if (b.close > hi) reaction = "break up";
    else if (b.close < lo) reaction = "break down";
    else if (b.close >= lo && b.close <= hi) reaction = "accept";
    else if (b.close > zoneMid) reaction = "reject ↑";
    else reaction = "reject ↓";
    touches.push({ time: b.time, reaction, close: b.close });
  }
  return {
    touchCount: touches.length,
    last: touches[touches.length - 1] || null,
    recent: touches.slice(-6),
    inZoneNow,
  };
}

function reactionChipClass(reaction) {
  if (!reaction) return "";
  if (reaction.includes("break")) return "react-break";
  if (reaction.includes("reject")) return "react-reject";
  if (reaction.includes("accept")) return "react-accept";
  return "";
}

function renderZoneBehaviorPanel(setup) {
  const el = document.getElementById("live-zone-behavior");
  if (!el || !setup) {
    if (el) el.classList.add("hidden");
    return;
  }
  const zone = setup.zone || {};
  const lo = Math.min(Number(zone.bot), Number(zone.top));
  const hi = Math.max(Number(zone.bot), Number(zone.top));
  if (!isFinite(lo) || !isFinite(hi)) {
    el.classList.add("hidden");
    return;
  }
  const behavior = analyzeZoneBehavior(LIVE.lastBars, lo, hi);
  const side = (setup.side || "").toLowerCase();
  const favorable = side === "short"
    ? (behavior.last?.reaction || "").includes("reject")
    : (behavior.last?.reaction || "").includes("accept") || (behavior.last?.reaction || "").includes("reject ↓");
  const timeline = behavior.recent.map((t) =>
    `<span class="behavior-chip ${reactionChipClass(t.reaction)}" title="close ${fmtNum(t.close, 2)}">${escapeHTML(t.reaction)} @ ${fmtNum(t.close, 0)}</span>`
  ).join("");
  el.classList.remove("hidden");
  el.innerHTML = `
    <h3>IFVG zone behaviour · how price reacted at ${fmtNum(lo, 0)}–${fmtNum(hi, 0)}</h3>
    <div class="behavior-grid">
      <div class="behavior-stat"><strong>${behavior.touchCount}</strong><span>touches (lookback)</span></div>
      <div class="behavior-stat"><strong>${escapeHTML(behavior.last?.reaction || "—")}</strong><span>last reaction</span></div>
      <div class="behavior-stat"><strong>${favorable ? "supports setup" : "caution"}</strong><span>vs ${escapeHTML(side || "?")} bias</span></div>
      <div class="behavior-stat"><strong>${behavior.inZoneNow ? "in zone" : "outside"}</strong><span>current price</span></div>
    </div>
    ${timeline ? `<div class="behavior-timeline">${timeline}</div>` : `<div class="muted small">No recent touches in lookback — watch for first retest into the band.</div>`}`;
}

function macroBiasClass(bias) {
  const b = String(bias || "").toLowerCase();
  if (b.includes("supports_buy") || b.includes("bullish")) return "support";
  if (b.includes("supports_sell") || b.includes("bearish")) return "against";
  return "neutral";
}

function renderConfluenceStrip(setup) {
  const el = document.getElementById("live-confluence-strip");
  if (!el || !setup) {
    if (el) el.classList.add("hidden");
    return;
  }
  const ext = setup.external_research || {};
  const macro = ext.macro || {};
  const opts = ext.options || {};
  const levels = (opts.important_levels || []).slice(0, 4);
  const pills = [];
  pills.push(`<span class="conf-pill ${macroBiasClass(macro.dxy_bias)}"><span class="conf-lbl">DXY</span>${escapeHTML(macro.dxy_bias || "?")}</span>`);
  pills.push(`<span class="conf-pill ${macroBiasClass(macro.us10y_bias)}"><span class="conf-lbl">US10Y</span>${escapeHTML(macro.us10y_bias || "?")}</span>`);
  pills.push(`<span class="conf-pill ${macroBiasClass(opts.bias)}"><span class="conf-lbl">Options</span>${escapeHTML(opts.bias || "?")}${levels.length ? ` · ${levels.map((l) => fmtNum(l, 0)).join(", ")}` : ""}</span>`);
  if (ext.news_risk && ext.news_risk !== "unknown") {
    pills.push(`<span class="conf-pill neutral"><span class="conf-lbl">News</span>${escapeHTML(ext.news_risk)}</span>`);
  }
  if (ext.confidence != null) {
    pills.push(`<span class="conf-pill ${ext.supports_trade ? "support" : "neutral"}"><span class="conf-lbl">AI conf</span>${ext.confidence}/100</span>`);
  }
  const cmeNote = (opts.notes || ext.summary || "").slice(0, 80);
  if (cmeNote) {
    pills.push(`<span class="conf-pill neutral" title="${escapeHTML(ext.summary || "")}"><span class="conf-lbl">CME / flow</span>${escapeHTML(cmeNote)}</span>`);
  }
  el.classList.remove("hidden");
  el.innerHTML = pills.join("");
}

function renderSetupChartBands(setup) {
  const plan = setup.entry_plan || {};
  const zone = setup.zone || {};
  const side = (setup.side || "").toLowerCase();
  const isLong = side === "long";
  const zLo = Math.min(Number(zone.bot), Number(zone.top));
  const zHi = Math.max(Number(zone.bot), Number(zone.top));
  if (isFinite(zLo) && isFinite(zHi)) {
    addZoneBand(zHi, zLo, zoneSideBandClass(side), zoneSideBandTag(side));
  }
  const entryLo = Number(plan.entry_low ?? zLo);
  const entryHi = Number(plan.entry_high ?? zHi);
  const stop = Number(plan.stop);
  const tp1 = Number(plan.tp1);
  if (isLong) {
    if (isFinite(entryHi) && isFinite(tp1)) addZoneBand(Math.max(entryHi, tp1), Math.min(entryHi, tp1), "trade-target", "Target");
    if (isFinite(stop) && isFinite(entryLo)) addZoneBand(Math.max(stop, entryLo), Math.min(stop, entryLo), "trade-risk", "Risk");
  } else {
    if (isFinite(entryLo) && isFinite(tp1)) addZoneBand(Math.max(entryLo, tp1), Math.min(entryLo, tp1), "trade-target", "Target");
    if (isFinite(stop) && isFinite(entryHi)) addZoneBand(Math.max(stop, entryHi), Math.min(stop, entryHi), "trade-risk", "Risk");
  }
  renderExternalLevelBands(setup);
}

function renderExternalLevelBands(setup) {
  const ext = setup.external_research || {};
  const opts = ext.options || {};
  const tick = Math.max((LIVE.lastBars?.length ? (LIVE.lastBars[LIVE.lastBars.length - 1].high - LIVE.lastBars[0].low) / 200 : 2), 1.5);
  (opts.important_levels || []).slice(0, 5).forEach((p) => {
    const n = Number(p);
    if (!isFinite(n)) return;
    addZoneBand(n + tick, n - tick, "options-level", "Options");
  });
  (opts.danger_zones || []).slice(0, 3).forEach((p) => {
    const n = Number(p);
    if (!isFinite(n)) return;
    addZoneBand(n + tick * 1.5, n - tick * 1.5, "danger-level", "Danger");
  });
}

function renderHistoricalIfvgBands(zones) {
  (zones || [])
    .filter((z) => z.status === "active" && (z.kind === "ifvg_bull" || z.kind === "ifvg_bear"))
    .slice(-6)
    .forEach((z) => {
      addZoneBand(z.top, z.bot, zoneSideBandClass(z.side, true), zoneSideBandTag(z.side));
    });
}

function renderChartOverlays() {
  clearZoneOverlay();
  if (LIVE.currentSetup) {
    renderSetupChartBands(LIVE.currentSetup);
    renderZoneBehaviorPanel(LIVE.currentSetup);
    renderConfluenceStrip(LIVE.currentSetup);
  } else {
    renderZoneBehaviorPanel(null);
    renderConfluenceStrip(null);
    if (LIVE.showZones && LIVE.detectedZones?.length) {
      LIVE.detectedZones.filter((z) => z.status !== "invalidated").slice(-12).forEach((z) => {
        if (z.kind.startsWith("ifvg")) {
          addZoneBand(z.top, z.bot, zoneSideBandClass(z.side, true), zoneSideBandTag(z.side));
        }
      });
    } else if (LIVE.detectedZones?.length) {
      renderHistoricalIfvgBands(LIVE.detectedZones);
    }
  }
  syncZoneOverlayPositions();
}

async function refreshLiveChart() {
  const meta = document.getElementById("live-chart-meta");
  const tf = LIVE.tf;
  try {
    const cacheParam = LIVE.bridgeOnline ? "" : "&prefer_cache=1";
    const d = await fetchJSON(`/api/live/candles?timeframe=${tf}&count=600${cacheParam}`);
    const bars = d.bars || [];
    // Normalize and show symbol as XAU/USD
    const rawSym = d.symbol || "XAUUSD";
    const sym = (typeof rawSym === "string") ? rawSym.replace(/^([A-Z]{3})([A-Z]{3})$/, '$1/$2') : rawSym;
    document.getElementById("live-symbol-label").textContent = `Symbol: ${sym}`;
    if (!bars.length) {
      meta.textContent = `no data (${d.source}${d.error ? " · " + d.error : ""})`;
      if (LIVE.chart && LIVE.candleSeries) LIVE.candleSeries.setData([]);
      return;
    }
    const dataLabel = d.source === "bridge" ? "MT5 bridge live" : (d.source === "twelvedata" ? "TwelveData fallback" : (d.source === "csv_fallback" ? "cached" : (d.source || "unknown")));
    const age = d.age_sec != null ? ` · last bar ${fmtBarAge(d.age_sec)}` : "";
    meta.textContent = `Data: ${dataLabel} · ${bars.length} bars · TF ${tf}m${age}`;
    renderLiveChart(bars, document.getElementById("live-ind-ema").checked);
  } catch (e) {
    meta.textContent = "error: " + e.message;
  }
}

function renderLiveChart(bars, withEma) {
  const container = document.getElementById("live-chart");
  if (!LIVE.chart) {
    container.innerHTML = "";
    const lc = window.LightweightCharts;
    if (!lc) { container.textContent = "chart library failed to load"; return; }
    LIVE.chart = lc.createChart(container, {
      layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
      grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#232a35" },
      rightPriceScale: { borderColor: "#232a35" },
      crosshair: { mode: lc.CrosshairMode.Normal },
      autoSize: true,
    });
    LIVE.candleSeries = LIVE.chart.addCandlestickSeries({
      upColor: "#4ade80", downColor: "#f87171",
      borderUpColor: "#4ade80", borderDownColor: "#f87171",
      wickUpColor: "#4ade80", wickDownColor: "#f87171",
    });
    ensureOverlaySync();
  }
  LIVE.candleSeries.setData(bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
  LIVE.lastBars = bars;
  // Wipe old EMA overlays
  LIVE.emaSeries.forEach((s) => { try { LIVE.chart.removeSeries(s); } catch (e) {} });
  LIVE.emaSeries = [];
  if (withEma) {
    const closes = bars.map((b) => b.close);
    const ema = (n) => {
      const k = 2 / (n + 1);
      let prev = closes[0];
      const out = [];
      for (let i = 0; i < closes.length; i++) {
        prev = i === 0 ? closes[0] : closes[i] * k + prev * (1 - k);
        if (i >= n) out.push({ time: bars[i].time, value: prev });
      }
      return out;
    };
    [["20", "#f6c44a"], ["50", "#60a5fa"]].forEach(([n, color]) => {
      const s = LIVE.chart.addLineSeries({ color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      s.setData(ema(parseInt(n, 10)));
      LIVE.emaSeries.push(s);
    });
  }
  // Zones (FVG / IFVG / swings / prev-day / asian) overlaid as price lines
  // — lightweight-charts has no native box, so we draw the top + bottom of each
  //   active/mitigated zone as a coloured horizontal line on the candle series.
  LIVE.zoneLines.forEach((pl) => {
    try { LIVE.candleSeries.removePriceLine(pl); } catch (e) {}
  });
  LIVE.zoneLines = [];
  if (LIVE.showZones) {
    refreshZones();
  } else {
    refreshIFVGZones();
  }
  drawSetupPlan();
  syncZoneOverlayPositions();
}

async function refreshIFVGZones() {
  if (!LIVE.candleSeries) return;
  LIVE.ifvgZoneLines.forEach((pl) => {
    try { LIVE.candleSeries.removePriceLine(pl); } catch (e) {}
  });
  LIVE.ifvgZoneLines = [];
  try {
    const cacheParam = LIVE.bridgeOnline ? "" : "&prefer_cache=1";
    const d = await fetchJSON(`/api/live/zones?timeframe=${LIVE.tf}&count=600&lookback=120${cacheParam}`);
    LIVE.detectedZones = d.zones || [];
    if (!LIVE.currentSetup) renderChartOverlays();
  } catch (e) {}
}

// Draw trade setup overlays + level nav (colour bands replace overlapping axis labels).
function drawSetupPlan() {
  if (!LIVE.candleSeries) return;
  (LIVE.planLines || []).forEach((pl) => {
    try { LIVE.candleSeries.removePriceLine(pl); } catch (e) {}
  });
  LIVE.planLines = [];
  updateLevelNav(LIVE.currentSetup);
  renderChartOverlays();
}

const ZONE_STYLE = {
  fvg_bull:  { color: "#22c55e", title: "FVG↑" },
  fvg_bear:  { color: "#ef4444", title: "FVG↓" },
  ifvg_bull: { color: "#22c55e", title: "IFVG BUY" },
  ifvg_bear: { color: "#ef4444", title: "IFVG SELL" },
  swing_high:{ color: "#f59e0b", title: "SH" },
  swing_low: { color: "#f59e0b", title: "SL" },
  pdh:       { color: "#94a3b8", title: "PDH" },
  pdl:       { color: "#94a3b8", title: "PDL" },
  asian_high:{ color: "#06b6d4", title: "AH" },
  asian_low: { color: "#06b6d4", title: "AL" },
};

async function refreshZones() {
  if (!LIVE.candleSeries) return;
  LIVE.zoneLines.forEach((pl) => {
    try { LIVE.candleSeries.removePriceLine(pl); } catch (e) {}
  });
  LIVE.zoneLines = [];
  try {
    const cacheParam = LIVE.bridgeOnline ? "" : "&prefer_cache=1";
    const d = await fetchJSON(`/api/live/zones?timeframe=${LIVE.tf}&count=600&lookback=120${cacheParam}`);
    LIVE.detectedZones = d.zones || [];
    renderChartOverlays();
    const meta = document.getElementById("live-chart-meta");
    if (meta) {
      const tag = meta.textContent.split(" · zones")[0];
      meta.textContent = `${tag} · zones ${LIVE.detectedZones.length}`;
    }
  } catch (e) {}
}

async function refreshLiveSide() {
  try {
    const b = await fetchJSON("/api/bridge/status");
    setBrokerMode(!!b.online);
    const ae = document.getElementById("live-account");
    if (b.online && b.account) {
      const a = b.account;
      ae.innerHTML = `
        <div><span>equity</span><span>${fmtNum(a.equity, 2)} ${escapeHTML(a.currency || "")}</span></div>
        <div><span>balance</span><span>${fmtNum(a.balance, 2)}</span></div>
        <div><span>margin free</span><span>${fmtNum(a.margin_free, 2)}</span></div>
        <div><span>leverage</span><span>${a.leverage || "?"}x</span></div>`;
    } else if (!b.online) {
      ae.innerHTML = `<div class="account-offline-msg">Not connected — use the card above to start the bridge. You can still scan IFVG setups on cached chart data.</div>`;
    } else {
      ae.innerHTML = `<span class="muted">Connected — waiting for account info</span>`;
    }
    const pe = document.getElementById("live-position");
    const p = b.open_position;
    if (p && p.broker_order_id) {
      pe.innerHTML = `
        <div class="kv-grid">
          <div><span>side</span><span class="${p.side === "long" ? "verdict-allow" : "verdict-block"}">${escapeHTML(p.side)}</span></div>
          <div><span>entry</span><span>${fmtNum(p.entry_price, 2)}</span></div>
          <div><span>stop</span><span>${fmtNum(p.stop_price, 2)}</span></div>
          <div><span>target</span><span>${fmtNum(p.target_price, 2)}</span></div>
          <div><span>volume</span><span>${fmtNum(p.volume, 2)}</span></div>
          <div><span>id</span><span class="muted small">${escapeHTML(p.broker_order_id)}</span></div>
        </div>
        <button class="ghost small" id="btn-live-close">Close position</button>`;
      const btn = document.getElementById("btn-live-close");
      if (btn) btn.addEventListener("click", async () => {
        if (!confirm("Close this position via MT5?")) return;
        try {
          await postJSON("/api/bridge/close", { broker_order_id: p.broker_order_id, reason: "manual_ui" });
          toast("close requested");
          await refreshLiveSide();
        } catch (e) { toast(e.message, "err"); }
      });
    } else {
      pe.innerHTML = `<span class="muted">no open position</span>`;
    }
  } catch (e) {
    setBrokerMode(false);
    document.getElementById("live-account").innerHTML = `<div class="account-offline-msg">Could not reach bridge. Start it from the connection card above.</div>`;
  }
  // Auto-trader pill
  try {
    const s = await fetchJSON("/api/summary");
    const en = s.config && s.config.auto_trade_enabled;
    const pill = document.getElementById("live-auto-pill");
    pill.textContent = en ? "ARMED" : "PAUSED";
    pill.className = "pill " + (en ? "ok" : "warn");
  } catch (e) {}
}

async function toggleAuto() {
  try {
    const s = await fetchJSON("/api/summary");
    const cur = !!(s.config && s.config.auto_trade_enabled);
    await postJSON("/api/config", { auto_trade_enabled: !cur });
    toast(`auto-trader ${!cur ? "ARMED" : "PAUSED"}`);
    await refreshLiveSide();
    refreshPills();
  } catch (e) { toast(e.message, "err"); }
}

async function refreshLiveTracker() {
  const el = document.getElementById("live-tracker");
  if (!el) return;
  try {
    const d = await fetchJSON(`/api/live/tracker?timeframe=${LIVE.tf}&count=600`);
    const b = d.last_bar || {};
    const levels = d.levels || {};
    const macro = d.macro || {};
    const age = Number(d.age_sec);
    const ageAbs = Math.abs(age);
    const fresh = d.source === "bridge" || (isFinite(ageAbs) && ageAbs <= 180);
    const ageLabel = isFinite(age)
      ? (age < -10 ? `clock +${Math.round(-age)}s` : `${Math.round(age)}s`)
      : "—";
    const sourceClass = d.source === "bridge" ? "verdict-allow" : "verdict-warn";
    const longVerdict = macro.long ? macro.long.verdict : "?";
    const shortVerdict = macro.short ? macro.short.verdict : "?";
    const pos = Number(levels.position_in_range);
    const posPct = isFinite(pos) ? Math.max(0, Math.min(100, pos * 100)) : 0;
    const alerts = (d.watch_log || []).filter((line) => line.includes("ALERT")).slice(-3);
    el.innerHTML = `
      <div class="kv-grid tracker-kv">
        <div><span>source</span><span class="${sourceClass}">${escapeHTML(d.source || "?")}</span></div>
        <div><span>freshness</span><span class="${fresh ? "verdict-allow" : "verdict-warn"}">${ageLabel}</span></div>
        <div><span>last candle</span><span>${fmtTS(b.iso || (b.time ? new Date(b.time * 1000).toISOString() : ""))}</span></div>
        <div><span>close</span><span>${fmtNum(b.close, 2)}</span></div>
        <div><span>macro long</span><span class="${longVerdict === "block" ? "verdict-block" : longVerdict.includes("warning") ? "verdict-warn" : "verdict-allow"}">${escapeHTML(longVerdict)}</span></div>
        <div><span>macro short</span><span class="${shortVerdict === "block" ? "verdict-block" : shortVerdict.includes("warning") ? "verdict-warn" : "verdict-allow"}">${escapeHTML(shortVerdict)}</span></div>
      </div>
      <div class="tracker-range">
        <div class="tracker-bar"><span style="left:${posPct}%"></span></div>
        <div class="tracker-labels"><span>L ${fmtNum(levels.session_low, 2)}</span><span>mid ${fmtNum(levels.midpoint, 2)}</span><span>H ${fmtNum(levels.session_high, 2)}</span></div>
      </div>
      <div class="tracker-plan">
        <div><strong>Short rejection</strong> ${fmtNum((levels.short_rejection_zone || [])[0], 2)}–${fmtNum((levels.short_rejection_zone || [])[1], 2)}</div>
        <div><strong>Short breakdown</strong> below ${fmtNum(levels.short_breakdown, 2)}</div>
        <div><strong>Long reclaim</strong> above ${fmtNum(levels.long_reclaim, 2)}</div>
      </div>
      <div class="tracker-alerts">${alerts.length ? renderLogLines(alerts) : "<span class='muted'>no tracker alerts fired</span>"}</div>
      ${d.error ? `<div class="muted small">bridge note: ${escapeHTML(d.error)}</div>` : ""}`;
  } catch (e) {
    el.innerHTML = `<span class="verdict-block">tracker error: ${escapeHTML(e.message)}</span>`;
  }
}

const IFVG_VERDICT = {
  valid_entry: { cls: "is-valid", label: "VALID ENTRY", canApprove: true },
  alert_wait: { cls: "is-alert", label: "ALERT · WAIT", canApprove: false },
};

function tradeStyleLabel(style) {
  const map = { scalping: "Scalp", intraday: "Intraday", swing: "Swing", position: "Position" };
  return map[style] || style || "Trade";
}

function scoutStatusLabel(status) {
  const map = {
    ready_to_enter: "Ready to enter",
    alert_wait: "Setup forming",
    watching: "Watching",
    scanning: "Scanning",
    offline: "Offline",
    starting: "Starting",
  };
  return map[status] || status || "Watching";
}

function buildWorkflowStepsHtml(brief) {
  const steps = brief?.workflow_steps || [];
  if (!steps.length) return "";
  const formula = brief.formula || "";
  const rows = steps.map((s) => {
    const cls = s.status === "pass" ? "pass" : (s.status === "warn" ? "partial" : (s.status === "fail" ? "fail" : "wait"));
    return `<div class="workflow-step ${cls}"><span class="wf-num">${s.step}</span><div><strong>${escapeHTML(s.title)}</strong><small>${escapeHTML(s.detail || "")}</small></div></div>`;
  }).join("");
  const passes = brief.workflow_passes != null ? `${brief.workflow_passes}/${steps.length}` : "";
  return `
    <div class="workflow-panel">
      <div class="workflow-formula muted small">${escapeHTML(formula)}</div>
      <div class="workflow-steps">${rows}</div>
      ${passes ? `<div class="workflow-score muted small">${passes} steps green</div>` : ""}
    </div>`;
}

function buildApprovalBriefHtml(brief, canEnter) {
  if (!brief) return "";
  const workflowHtml = buildWorkflowStepsHtml(brief);
  const reasons = (brief.reasons || []).map((r) => {
    const cls = r.status === "pass" ? "pass" : (r.status === "warn" ? "partial" : "fail");
    return `<div class="brief-reason ${cls}"><span>${escapeHTML(r.title)}</span><small>${escapeHTML(r.detail)}</small></div>`;
  }).join("");
  const blockers = (brief.blockers || []).map((b) => `<div class="brief-blocker">${escapeHTML(b)}</div>`).join("");
  const watch = (brief.model_watch || []).slice(0, 4).map((w) => `<div class="muted small">→ ${escapeHTML(w)}</div>`).join("");
  return `
    <div class="approval-brief ${canEnter ? "is-ready" : "is-wait"}">
      ${workflowHtml}
      <div class="brief-head">${escapeHTML(brief.headline || "")}</div>
      <div class="brief-summary">${escapeHTML(brief.summary || "")}</div>
      ${canEnter ? `<div class="brief-ok">Why you are allowed to enter:</div>` : `<div class="brief-wait">Why you must wait:</div>`}
      ${canEnter ? reasons : (blockers || reasons)}
      ${watch ? `<details class="brief-watch" ${canEnter ? "open" : ""}><summary>${canEnter ? "What the AI is still monitoring" : "What the AI is watching"}</summary>${watch}</details>` : ""}
    </div>`;
}

function updateScoutBanner(scout) {
  const text = document.getElementById("banner-text");
  const detail = document.getElementById("banner-detail");
  const el = document.getElementById("live-banner");
  if (!scout || !text || !el) return;
  const brief = scout.approval_brief || {};
  const canEnter = !!brief.can_enter && LIVE.bridgeOnline;
  if (canEnter) {
    el.className = "live-banner banner-ok banner-enter";
    text.textContent = "READY TO ENTER · click Enter trade";
    detail.textContent = brief.headline || "All gates passed";
    if (!LIVE.scoutReadyNotified) {
      LIVE.scoutReadyNotified = true;
      toast("Setup ready — review why, then Enter trade", "ok");
    }
  } else {
    LIVE.scoutReadyNotified = false;
  }
  if (!canEnter && LIVE.bridgeOnline && scout.status) {
    detail.textContent = `AI ${scoutStatusLabel(scout.status)} · scan every ${scout.scan_interval_sec || 60}s`;
  }
}

async function refreshIFVGAssistant() {
  const el = document.getElementById("live-ifvg-assistant");
  if (!el) return;
  try {
    const d = await fetchJSON(`/api/live/scout?timeframe=${LIVE.tf}`);
    updateScoutBanner(d);
    let scannedText = "";
    if (d.last_scan_at) {
      const ageSec = (Date.now() - new Date(d.last_scan_at).getTime()) / 1000;
      scannedText = ageSec > 300 ? " · Decision stale — run full-system scan" : ` · scanned ${fmtBarAge(ageSec)}`;
    }
    const scoutSource = d.source === "bridge" ? "live MT5" : (d.source || "data");
    const scoutTag = `<div class="scout-status muted small">AI auto-watch · ${escapeHTML(scoutStatusLabel(d.status))} · ${scoutSource}${scannedText}</div>`;
    const brief = d.approval_brief || {};
    const s = d.setup;
    if (!s) {
      LIVE.currentSetup = null;
      drawSetupPlan();
      refreshIFVGZones();
      el.innerHTML = `
        ${scoutTag}
        ${buildApprovalBriefHtml(brief, false)}
        <div class="ifvg-headline is-none">
          <span class="verdict-line">AI watching — no entry yet</span>
          <span class="verdict-sub">Sweep → displacement → inversion → retest. Your only action: click Enter trade when this turns green.</span>
        </div>
        ${(d.model_alerts || []).slice(-3).map((a) => `<div class="muted small ai-alert">▸ ${escapeHTML(a.message)}</div>`).join("")}
        ${d.error ? `<div class="ifvg-warn">${escapeHTML(d.error)}</div>` : ""}`;
      return;
    }
    LIVE.currentSetup = s;
    LIVE.lastBrief = brief;
    const plan = s.entry_plan || {};
    const zone = s.zone || {};
    const ext = s.external_research || {};
    const macro = ext.macro || {};
    const opts = ext.options || {};
    const side = (s.side || "").toLowerCase();
    const grading = s.grading || brief.grading || {};
    const researchMode = ext.mode || brief.research_mode || "soft";
    const finalGrade = grading.letter || s.grade || "?";
    const v = IFVG_VERDICT[s.verdict] || { cls: "is-none", label: (s.verdict || "ignore").toUpperCase(), canApprove: false };
    const blocked = !!s.externally_blocked && researchMode === "hard";
    const canEnter = !!brief.can_enter && !blocked && LIVE.bridgeOnline;
    const riskHint = grading.suggested_risk_pct != null ? `${(grading.suggested_risk_pct * 100).toFixed(2)}% suggested risk` : "";

    const checklist = (s.checklist || []).map((item) => {
      const cls = item.status === "pass" ? "pass" : (item.status === "partial" ? "partial" : "fail");
      return `<div class="ck ${cls}"><span>${escapeHTML(item.label)}</span><span class="pts">${item.points}/${item.max_points}</span></div>`;
    }).join("");

    const warnings = (s.warnings || []).slice(0, 4).map((w) => `<div>${escapeHTML(w)}</div>`).join("");
    const extWarnings = (ext.warnings || []).slice(0, 4).map((w) => `<div class="muted small">${escapeHTML(w)}</div>`).join("");
    const extSources = (ext.sources || []).slice(0, 5).map((src) => `<div class="muted small">${escapeHTML(src)}</div>`).join("");

    const zLo = Math.min(Number(zone.bot), Number(zone.top));
    const zHi = Math.max(Number(zone.bot), Number(zone.top));
    const beh = analyzeZoneBehavior(LIVE.lastBars, zLo, zHi);
    const behLine = beh.touchCount
      ? `Zone tested ${beh.touchCount}× · last: ${beh.last?.reaction || "?"}`
      : "Zone not tested yet — AI watching first retest";

    el.innerHTML = `
      ${scoutTag}
      ${buildApprovalBriefHtml(brief, canEnter)}
      <div class="ifvg-headline ${canEnter ? "is-valid" : v.cls}">
        <span class="verdict-line">${escapeHTML(tradeActionLabel(side))} · ${canEnter ? "ENTER TRADE" : v.label} · ${escapeHTML(tradeStyleLabel(s.trade_style))}</span>
        <span class="verdict-sub">M${LIVE.tf} · grade ${escapeHTML(finalGrade)} · tech ${s.score}/100 · ${escapeHTML(researchMode)} research · ${escapeHTML(riskHint || behLine)}</span>
      </div>
      <div class="ifvg-actions">
        <button id="btn-ifvg-approve" class="primary enter-trade-btn" ${canEnter ? "" : "disabled"} title="${canEnter ? "You have full rationale above — place live on MT5" : (brief.blockers?.[0] || "Waiting for valid entry")}">
          Enter trade
        </button>
      </div>
      ${buildIfvgPlanHtml(plan, zone, side)}
      ${s.inversion_note ? `<div class="muted small ifvg-inversion-note">${escapeHTML(s.inversion_note)}</div>` : ""}
      <details class="ifvg-checklist-wrap" open>
        <summary>Full checklist (${s.score}/100)</summary>
        <div class="ifvg-checklist">${checklist}</div>
      </details>
      ${warnings ? `<div class="ifvg-warn">${warnings}</div>` : ""}
      <details class="ifvg-research">
        <summary>Research detail · macro · options · CME · ${escapeHTML(ext.enabled ? "on" : "off")}</summary>
        <div class="ifvg-ai-verdict">${escapeHTML(s.ai_verdict || brief.summary || "")}</div>
        <div class="kv-grid">
          <div><span>confidence</span><span>${escapeHTML(String(ext.confidence ?? 0))}/100</span></div>
          <div><span>supports trade</span><span>${escapeHTML(String(!!ext.supports_trade))}</span></div>
          <div><span>DXY / US10Y</span><span>${escapeHTML(macro.dxy_bias || "?")} / ${escapeHTML(macro.us10y_bias || "?")}</span></div>
          <div><span>options</span><span>${escapeHTML(opts.bias || "?")}</span></div>
          <div><span>levels</span><span>${escapeHTML((opts.important_levels || []).join(", ") || "—")}</span></div>
        </div>
        <div class="muted small" style="margin-top:8px">${escapeHTML(ext.summary || "Research runs automatically every scan.")}</div>
        ${extWarnings || ""}
        ${extSources || ""}
      </details>
      <details class="brief-watch"><summary>AI internal alerts (last ${(d.model_alerts || []).length})</summary>
        ${(d.model_alerts || []).slice().reverse().map((a) => `<div class="muted small ai-alert">[${escapeHTML(a.kind || "")}] ${escapeHTML(a.message)}</div>`).join("") || "<div class='muted small'>No alerts yet</div>"}
      </details>`;
    drawSetupPlan();
  } catch (e) {
    LIVE.currentSetup = null;
    el.innerHTML = `<div class="ifvg-warn">Scout error: ${escapeHTML(e.message)}</div>`;
  }
}

async function approveIFVGTrade() {
  const s = LIVE.currentSetup;
  const brief = LIVE.lastBrief || {};
  const grading = s?.grading || brief.grading || {};
  const researchMode = (s?.external_research || {}).mode || brief.research_mode || "soft";
  if (!s) { toast("No setup to enter", "err"); return; }
  if (s.externally_blocked && researchMode === "hard") { toast("Setup is externally blocked (hard mode)", "err"); return; }
  if (!brief.can_enter) { toast(brief.blockers?.[0] || "Not allowed to enter yet", "err"); return; }
  if (!LIVE.bridgeOnline) { toast("MT5 bridge is offline — run ./start", "err"); return; }

  const plan = s.entry_plan || {};
  const side = (s.side || "").toUpperCase();
  const why = (brief.reasons || []).filter((r) => r.status === "pass").slice(0, 6).map((r) => `• ${r.title}: ${r.detail}`).join("\n");
  const wf = (brief.workflow_steps || []).filter((s) => s.status === "pass").map((s) => `• ${s.step}. ${s.title}`).join("\n");
  const msg = [
    `Enter ${side} on ${document.getElementById("live-symbol-label")?.textContent || "XAUUSD"}?`,
    "",
    brief.headline || "",
    brief.entry_type ? `Entry type: ${brief.entry_type}` : "",
    "",
    "Workflow passed:",
    wf,
    "",
    why,
    "",
    `Entry: ${fmtNum(plan.entry_low, 2)} – ${fmtNum(plan.entry_high, 2)}`,
    `Stop: ${fmtNum(plan.stop, 2)}`,
    `Target: ${fmtNum(plan.tp1, 2)}`,
    `Grade: ${grading.letter || brief.final_grade || "?"}`,
    `Risk: ${((grading.suggested_risk_pct || brief.suggested_risk_pct || 0.01) * 100).toFixed(2)}% of equity`,
  ].join("\n");
  if (!confirm(msg)) return;

  try {
    const r = await postJSON("/api/ifvg/approve", {
      side: s.side,
      entry: plan.entry,
      stop: plan.stop,
      target: plan.tp1,
      tp1: plan.tp1,
      verdict: s.verdict,
      externally_blocked: s.externally_blocked && researchMode === "hard",
      risk_pct: grading.suggested_risk_pct || brief.suggested_risk_pct || 0.01,
      comment: `ifvg_m${LIVE.tf}`,
    });
    if (!r.ok) throw new Error(r.error || "order failed");
    const o = r.order || {};
    toast(`Live order placed · ${o.side} @ ${fmtNum(o.fill_price, 2)} · ticket ${o.broker_order_id}`);
    await refreshLiveSide();
  } catch (e) {
    toast(`Approve failed: ${e.message}`, "err");
  }
}

// ---------- Dashboard ----------------------------------------------------

async function loadDashboard() {
  const [s, st, cal, log, notes] = await Promise.all([
    fetchJSON("/api/summary"),
    fetchJSON("/api/stats"),
    fetchJSON("/api/calendar"),
    fetchJSON("/api/logs?n=80"),
    fetchJSON("/api/live/notes?n=120"),
  ]);

  // State cards
  const states = (s.states || []).map((x) => `
    <div class="card subtle">
      <h3>${escapeHTML(x.broker)} <span class="muted small">${escapeHTML(x.path)}</span></h3>
      <div class="kv-grid">
        <div><span>equity</span><span>${fmtNum(x.paper_equity, 2)}</span></div>
        <div><span>daily peak</span><span>${fmtNum(x.daily_peak_equity, 2)}</span></div>
        <div><span>total trades</span><span>${x.total_trades || 0}</span></div>
        <div><span>win rate</span><span>${fmtPct(x.win_rate || 0)}</span></div>
        <div><span>last update</span><span>${fmtTS(x.last_updated)}</span></div>
        <div><span>open</span><span>${x.open_position ? `${x.open_position.side || "?"} ${x.open_position.family || ""} @ ${fmtNum(x.open_position.entry_price, 2)}` : "—"}</span></div>
      </div>
    </div>`).join("");
  $("#state-cards").innerHTML = states || "<div class='muted'>no paper states</div>";

  // last50 stats
  const ls = (s.journal && s.journal.last50_stats) || {};
  $("#last50-stats").innerHTML = `
    <div><span>n</span><span>${ls.n || 0}</span></div>
    <div><span>win rate</span><span>${fmtPct(ls.wr || 0)}</span></div>
    <div><span>profit factor</span><span>${ls.pf === -1 ? "∞" : fmtNum(ls.pf, 2)}</span></div>
    <div><span>avg R</span><span>${fmtR(ls.avg_r)}</span></div>
    <div><span>total R</span><span>${fmtR(ls.total_r, 2)}</span></div>
    <div><span>journal total</span><span>${(s.journal && s.journal.total) || 0}</span></div>`;

  // Mini equity curve
  if (st.n > 0) {
    const r = await fetchJSON("/api/risk");
    drawEquityCurve("dash-equity", r.equity_curve || [], { compact: true });
  }

  // Upcoming events
  const ev = cal.upcoming || [];
  if (!ev.length) {
    $("#upcoming-events").innerHTML = "<div class='muted'>no upcoming events. Add some in <strong>Controls</strong>.</div>";
  } else {
    $("#upcoming-events").innerHTML = `<table>
      <thead><tr><th>when</th><th>event</th><th>impact</th><th>in</th></tr></thead>
      <tbody>${ev.map((e) => {
        const m = e.minutes_until;
        const cls = m < 0 ? "muted" : Math.abs(m) < 30 ? "verdict-block" : Math.abs(m) < 120 ? "verdict-warn" : "";
        const inLbl = m < 0 ? `${Math.round(-m)}m ago` : `in ${Math.round(m)}m`;
        return `<tr><td>${fmtTS(e.timestamp)}</td><td>${escapeHTML(e.event)}</td><td>${escapeHTML(e.impact)}</td><td class="${cls}">${inLbl}</td></tr>`;
      }).join("")}</tbody></table>`;
  }

  // Live special notes
  $("#dash-special-notes").innerHTML = renderCompactLogLines(renderLogNotes(notes.notes || []), 8);

  // Log tail
  $("#dash-log-tail").innerHTML = renderCompactLogLines(log.entries || log.lines || [], 10);
}

// ---------- Charts -------------------------------------------------------

let chartCandles = null;
let chartSeriesRefs = {};

async function initChartsTab() {
  if (!TAB_INIT.has("charts")) {
    const ds = await loadDatasets();
    populateDatasetSelect("#chart-dataset", ds);
    $("#btn-chart-load").addEventListener("click", loadChart);
    TAB_INIT.add("charts");
    await loadChart();
  }
}

function populateDatasetSelect(sel, datasets) {
  const opts = datasets.map((d) => `<option value="${escapeHTML(d.path)}">${escapeHTML(d.path)} (${(d.size / 1024).toFixed(0)}kb)</option>`).join("");
  $(sel).innerHTML = opts;
}

async function loadChart() {
  const path = $("#chart-dataset").value;
  const limit = $("#chart-limit").value;
  if (!path) return;
  $("#chart-meta").textContent = "loading…";
  const [c, ind] = await Promise.all([
    fetchJSON(`/api/candles?path=${encodeURIComponent(path)}&limit=${limit}`),
    fetchJSON(`/api/indicators?path=${encodeURIComponent(path)}&limit=${limit}`),
  ]);
  if (c.error) { $("#chart-meta").textContent = c.error; return; }
  $("#chart-meta").textContent = `${c.count} bars · ${fmtTS(c.first)} → ${fmtTS(c.last)}`;
  renderCandlesChart("chart-candles", "chart-volume", c.bars, ind);
}

function renderCandlesChart(candleId, volumeId, bars, ind) {
  const containerC = document.getElementById(candleId);
  const containerV = document.getElementById(volumeId);
  containerC.innerHTML = "";
  containerV.innerHTML = "";
  const lc = window.LightweightCharts;
  if (!lc) { containerC.textContent = "lightweight-charts failed to load"; return; }
  const chart = lc.createChart(containerC, {
    layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
    grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#232a35" },
    rightPriceScale: { borderColor: "#232a35" },
    crosshair: { mode: lc.CrosshairMode.Normal },
    autoSize: true,
  });
  const candles = chart.addCandlestickSeries({
    upColor: "#4ade80", downColor: "#f87171",
    borderUpColor: "#4ade80", borderDownColor: "#f87171",
    wickUpColor: "#4ade80", wickDownColor: "#f87171",
  });
  candles.setData(bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
  chartCandles = chart;
  chartSeriesRefs = { candles };

  const overlays = [
    ["ind-ema20", "ema20", "#f6c44a"],
    ["ind-ema50", "ema50", "#60a5fa"],
    ["ind-ema200", "ema200", "#a78bfa"],
    ["ind-vwap", "vwap", "#22d3ee"],
  ];
  overlays.forEach(([cb, key, color]) => {
    if ($("#" + cb).checked && ind[key] && ind[key].length) {
      const s = chart.addLineSeries({ color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      s.setData(ind[key]);
    }
  });

  // Volume
  const vChart = lc.createChart(containerV, {
    layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
    grid: { vertLines: { visible: false }, horzLines: { color: "#1c222b" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#232a35" },
    rightPriceScale: { borderColor: "#232a35" },
    autoSize: true,
  });
  const volSeries = vChart.addHistogramSeries({ priceFormat: { type: "volume" } });
  volSeries.setData(bars.map((b) => ({
    time: b.time, value: b.volume,
    color: b.close >= b.open ? "rgba(74, 222, 128, 0.4)" : "rgba(248, 113, 113, 0.4)",
  })));
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && vChart.timeScale().setVisibleLogicalRange(r));
  vChart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && chart.timeScale().setVisibleLogicalRange(r));
}

// ---------- Bridge -------------------------------------------------------

async function loadBridge() {
  if (!TAB_INIT.has("bridge")) {
    $("#btn-bridge-refresh").addEventListener("click", loadBridge);
    TAB_INIT.add("bridge");
  }
  const b = await fetchJSON("/api/bridge/status");
  $("#bridge-url").textContent = `URL: ${b.url}`;
  const s = $("#bridge-status");
  s.innerHTML = `
    <div><span>online</span><span class="${b.online ? "verdict-allow" : "verdict-block"}">${b.online ? "YES" : "no"}</span></div>
    <div><span>healthz</span><span>${b.healthz ? escapeHTML(JSON.stringify(b.healthz)) : "—"}</span></div>
    <div><span>error</span><span>${b.error ? escapeHTML(b.error) : "—"}</span></div>`;
  if (b.account) {
    const a = b.account;
    $("#bridge-account").innerHTML = `
      <div><span>equity</span><span>${fmtNum(a.equity, 2)} ${escapeHTML(a.currency)}</span></div>
      <div><span>balance</span><span>${fmtNum(a.balance, 2)}</span></div>
      <div><span>margin used</span><span>${fmtNum(a.margin_used, 2)}</span></div>
      <div><span>margin free</span><span>${fmtNum(a.margin_free, 2)}</span></div>
      <div><span>leverage</span><span>${fmtNum(a.leverage, 0)}</span></div>`;
  } else {
    $("#bridge-account").innerHTML = `<div class="muted">${b.account_error ? escapeHTML(b.account_error) : "not connected"}</div>`;
  }
  const pos = b.open_position;
  if (pos) {
    $("#bridge-position").innerHTML = `
      <div class="kv-grid">
        <div><span>order id</span><span>${escapeHTML(pos.broker_order_id)}</span></div>
        <div><span>symbol</span><span>${escapeHTML(pos.symbol)}</span></div>
        <div><span>side</span><span>${escapeHTML(pos.side)}</span></div>
        <div><span>units</span><span>${fmtNum(pos.units, 2)}</span></div>
        <div><span>entry</span><span>${fmtNum(pos.entry_price, 2)}</span></div>
        <div><span>stop</span><span>${fmtNum(pos.stop_price, 2)}</span></div>
        <div><span>target</span><span>${fmtNum(pos.target_price, 2)}</span></div>
        <div><span>opened</span><span>${fmtTS(pos.opened_at)}</span></div>
        <div><span>unrealised</span><span>${fmtNum(pos.unrealised_pnl, 2)}</span></div>
      </div>
      <div class="row" style="margin-top:12px;">
        <button class="danger" id="btn-bridge-close">Close position</button>
      </div>`;
    $("#btn-bridge-close").addEventListener("click", async () => {
      if (!confirm(`Close ${pos.side} ${pos.symbol} @ market?`)) return;
      try {
        const r = await postJSON("/api/bridge/close", { broker_order_id: pos.broker_order_id, reason: "manual_ui" });
        toast(r.ok ? `closed @ ${r.closed.exit_price}` : `failed: ${r.error}`, r.ok ? "ok" : "err");
        loadBridge();
      } catch (e) { toast(e.message, "err"); }
    });
  } else {
    $("#bridge-position").innerHTML = `<div class="muted">no open position</div>`;
  }
}

// ---------- Lab ----------------------------------------------------------

async function loadLab() {
  if (!TAB_INIT.has("lab")) {
    const [ds, fams] = await Promise.all([loadDatasets(), loadFamilies()]);
    populateDatasetSelect("#lab-dataset", ds);
    $("#lab-family").innerHTML = fams.map((f) => `<option>${f}</option>`).join("");
    $("#btn-lab-holdout").addEventListener("click", () => runLabJob("/api/lab/holdout"));
    $("#btn-lab-perm").addEventListener("click", () => runLabJob("/api/lab/permutation"));
    TAB_INIT.add("lab");
  }
}

async function runLabJob(endpoint) {
  const body = {
    path: $("#lab-dataset").value,
    family: $("#lab-family").value,
    n_permutations: Number($("#lab-nperm").value),
    holdout_fraction: Number($("#lab-holdout-frac").value),
  };
  $("#lab-status").textContent = "starting…";
  $("#lab-output").textContent = "";
  try {
    const r = await postJSON(endpoint, body);
    if (!r.ok) throw new Error(r.error || "failed to start");
    $("#lab-status").textContent = `running ${r.job_id}…`;
    streamJobOutput(r.job_id, "lab-output", "lab-status");
  } catch (e) {
    $("#lab-status").textContent = e.message;
    toast(e.message, "err");
  }
}

async function streamJobOutput(jobId, outputId, statusId) {
  for (let i = 0; i < 360; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const j = await fetchJSON(`/api/jobs?id=${encodeURIComponent(jobId)}`);
      if (j.output) $("#" + outputId).textContent = j.output;
      if (j.status === "done" || j.status === "failed" || j.status === "error" || j.status === "timeout") {
        $("#" + statusId).textContent = `${j.status} (exit=${j.exit_code ?? "—"})`;
        return;
      }
    } catch (e) { /* keep going */ }
  }
}

// ---------- Miner --------------------------------------------------------

async function loadMiner() {
  if (!TAB_INIT.has("miner")) {
    const ds = await loadDatasets();
    populateDatasetSelect("#miner-dataset", ds);
    $("#btn-miner-run").addEventListener("click", runMiner);
    $("#btn-miner-refresh").addEventListener("click", loadMinerRuns);
    TAB_INIT.add("miner");
  }
  await loadMinerRuns();
}

async function runMiner() {
  const body = {
    path: $("#miner-dataset").value,
    timeframes: $("#miner-tfs").value,
    horizons: $("#miner-horizons").value,
    max_combo_size: Number($("#miner-combo").value),
    min_signals: Number($("#miner-minsig").value),
    min_effect_r: Number($("#miner-effect").value),
    with_macro: $("#miner-macro").checked,
  };
  try {
    const r = await postJSON("/api/miner/run", body);
    if (!r.ok) throw new Error(r.error || "failed");
    toast(`miner started (${r.job_id})`);
    streamJobOutput(r.job_id, "miner-job", "miner-job");
    setTimeout(loadMinerRuns, 5000);
  } catch (e) { toast(e.message, "err"); }
}

async function loadMinerRuns() {
  const r = await fetchJSON("/api/miner/results");
  const runs = r.runs || [];
  if (!runs.length) {
    $("#miner-runs").innerHTML = "<div class='muted'>no runs yet</div>";
    return;
  }
  $("#miner-runs").innerHTML = `<table>
    <thead><tr><th>run</th><th>survivors</th><th>cross-tf</th><th>updated</th><th></th></tr></thead>
    <tbody>${runs.map((x) => `
      <tr>
        <td>${escapeHTML(x.name)}</td>
        <td class="num">${(x["all_survivors.csv"] && x["all_survivors.csv"].count) || 0}</td>
        <td class="num">${(x["cross_tf_replicators.csv"] && x["cross_tf_replicators.csv"].count) || 0}</td>
        <td>${fmtTS(x.mtime)}</td>
        <td><button class="small" data-dir="${escapeHTML(x.path)}">open</button></td>
      </tr>`).join("")}</tbody></table>`;
  $$("#miner-runs button[data-dir]").forEach((btn) => {
    btn.addEventListener("click", () => loadSurvivors(btn.dataset.dir));
  });
}

async function loadSurvivors(dir) {
  $("#survivors-meta").textContent = `loading ${dir}…`;
  const r = await fetchJSON(`/api/miner/survivors?dir=${encodeURIComponent(dir)}&limit=200&sort=effect_r`);
  if (r.error) { $("#survivors-meta").textContent = r.error; return; }
  $("#survivors-meta").textContent = `${r.file} — ${r.count} rows (top 200 by |effect_r|)`;
  const rows = r.rows || [];
  if (!rows.length) { $("#survivors-table").innerHTML = "<div class='muted'>empty</div>"; return; }
  const cols = Object.keys(rows[0]);
  $("#survivors-table").innerHTML = `<table>
    <thead><tr>${cols.map((c) => `<th>${escapeHTML(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((x) => `<tr>${cols.map((c) => {
      const v = x[c];
      const isNum = !isNaN(Number(v)) && v !== "" && v !== null;
      return `<td class="${isNum ? "num" : ""}">${escapeHTML(v)}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table>`;
}

// ---------- Macro --------------------------------------------------------

let chartMacro = null;

async function loadMacro() {
  if (!TAB_INIT.has("macro")) {
    $("#btn-macro-load").addEventListener("click", loadMacroSeries);
    $("#btn-macro-refresh").addEventListener("click", loadMacroList);
    $("#btn-macro-sync").addEventListener("click", async () => {
      try {
        const r = await postJSON("/api/sync-macro", {});
        toast(`sync started (${r.job_id})`);
        pollJob(r.job_id, () => { loadMacroList(); toast("sync done"); });
      } catch (e) { toast(e.message, "err"); }
    });
    TAB_INIT.add("macro");
  }
  await loadMacroList();
}

async function loadMacroList() {
  const r = await fetchJSON("/api/macro/list");
  const series = r.series || [];
  $("#macro-name").innerHTML = series.map((s) => `<option value="${escapeHTML(s.name)}">${escapeHTML(s.name)} (${s.rows})</option>`).join("");
  $("#macro-list").innerHTML = `<table>
    <thead><tr><th>name</th><th>rows</th><th>first</th><th>last</th></tr></thead>
    <tbody>${series.map((s) => `<tr><td>${escapeHTML(s.name)}</td><td class="num">${s.rows}</td><td>${escapeHTML(s.first || "—")}</td><td>${escapeHTML(s.last || "—")}</td></tr>`).join("")}</tbody></table>`;
  if (series.length) await loadMacroSeries();
}

async function loadMacroSeries() {
  const name = $("#macro-name").value;
  if (!name) return;
  const r = await fetchJSON(`/api/macro/series?name=${encodeURIComponent(name)}`);
  if (r.error) { $("#macro-meta").textContent = r.error; return; }
  $("#macro-meta").textContent = `${r.count} rows`;
  const container = $("#chart-macro");
  container.innerHTML = "";
  const lc = window.LightweightCharts;
  chartMacro = lc.createChart(container, {
    layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
    grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
    timeScale: { timeVisible: false, borderColor: "#232a35" },
    rightPriceScale: { borderColor: "#232a35" },
    autoSize: true,
  });
  const series = chartMacro.addLineSeries({ color: "#f6c44a", lineWidth: 2 });
  series.setData(r.rows.map((x) => ({
    time: x.date,
    value: x.value,
  })));
}

// ---------- Journal -----------------------------------------------------

async function loadJournal() {
  if (!TAB_INIT.has("journal")) {
    const fams = await loadFamilies();
    $("#jrn-family").innerHTML = `<option value="">all</option>` + fams.map((f) => `<option>${f}</option>`).join("");
    $("#btn-jrn-load").addEventListener("click", loadJournalRows);
    TAB_INIT.add("journal");
  }
  await loadJournalRows();
  await loadJournalBuckets();
}

async function loadJournalRows() {
  const params = new URLSearchParams();
  ["verdict", "family", "side", "limit"].forEach((k) => {
    const v = $("#jrn-" + k).value;
    if (v) params.set(k, v);
  });
  const r = await fetchJSON("/api/journal?" + params.toString());
  $("#jrn-stats").textContent = `${r.count} rows`;
  const rows = r.rows || [];
  if (!rows.length) { $("#journal-table").innerHTML = "<div class='muted'>no rows</div>"; return; }
  const cols = ["closed_at", "family", "tf", "side", "filter_verdict", "regime_trend", "regime_session_vwap", "regime_macro_real10y", "regime_macro_dxy", "exit_reason", "expected_r", "realised_r", "drift_r"];
  $("#journal-table").innerHTML = `<table>
    <thead><tr>${cols.map((c) => `<th>${escapeHTML(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice().reverse().map((x) => `<tr>${cols.map((c) => {
      const v = x[c] || "";
      let cls = "";
      if (c === "filter_verdict") cls = v === "block" ? "verdict-block" : v.startsWith("allow") ? "verdict-allow" : "";
      if (c === "realised_r" || c === "expected_r" || c === "drift_r") cls = "num " + classOfR(v);
      return `<td class="${cls}">${escapeHTML(v)}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table>`;
}

async function loadJournalBuckets() {
  const s = await fetchJSON("/api/stats");
  if (!s.n) { $("#journal-buckets").innerHTML = "<div class='muted'>no journal yet</div>"; return; }
  let html = "";
  if (s.filter_lift) {
    const f = s.filter_lift;
    html += `<h3>Filter lift</h3>
      <div class="kv-grid">
        <div><span>allow n</span><span>${f.allow_n}</span></div>
        <div><span>allow avg R</span><span class="${classOfR(f.allow_avg_r)}">${fmtR(f.allow_avg_r)}</span></div>
        <div><span>block n</span><span>${f.block_n}</span></div>
        <div><span>block avg R</span><span class="${classOfR(f.block_avg_r)}">${fmtR(f.block_avg_r)}</span></div>
        <div><span>delta</span><span class="${classOfR(f.delta_avg_r)}">${fmtR(f.delta_avg_r)}</span></div>
        <div><span>promote → hard</span><span>${s.promote_filter_to_hard ? "✓" : "—"}</span></div>
      </div>`;
  }
  for (const [reg, buckets] of Object.entries(s.by_regime || {})) {
    if (!Object.keys(buckets).length) continue;
    html += `<h3>${escapeHTML(reg)}</h3>
      <table><thead><tr><th>bucket</th><th>n</th><th>win rate</th><th>avg R</th><th>total R</th></tr></thead><tbody>`;
    for (const [k, v] of Object.entries(buckets)) {
      html += `<tr><td>${escapeHTML(k)}</td><td class="num">${v.n}</td><td class="num">${fmtPct(v.wr)}</td><td class="num ${classOfR(v.avg_r)}">${fmtR(v.avg_r)}</td><td class="num ${classOfR(v.total_r)}">${fmtR(v.total_r, 2)}</td></tr>`;
    }
    html += "</tbody></table>";
  }
  if (s.exit_reason_mix) {
    html += `<h3>Exit reason mix</h3><div class="bar-list">`;
    const total = Object.values(s.exit_reason_mix).reduce((a, b) => a + b, 0);
    for (const [k, n] of Object.entries(s.exit_reason_mix)) {
      const pct = total ? (n / total) * 100 : 0;
      html += `<div class="bar"><span class="bar-label">${escapeHTML(k)}</span><span class="bar-fill"><span style="width:${pct.toFixed(1)}%"></span></span><span class="bar-val">${n}</span></div>`;
    }
    html += `</div>`;
  }
  $("#journal-buckets").innerHTML = html || "<div class='muted'>no buckets</div>";
}

// ---------- Performance --------------------------------------------------

async function loadPerformance() {
  if (!TAB_INIT.has("performance")) {
    TAB_INIT.add("performance");
  }
  let p = {};
  try { p = await fetchJSON("/api/performance"); } catch (e) { p = {}; }
  let s = { rows: [] };
  try { s = await fetchJSON("/api/paper-signals?limit=20"); } catch (e) { s = { rows: [] }; }

  // metrics
  if (!p || p.error) {
    $("#perf-metrics").innerHTML = '<div class="muted">No performance data yet.</div>';
  } else {
    const html = `
      <div><span>Total signals</span><b>${p.total_signals||0}</b></div>
      <div><span>Open signals</span><b>${p.open_signals||0}</b></div>
      <div><span>Closed signals</span><b>${p.closed_signals||0}</b></div>
      <div><span>TP1 hit rate</span><b>${(p.tp1_hit_rate||0).toFixed(1)}%</b></div>
      <div><span>TP2 hit rate</span><b>${(p.tp2_hit_rate||0).toFixed(1)}%</b></div>
      <div><span>TP3 hit rate</span><b>${(p.tp3_hit_rate||0).toFixed(1)}%</b></div>
      <div><span>SL hit rate</span><b>${(p.sl_hit_rate||0).toFixed(1)}%</b></div>
      <div><span>Avg MFE R</span><b>${(p.average_max_favorable_r||0).toFixed(3)}</b></div>
      <div><span>Avg MAE R</span><b>${(p.average_max_adverse_r||0).toFixed(3)}</b></div>
      <div><span>Expectancy</span><b>${(p.expectancy_r||0).toFixed(3)}</b></div>
    `;
    $("#perf-metrics").innerHTML = html;
  }

  // latest signals
  const rows = (s && s.rows) || [];
  if (!rows.length) {
    $("#perf-signals").innerHTML = '<div class="muted">No paper signals yet.</div>';
  } else {
    let html = '<table><thead><tr><th>Time</th><th>Action</th><th>Grade</th><th>Score</th><th>Side</th><th>Price</th><th>Status</th></tr></thead><tbody>';
    rows.slice().reverse().forEach((r) => {
      html += `<tr><td>${escapeHTML(r.opened_at||r.timestamp_utc||'')}</td><td>${escapeHTML(r.action||'')}</td><td>${escapeHTML(r.grade||'')}</td><td class="num ${classOfR(r.realised_r||r.score)}">${escapeHTML(String(r.score||r.realised_r||''))}</td><td>${escapeHTML(r.side||'')}</td><td>${escapeHTML(String(r.entry||r.current_price||''))}</td><td>${escapeHTML(r.status||r.exit_reason||'')}</td></tr>`;
    });
    html += '</tbody></table>';
    $("#perf-signals").innerHTML = html;
  }
}

// ---------- Risk --------------------------------------------------------

async function loadRisk() {
  const r = await fetchJSON("/api/risk");
  drawEquityCurve("chart-equity", r.equity_curve || []);
  $("#risk-summary").innerHTML = `
    <div><span>n trades</span><span>${r.n_trades}</span></div>
    <div><span>total R</span><span class="${classOfR(r.total_r)}">${fmtR(r.total_r, 2)}</span></div>
    <div><span>max drawdown R</span><span class="neg">${fmtR(-Math.abs(r.max_drawdown_r), 2)}</span></div>
    <div><span>current drawdown</span><span>${fmtR(-Math.abs(r.current_drawdown_r), 2)}</span></div>`;

  const tpd = r.trades_per_day || {};
  const max = Math.max(1, ...Object.values(tpd));
  $("#trades-per-day").innerHTML = Object.entries(tpd).map(([d, n]) =>
    `<div class="bar"><span class="bar-label">${escapeHTML(d)}</span><span class="bar-fill"><span style="width:${(n / max * 100).toFixed(1)}%"></span></span><span class="bar-val">${n}</span></div>`
  ).join("") || "<div class='muted'>no trades</div>";

  const ks = r.paper_states || [];
  $("#kill-switches").innerHTML = ks.map((p) => `
    <div class="card subtle">
      <h3>${escapeHTML(p.broker)}</h3>
      <div class="kv-grid">
        <div><span>equity</span><span>${fmtNum(p.equity, 2)}</span></div>
        <div><span>kill switch</span><span class="${p.kill_switch ? "verdict-block" : "verdict-allow"}">${p.kill_switch ? "ARMED" : "ok"}</span></div>
        <div><span>reason</span><span>${escapeHTML(p.kill_reason || "—")}</span></div>
        <div><span>open position</span><span>${p.open_position ? "yes" : "—"}</span></div>
      </div>
      <div class="row">
        <button class="danger" data-broker="${escapeHTML(p.broker)}" data-action="arm">Arm kill</button>
        <button data-broker="${escapeHTML(p.broker)}" data-action="disarm">Disarm</button>
      </div>
    </div>`).join("");
  $$("#kill-switches button[data-action]").forEach((b) => b.addEventListener("click", async () => {
    const arm = b.dataset.action === "arm";
    if (arm && !confirm(`Arm kill switch for ${b.dataset.broker}?`)) return;
    try {
      const r2 = await postJSON("/api/risk/kill", { broker: b.dataset.broker, enabled: arm, reason: arm ? "manual_ui" : "" });
      toast(`kill ${arm ? "armed" : "disarmed"}: ${r2.affected.join(",")}`, "ok");
      loadRisk();
    } catch (e) { toast(e.message, "err"); }
  }));
}

function drawEquityCurve(containerId, points, opts = {}) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!points.length) { container.innerHTML = "<div class='muted' style='padding:20px;'>no trades</div>"; return; }
  const lc = window.LightweightCharts;
  const chart = lc.createChart(container, {
    layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
    grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
    timeScale: { timeVisible: false, borderColor: "#232a35", visible: !opts.compact },
    rightPriceScale: { borderColor: "#232a35" },
    autoSize: true,
  });
  const series = chart.addAreaSeries({
    lineColor: "#f6c44a", topColor: "rgba(246, 196, 74, 0.4)", bottomColor: "rgba(246, 196, 74, 0.0)",
    lineWidth: 2,
  });
  // synthesize timestamps from i so chart sorts consistently
  const start = Math.floor(Date.now() / 1000) - points.length * 3600;
  series.setData(points.map((p, i) => {
    let t;
    if (p.closed_at) {
      try { t = Math.floor(new Date(p.closed_at).getTime() / 1000); } catch { t = start + i * 3600; }
    } else { t = start + i * 3600; }
    return { time: t, value: p.cum_r };
  }));
}

// ---------- Replay -------------------------------------------------------

async function loadReplay() {
  if (!TAB_INIT.has("replay")) {
    const ds = await loadDatasets();
    populateDatasetSelect("#replay-dataset", ds);
    // pre-fill date with last available
    $("#replay-date").value = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    $("#btn-replay-load").addEventListener("click", runReplay);
    TAB_INIT.add("replay");
  }
}

async function runReplay() {
  const path = $("#replay-dataset").value;
  const date = $("#replay-date").value;
  if (!path || !date) { toast("pick dataset + date", "err"); return; }
  const r = await fetchJSON(`/api/replay?path=${encodeURIComponent(path)}&date=${encodeURIComponent(date)}`);
  if (r.error) { $("#replay-meta").textContent = r.error; return; }
  $("#replay-meta").textContent = `${r.count} bars · ${(r.trades || []).length} trades on ${date}`;
  const container = $("#chart-replay");
  container.innerHTML = "";
  const lc = window.LightweightCharts;
  const chart = lc.createChart(container, {
    layout: { background: { color: "#0b0e13" }, textColor: "#c5cbd5" },
    grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#232a35" },
    rightPriceScale: { borderColor: "#232a35" },
    autoSize: true,
  });
  const candles = chart.addCandlestickSeries({
    upColor: "#4ade80", downColor: "#f87171",
    borderUpColor: "#4ade80", borderDownColor: "#f87171",
    wickUpColor: "#4ade80", wickDownColor: "#f87171",
  });
  candles.setData((r.bars || []).map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));

  // Overlay trades as price lines and markers
  const markers = [];
  (r.trades || []).forEach((t) => {
    const isLong = (t.side || "").toLowerCase().includes("long");
    if (t.opened_at) {
      const tm = Math.floor(new Date(t.opened_at).getTime() / 1000);
      markers.push({ time: tm, position: isLong ? "belowBar" : "aboveBar",
        color: isLong ? "#4ade80" : "#f87171", shape: isLong ? "arrowUp" : "arrowDown",
        text: `${t.family} ${t.side}` });
    }
    if (t.closed_at) {
      const tc = Math.floor(new Date(t.closed_at).getTime() / 1000);
      markers.push({ time: tc, position: "inBar", color: "#facc15", shape: "circle",
        text: `${t.exit_reason} ${fmtR(t.realised_r)}` });
    }
  });
  if (markers.length) candles.setMarkers(markers.sort((a, b) => a.time - b.time));

  // Trade table
  const trades = r.trades || [];
  if (!trades.length) {
    $("#replay-trades").innerHTML = "<div class='muted'>no trades on this day</div>";
  } else {
    $("#replay-trades").innerHTML = `<table>
      <thead><tr><th>opened</th><th>family</th><th>side</th><th>entry</th><th>stop</th><th>target</th><th>exit</th><th>reason</th><th>R</th><th>verdict</th></tr></thead>
      <tbody>${trades.map((t) => `
        <tr>
          <td>${fmtTS(t.opened_at)}</td>
          <td>${escapeHTML(t.family)}</td>
          <td>${escapeHTML(t.side)}</td>
          <td class="num">${fmtNum(t.entry, 2)}</td>
          <td class="num">${fmtNum(t.stop, 2)}</td>
          <td class="num">${fmtNum(t.target, 2)}</td>
          <td class="num">${fmtNum(t.exit_price, 2)}</td>
          <td>${escapeHTML(t.exit_reason)}</td>
          <td class="num ${classOfR(t.realised_r)}">${fmtR(t.realised_r)}</td>
          <td class="${t.filter_verdict === 'block' ? 'verdict-block' : 'verdict-allow'}">${escapeHTML(t.filter_verdict)}</td>
        </tr>`).join("")}</tbody></table>`;
  }
}

// ---------- Logs --------------------------------------------------------

let logTimer = null;
let logFiles = [];

const LOG_CATEGORY_ORDER = ["live", "agent", "connection", "journal", "research", "other"];
const LOG_CATEGORY_LABELS = {
  live: "Live trading",
  agent: "Agent decisions",
  connection: "Connection",
  journal: "Journal",
  research: "Research",
  other: "Other",
};

function preferredLogForCategory(key, files) {
  const names = files.map((f) => f.name);
  const priority = {
    live: ["live_trade_watch.log", "live_monitor.log"],
    agent: ["agent.log", "events.jsonl", "gold_trader.jsonl"],
    connection: ["web.log", "bridge.log"],
    journal: ["trade_journal.csv", "macro_paper_journal.csv", "mtf_paper_journal.csv"],
    research: ["premium_audit_60m.log", "macro_audit_60m.log", "champion.log", "mtf_full_validation.log"],
  }[key] || [];
  return priority.find((name) => names.includes(name)) || (files.find((f) => f.category_key === key) || {}).name || "";
}

function renderLogCategoryHub(files) {
  const groups = new Map();
  files.forEach((file) => {
    const key = file.category_key || "other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(file);
  });
  const selected = $("#log-file").value;
  $("#log-category-hub").innerHTML = LOG_CATEGORY_ORDER.filter((key) => groups.has(key)).map((key) => {
    const items = groups.get(key);
    const pick = preferredLogForCategory(key, files);
    const active = items.some((file) => file.name === selected) ? " active" : "";
    const desc = (items[0] && items[0].description) || "Supporting log files.";
    const count = items.length === 1 ? "1 file" : `${items.length} files`;
    return `<button class="log-category-card${active}" data-log-pick="${escapeHTML(pick)}">
      <strong>${escapeHTML(LOG_CATEGORY_LABELS[key] || key)}</strong>
      <span>${escapeHTML(desc)}</span>
      <small>${escapeHTML(pick || "no file")} · ${count}</small>
    </button>`;
  }).join("");
  $$("#log-category-hub [data-log-pick]").forEach((btn) => btn.addEventListener("click", async () => {
    const pick = btn.dataset.logPick;
    if (!pick) return;
    $("#log-file").value = pick;
    renderLogCategoryHub(logFiles);
    await refreshLog();
  }));
}

function selectedLogHelp() {
  const f = logFiles.find((file) => file.name === $("#log-file").value);
  if (!f) return "";
  return `${f.category || "Log"}: ${f.description || "Supporting log file."}`;
}

async function loadLogs() {
  if (!TAB_INIT.has("logs")) {
    const r = await fetchJSON("/api/logs/list");
    logFiles = r.files || [];
    $("#log-file").innerHTML = logFiles.map((f) => `<option value="${escapeHTML(f.name)}">${escapeHTML(f.category || "Other")} · ${escapeHTML(f.name)} (${(f.size / 1024).toFixed(0)}kb)</option>`).join("");
    const names = logFiles.map((f) => f.name);
    if (names.includes("live_trade_watch.log")) $("#log-file").value = "live_trade_watch.log";
    else if (names.includes("agent.log")) $("#log-file").value = "agent.log";
    $("#btn-log-load").addEventListener("click", refreshLog);
    $("#btn-log-tail").addEventListener("click", toggleLogTail);
    $("#log-file").addEventListener("change", () => { renderLogCategoryHub(logFiles); refreshLog(); });
    renderLogCategoryHub(logFiles);
    TAB_INIT.add("logs");
  }
  await refreshLog();
}

async function refreshLog() {
  const f = $("#log-file").value;
  const n = $("#log-n").value;
  const r = await fetchJSON(`/api/logs?file=${encodeURIComponent(f)}&n=${n}`);
  const rows = r.entries || r.lines || [];
  $("#log-meta").textContent = `${r.file} · ${rows.length} lines · ${selectedLogHelp()}`;
  renderLogCategoryHub(logFiles);
  const entries = recentFirst(entryPointLines(rows)).slice(0, 8);
  $("#entry-log-strip").innerHTML = entries.length
    ? entries.map((line) => `<div class="entry-chip"><span>${escapeHTML(logTimestamp(line))}</span><strong>${escapeHTML(logActionSummary(line) || "ENTRY")}</strong>${escapeHTML(logLineText(line))}</div>`).join("")
    : "<div class='muted small'>No entry alerts in this log window.</div>";
  $("#log-body").innerHTML = renderLogLines(rows);
  $("#log-body").scrollTop = 0;
}

function toggleLogTail() {
  if (logTimer) {
    clearInterval(logTimer); logTimer = null;
    $("#btn-log-tail").textContent = "Auto-refresh";
  } else {
    logTimer = setInterval(refreshLog, 3000);
    $("#btn-log-tail").textContent = "Stop auto-refresh";
  }
}

// ---------- Controls + Calendar -----------------------------------------

function renderSecretsStatus(secrets) {
  const s = secrets || {};
  const openaiEl = $("#cfg-openai-status");
  if (openaiEl) {
    const hint = s.openai_api_key_hint ? `saved ${s.openai_api_key_hint}` : "not set";
    const src = s.openai_api_key_from_env ? " · from shell env (overrides file)" : "";
    openaiEl.textContent = s.openai_api_key_set ? `(${hint}${src})` : `(not set — add in Settings once)`;
  }
  const bridgeEl = $("#cfg-secret-status");
  if (bridgeEl) {
    const hint = s.bridge_secret_hint ? `saved ${s.bridge_secret_hint}` : "not set";
    const src = s.bridge_secret_from_env ? " · from shell env (overrides file)" : "";
    bridgeEl.textContent = s.bridge_secret_set ? `(${hint}${src})` : "(not set)";
  }
}

async function loadControls() {
  if (!TAB_INIT.has("controls")) {
    $("#btn-cfg-save").addEventListener("click", saveConfig);
    const clearBtn = $("#btn-clear-secrets");
    if (clearBtn) clearBtn.addEventListener("click", clearSavedSecrets);
    $("#btn-sync-macro").addEventListener("click", async () => {
      try {
        const r = await postJSON("/api/sync-macro", {});
        toast(`sync-macro started (${r.job_id})`);
        pollJob(r.job_id, () => toast("sync done"));
      } catch (e) { toast(e.message, "err"); }
    });
    $("#btn-cal-add").addEventListener("click", addCalendarEvent);
    TAB_INIT.add("controls");
  }
  const s = await fetchJSON("/api/summary");
  const cfg = s.config || {};
  $("#cfg-mode").value = cfg.macro_filter_mode || "soft";
  $("#cfg-auto").checked = !!cfg.auto_trade_enabled;
  $("#cfg-news-min").value = cfg.news_blackout_min || 0;
  $("#cfg-notes").value = cfg.notes || "";
  if ($("#cfg-bridge-url")) $("#cfg-bridge-url").value = cfg.bridge_url || "";
  if ($("#cfg-symbol")) $("#cfg-symbol").value = cfg.symbol || "";
  renderSecretsStatus(s.secrets);
  await loadCalendar();
}

async function clearSavedSecrets() {
  if (!confirm("Clear saved OpenAI and bridge keys from config/secrets.json?\nShell env vars (if set) still override.")) return;
  try {
    const r = await postJSON("/api/secrets", {
      clear_openai_api_key: true,
      clear_bridge_secret: true,
    });
    renderSecretsStatus(r.secrets);
    const openai = $("#cfg-openai-key");
    const sec = $("#cfg-bridge-secret");
    if (openai) openai.value = "";
    if (sec) sec.value = "";
    toast("Saved keys cleared — paste new keys when ready");
  } catch (e) { toast(e.message, "err"); }
}

async function saveConfig() {
  const body = {
    macro_filter_mode: $("#cfg-mode").value,
    auto_trade_enabled: $("#cfg-auto").checked,
    news_blackout_min: Number($("#cfg-news-min").value),
    notes: $("#cfg-notes").value,
  };
  const url = $("#cfg-bridge-url");
  if (url && url.value.trim()) body.bridge_url = url.value.trim();
  const sym = $("#cfg-symbol");
  if (sym && sym.value.trim()) body.symbol = sym.value.trim();
  const sec = $("#cfg-bridge-secret");
  if (sec && sec.value) body.bridge_secret = sec.value;
  const openai = $("#cfg-openai-key");
  if (openai && openai.value.trim()) body.openai_api_key = openai.value.trim();
  try {
    let secretsPayload = {};
    if (body.openai_api_key) {
      secretsPayload.openai_api_key = body.openai_api_key;
    }
    if (body.bridge_secret) {
      secretsPayload.bridge_secret = body.bridge_secret;
    }
    const cfgBody = { ...body };
    delete cfgBody.openai_api_key;
    const r = await postJSON("/api/config", cfgBody);
    if (Object.keys(secretsPayload).length) {
      const sr = await postJSON("/api/secrets", secretsPayload);
      renderSecretsStatus(sr.secrets);
    } else if (r.secrets) {
      renderSecretsStatus(r.secrets);
    }
    $("#cfg-status").textContent = "saved.";
    if (sec) sec.value = "";
    if (openai) openai.value = "";
    toast("config saved");
    refreshLiveBanner();
  } catch (e) { toast(e.message, "err"); }
}

async function loadCalendar() {
  const r = await fetchJSON("/api/calendar");
  const ev = r.upcoming || [];
  if (!ev.length) {
    $("#calendar-list").innerHTML = "<div class='muted'>no upcoming events</div>";
    return;
  }
  $("#calendar-list").innerHTML = `<table>
    <thead><tr><th>timestamp</th><th>event</th><th>impact</th><th>in</th><th></th></tr></thead>
    <tbody>${ev.map((e) => `<tr>
      <td>${fmtTS(e.timestamp)}</td>
      <td>${escapeHTML(e.event)}</td>
      <td>${escapeHTML(e.impact)}</td>
      <td>${e.minutes_until < 0 ? "passed" : `in ${Math.round(e.minutes_until)}m`}</td>
      <td><button class="small" data-ts="${escapeHTML(e.timestamp)}">delete</button></td>
    </tr>`).join("")}</tbody></table>`;
  $$("#calendar-list button[data-ts]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Delete event at ${b.dataset.ts}?`)) return;
    try {
      await postJSON("/api/calendar/delete", { timestamp: b.dataset.ts });
      loadCalendar();
    } catch (e) { toast(e.message, "err"); }
  }));
}

async function addCalendarEvent() {
  const ts = $("#cal-ts").value;
  const event = $("#cal-event").value.trim();
  const impact = $("#cal-impact").value;
  if (!ts || !event) { toast("timestamp + event required", "err"); return; }
  // datetime-local has no timezone; treat as UTC
  const isoUtc = ts.length === 16 ? ts + ":00Z" : ts + "Z";
  try {
    const r = await postJSON("/api/calendar/add", { timestamp: isoUtc, event, impact });
    if (!r.ok) throw new Error(r.error);
    toast("event added");
    $("#cal-event").value = "";
    loadCalendar();
  } catch (e) { toast(e.message, "err"); }
}

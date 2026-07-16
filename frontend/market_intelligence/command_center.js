/* Gold Trader — Command Center (operator cockpit)
 * Easy on the surface, complicated underneath:
 *  - one glanceable ENTER / WAIT hero from the corrected IFVG scout (live MT5)
 *  - trade ticket with $30-risk lot sizing + 5R routed target
 *  - candle chart with entry / stop / target overlays
 *  - all engine detail (timeframe alignment, provider health, JSON, journal)
 *    tucked behind a single "Engine details" toggle.
 * Reads: /api/decision, /api/candles, /api/performance, /api/provider-health.
 */
(function () {
  "use strict";

  var TF_LIST = ["H4", "H1", "M15", "M5"];
  var REFRESH_MS = 10000;
  var state = {
    tf: "H1",
    decision: null,
    decisionErr: null,
    candles: [],
    candleMeta: {},
    perf: null,
    health: null,
    showAdv: false,
    advTab: "alignment",
    toast: null,
  };

  /* ---------- helpers ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function num(v) { var n = Number(v); return Number.isFinite(n) ? n : null; }
  function fmtPx(v) { var n = num(v); return n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function getJSON(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status);
      return r.json();
    });
  }

  /* ---------- styles (injected; namespaced gt-) ---------- */
  function injectStyles() {
    if (document.getElementById("gt-cc-style")) return;
    var css = [
      ":root{--bg:#05080c;--card:#0e1622;--card2:#0b121c;--line:#1b2a3b;--ink:#eaf1fb;--mut:#8a99ad;--gold:#f7c948;--green:#1fd18a;--red:#ff5d77;--amber:#f5b942;--blue:#6aa8ff}",
      "*{box-sizing:border-box}",
      "body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#142233 0,#070b11 45%,#04060a 100%);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,Segoe UI,Roboto,Arial,sans-serif}",
      ".gt-wrap{max-width:1280px;margin:0 auto;padding:22px 26px 60px}",
      ".gt-top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;flex-wrap:wrap}",
      ".gt-brand{display:flex;align-items:center;gap:13px}",
      ".gt-coin{width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#ffe27a,#b8851a);box-shadow:0 0 26px rgba(247,201,72,.35)}",
      ".gt-brand h1{margin:0;font-size:17px;letter-spacing:.14em}.gt-brand p{margin:2px 0 0;font-size:10.5px;letter-spacing:.34em;color:var(--mut)}",
      ".gt-badges{display:flex;gap:8px;flex-wrap:wrap;align-items:center}",
      ".gt-badge{display:flex;align-items:center;gap:7px;border:1px solid var(--line);background:#0b121c;border-radius:999px;padding:8px 13px;font-size:12px;color:var(--mut)}",
      ".gt-badge b{color:#fff;font-weight:600}",
      ".gt-badge.lock{background:rgba(255,93,119,.12);border-color:rgba(255,93,119,.4);color:#ffd3db}",
      ".gt-badge.live{background:rgba(31,209,138,.12);border-color:rgba(31,209,138,.4);color:#bff6e0}",
      ".gt-badge.warn{background:rgba(245,185,66,.12);border-color:rgba(245,185,66,.4);color:#ffe9b8}",
      ".gt-dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}",
      ".gt-dot.ok{background:var(--green);box-shadow:0 0 10px rgba(31,209,138,.7)}.gt-dot.bad{background:var(--red)}",
      ".gt-refresh{cursor:pointer}",
      ".gt-hero{position:relative;border:1px solid var(--line);border-radius:22px;padding:26px 28px;margin-bottom:18px;overflow:hidden;background:linear-gradient(180deg,#101b29,#0a121c)}",
      ".gt-hero:before{content:'';position:absolute;top:0;bottom:0;left:0;width:7px}",
      ".gt-hero.go:before{background:linear-gradient(var(--green),#0b8a5a)}.gt-hero.wait:before{background:linear-gradient(var(--amber),#a9791a)}.gt-hero.off:before{background:linear-gradient(var(--red),#a01f33)}",
      ".gt-hero.go{box-shadow:0 0 0 1px rgba(31,209,138,.25),0 30px 80px rgba(0,0,0,.45)}",
      ".gt-heroRow{display:grid;grid-template-columns:1fr 150px;gap:22px;align-items:center}",
      ".gt-kicker{font-size:11px;letter-spacing:.3em;text-transform:uppercase;color:var(--gold)}",
      ".gt-state{font-size:60px;line-height:1;font-weight:900;letter-spacing:.04em;margin:10px 0 6px}",
      ".gt-state.go{color:var(--green)}.gt-state.wait{color:var(--amber)}.gt-state.off{color:var(--red)}",
      ".gt-msg{color:#d6dfeb;max-width:760px;font-size:14.5px;line-height:1.5}",
      ".gt-heroFacts{display:flex;gap:20px;margin-top:16px;flex-wrap:wrap}",
      ".gt-hf{font-size:12px;color:var(--mut)}.gt-hf b{display:block;color:#fff;font-size:20px;margin-top:3px;font-weight:700}",
      ".gt-ring{width:138px;height:138px;border-radius:50%;display:grid;place-items:center;padding:11px;background:conic-gradient(var(--gold) calc(var(--p)*1%),#1a2736 0)}",
      ".gt-ringIn{width:100%;height:100%;border-radius:50%;background:#08111c;display:grid;place-items:center;text-align:center}",
      ".gt-ringIn strong{font-size:32px}.gt-ringIn span{font-size:11px;color:var(--mut)}.gt-grade{font-size:13px;font-weight:800;letter-spacing:.1em;margin-top:4px}",
      ".gt-grid{display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:18px}",
      ".gt-card{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);border-radius:18px;overflow:hidden}",
      ".gt-card h3{margin:0;padding:15px 18px;border-bottom:1px solid var(--line);font-size:11.5px;letter-spacing:.22em;text-transform:uppercase;color:#b9c6d6;display:flex;justify-content:space-between;align-items:center}",
      ".gt-card .pad{padding:18px}",
      ".gt-col{display:flex;flex-direction:column;gap:18px}",
      ".gt-side{display:inline-flex;align-items:center;gap:9px;font-size:22px;font-weight:900;letter-spacing:.08em;padding:8px 16px;border-radius:12px}",
      ".gt-side.buy{color:#bff6e0;background:rgba(31,209,138,.14);border:1px solid rgba(31,209,138,.4)}",
      ".gt-side.sell{color:#ffd3db;background:rgba(255,93,119,.14);border:1px solid rgba(255,93,119,.4)}",
      ".gt-side.none{color:var(--mut);background:#0b121c;border:1px solid var(--line)}",
      ".gt-tk{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}",
      ".gt-tkc{background:#0b1220;border:1px solid var(--line);border-radius:13px;padding:13px}.gt-tkc.full{grid-column:1/-1}",
      ".gt-tkc span{display:block;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--mut)}",
      ".gt-tkc b{display:block;margin-top:6px;font-size:19px;font-weight:700}",
      ".gt-tkc.tp b{color:var(--green)}.gt-tkc.sl b{color:var(--red)}.gt-tkc.entry b{color:var(--gold)}",
      ".gt-enter{width:100%;margin-top:16px;border:0;border-radius:14px;padding:16px;font-size:16px;font-weight:800;letter-spacing:.06em;cursor:pointer;color:#06140e;background:linear-gradient(135deg,#33e4a0,#11a874)}",
      ".gt-enter:disabled{cursor:not-allowed;background:#101a27;color:#6b7a8e;border:1px solid var(--line)}",
      ".gt-locknote{margin-top:10px;font-size:11.5px;color:#ffc6cf;text-align:center;letter-spacing:.04em}",
      ".gt-why{list-style:none;margin:0;padding:0}",
      ".gt-why li{display:flex;gap:11px;padding:11px 0;border-bottom:1px solid rgba(27,42,59,.6);font-size:13.5px;color:#d6dfeb}.gt-why li:last-child{border-bottom:0}",
      ".gt-why .b{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:0 0 auto;background:var(--mut)}",
      ".gt-why .b.block{background:var(--red)}.gt-why .b.pass{background:var(--green)}.gt-why .b.warn{background:var(--amber)}",
      ".gt-empty{color:var(--mut);font-size:13px;padding:6px 0}",
      ".gt-facts{display:grid;grid-template-columns:1fr 1fr;gap:10px}",
      ".gt-fact{background:#0b1220;border:1px solid var(--line);border-radius:12px;padding:11px 12px}",
      ".gt-fact span{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--mut)}.gt-fact b{display:block;margin-top:5px;font-size:14px;font-weight:600}",
      ".gt-tfbtns button{background:#0b121c;border:1px solid var(--line);color:#aebccd;border-radius:9px;padding:7px 11px;margin-left:6px;cursor:pointer;font-size:12px}",
      ".gt-tfbtns button.active{background:var(--gold);color:#1a1200;border-color:var(--gold);font-weight:800}",
      ".gt-chartmeta{font-size:11.5px;color:var(--mut);padding:10px 18px 0}",
      "canvas{width:100%;height:330px;display:block}",
      ".gt-advbar{margin-top:20px;display:flex;justify-content:center}",
      ".gt-advtoggle{background:#0b121c;border:1px solid var(--line);color:#b9c6d6;border-radius:12px;padding:11px 20px;cursor:pointer;font-size:13px;letter-spacing:.06em}.gt-advtoggle:hover{border-color:var(--gold);color:#fff}",
      ".gt-adv{margin-top:16px}",
      ".gt-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}",
      ".gt-tab{background:#0b121c;border:1px solid var(--line);color:var(--mut);border-radius:10px;padding:9px 14px;cursor:pointer;font-size:12.5px}",
      ".gt-tab.active{background:rgba(247,201,72,.12);border-color:rgba(247,201,72,.4);color:#fff}",
      ".gt-tfgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}",
      ".gt-tfcell{background:#0b1220;border:1px solid var(--line);border-radius:13px;padding:13px}",
      ".gt-tfcell.aligned{border-color:rgba(31,209,138,.45);box-shadow:inset 0 0 0 1px rgba(31,209,138,.08)}",
      ".gt-tfcell h5{margin:0 0 8px;font-size:15px}.gt-tfcell p{margin:3px 0;font-size:12px;color:var(--mut)}",
      ".gt-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}",
      ".gt-json{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#aebccd;background:#05090f;border:1px solid var(--line);border-radius:13px;padding:15px;max-height:64vh;overflow:auto}",
      ".gt-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#0e1b14;border:1px solid rgba(31,209,138,.5);color:#bff6e0;padding:13px 20px;border-radius:13px;box-shadow:0 18px 50px rgba(0,0,0,.5);font-size:13.5px;z-index:50}",
      ".gt-syncbar{background:rgba(255,93,119,.14);border:1px solid rgba(255,93,119,.4);color:#ffd3db;border-radius:13px;padding:12px 16px;margin-bottom:16px;font-size:13px;letter-spacing:.04em}",
      "@media(max-width:980px){.gt-grid{grid-template-columns:1fr}.gt-heroRow{grid-template-columns:1fr}}",
    ].join("\n");
    var s = document.createElement("style");
    s.id = "gt-cc-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  /* ---------- data load ---------- */
  function load() {
    getJSON("/api/decision").then(function (d) { state.decision = d; state.decisionErr = null; })
      .catch(function (e) { state.decisionErr = String(e.message || e); })
      .then(function () { return loadCandles(state.tf); })
      .then(function () { return getJSON("/api/performance").then(function (p) { state.perf = p; }).catch(function () {}); })
      .then(function () { return getJSON("/api/provider-health").then(function (h) { state.health = h; }).catch(function () {}); })
      .then(render).catch(render);
  }
  function loadCandles(tf) {
    return getJSON("/api/candles?tf=" + encodeURIComponent(tf)).then(function (c) {
      state.candles = (c && c.candles) || [];
      state.candleMeta = c || {};
    }).catch(function (e) { state.candles = []; state.candleMeta = { error: String(e.message || e) }; });
  }

  /* ---------- view-model ---------- */
  function vm() {
    var d = state.decision || {};
    var noState = d.source === "no_valid_local_state" || d.action === "NO_VALID_STATE";
    var canEnter = d.paper_allowed === true || /TRADE_READY/.test(String(d.action || ""));
    var side = String(d.side || "none").toLowerCase();
    var grade = d.final_grade || "—";
    var score = num(d.final_score) || 0;
    var entryLow = num(d.entry_low), entryHigh = num(d.entry_high);
    var entry = entryLow != null && entryHigh != null ? (entryLow + entryHigh) / 2 : num(d.current_price);
    var stop = num(d.stop_loss);
    var target = num(d.tp1);
    var risk = entry != null && stop != null ? Math.abs(entry - stop) : null;
    var rr = risk && target != null && entry != null ? Math.abs(target - entry) / risk : num(d.rr_tp1);
    var lot = risk ? Math.max(0.01, Math.round((30 / (risk * 100)) * 100) / 100) : null;
    var st = noState ? "off" : canEnter ? "go" : "wait";
    return {
      d: d, noState: noState, canEnter: canEnter, side: side, grade: grade, score: score,
      price: num(d.current_price), entry: entry, entryLow: entryLow, entryHigh: entryHigh,
      stop: stop, target: target, risk: risk, rr: rr, lot: lot, st: st,
      tpModel: d.tp_model, liveTrack: d.live_track === true,
      blockers: d.blockers || [], reasons: d.reasons || [], watching: d.watching_for || [],
      sync: d.cloud_sync, age: num(d.cloud_state_age_seconds),
      source: (d.data_health || {}).data_source || (d.market_context || {}).data_provider || "—",
      engine: d.engine || "—", liveLocked: !d.live_orders_enabled,
    };
  }

  /* ---------- render ---------- */
  function render() {
    injectStyles();
    var m = vm();
    var stale = m.sync && m.sync !== "fresh";
    var root = document.getElementById("root");
    if (!root) return;
    root.innerHTML =
      '<div class="gt-wrap">' +
        topBar(m) +
        (stale ? '<div class="gt-syncbar">LOCAL ENGINE NOT SYNCING — last known state (' + esc(m.sync) + (m.age != null ? ", " + m.age + "s old" : "") + ")</div>" : "") +
        hero(m) +
        '<div class="gt-grid">' +
          '<div class="gt-col">' + ticketCard(m) + chartCard() + "</div>" +
          '<div class="gt-col">' + whyCard(m) + factsCard(m) + "</div>" +
        "</div>" +
        advSection(m) +
      "</div>" +
      (state.toast ? '<div class="gt-toast">' + esc(state.toast) + "</div>" : "");
    wire(m);
    drawChart(m);
  }

  function topBar(m) {
    var live = m.liveLocked
      ? '<span class="gt-badge lock"><span class="gt-dot bad"></span>Orders <b>LOCKED</b></span>'
      : '<span class="gt-badge live"><span class="gt-dot ok"></span>Orders <b>LIVE</b></span>';
    var srcOk = /bridge|mt5|live/i.test(m.source);
    return '<div class="gt-top">' +
      '<div class="gt-brand"><div class="gt-coin"></div><div><h1>GOLD TRADER</h1><p>COMMAND CENTER · PAPER</p></div></div>' +
      '<div class="gt-badges">' +
        '<span class="gt-badge"><b>XAUUSD</b></span>' +
        '<span class="gt-badge ' + (srcOk ? "live" : "warn") + '"><span class="gt-dot ' + (srcOk ? "ok" : "") + '"></span>' + (srcOk ? "Live MT5" : "Source") + ' <b>' + esc(m.source) + "</b></span>" +
        '<span class="gt-badge ' + (m.sync === "fresh" ? "" : "warn") + '">Sync <b>' + esc(m.sync || "—") + "</b></span>" +
        live +
        '<span class="gt-badge gt-refresh" id="gt-refresh"><span class="gt-dot ok"></span>Refresh</span>' +
      "</div></div>";
  }

  function hero(m) {
    var word = m.st === "go" ? "ENTER" : m.st === "off" ? "NO SIGNAL" : (m.d.action === "WAIT" && m.side !== "none" ? "WAIT" : "SCANNING");
    var kick = m.st === "go" ? "Tradeable setup — manual approval" : m.st === "off" ? "Engine state" : "AI is watching the tape";
    var msg;
    if (m.st === "go") {
      msg = "Grade " + esc(m.grade) + " " + m.side.toUpperCase() + " is ready. Stop is 1R just outside the IFVG; target is the " +
        esc(m.tpModel || "5R") + " runner" + (m.rr ? " (" + m.rr.toFixed(1) + "R)" : "") + ". " +
        (m.liveTrack ? "Counts toward the live go/no-go." : "PAPER ONLY — excluded from the live go/no-go.");
    } else if (m.noState) {
      msg = "No fresh local decision. Start the engine: bash scripts/local_up_8766.sh";
    } else if (m.blockers && m.blockers.length) {
      msg = esc(m.blockers[0]);
    } else {
      msg = "Scanning live bars for the next IFVG setup (sweep → displacement → inversion → retest).";
    }
    return '<div class="gt-hero ' + m.st + '"><div class="gt-heroRow"><div>' +
        '<div class="gt-kicker">' + esc(kick) + "</div>" +
        '<div class="gt-state ' + m.st + '">' + esc(word) + "</div>" +
        '<div class="gt-msg">' + msg + "</div>" +
        '<div class="gt-heroFacts">' +
          '<div class="gt-hf">Live price<b>' + fmtPx(m.price) + "</b></div>" +
          '<div class="gt-hf">Side<b>' + (m.side === "none" ? "—" : m.side.toUpperCase()) + "</b></div>" +
          '<div class="gt-hf">Timeframe<b>' + esc(m.d.timeframe || "1H") + "</b></div>" +
          '<div class="gt-hf">Risk<b>$30</b></div>' +
        "</div>" +
      "</div>" +
      '<div class="gt-ring" style="--p:' + Math.max(0, Math.min(100, m.score)) + '"><div class="gt-ringIn"><div><strong>' + esc(m.score) + "</strong><br><span>/100</span><div class=\"gt-grade\">GRADE " + esc(m.grade) + "</div></div></div></div>" +
      "</div></div>";
  }

  function ticketCard(m) {
    var sideCls = m.side === "buy" ? "buy" : m.side === "sell" ? "sell" : "none";
    var sideTxt = m.side === "buy" ? "▲ BUY" : m.side === "sell" ? "▼ SELL" : "NO SIDE";
    var rows =
      '<div class="gt-tkc entry"><span>Entry zone</span><b>' + (m.entryLow != null ? fmtPx(m.entryLow) + " – " + fmtPx(m.entryHigh) : fmtPx(m.entry)) + "</b></div>" +
      '<div class="gt-tkc sl"><span>Stop (1R)</span><b>' + fmtPx(m.stop) + "</b></div>" +
      '<div class="gt-tkc tp"><span>Target · ' + esc(m.tpModel || "5R") + '</span><b>' + fmtPx(m.target) + "</b></div>" +
      '<div class="gt-tkc"><span>Reward : Risk</span><b>' + (m.rr ? m.rr.toFixed(1) + "R" : "—") + "</b></div>" +
      '<div class="gt-tkc"><span>Lot @ $30 risk</span><b>' + (m.lot != null ? m.lot.toFixed(2) : "—") + "</b></div>" +
      '<div class="gt-tkc full"><span>Track</span><b>' + (m.liveTrack ? "Live go/no-go" : "Paper only") + "</b></div>";
    var btn = '<button class="gt-enter" id="gt-enter"' + (m.canEnter ? "" : " disabled") + ">" +
      (m.canEnter ? "Mark Paper Entry" : (m.side === "none" ? "No setup" : "Waiting for green")) + "</button>";
    var lock = '<div class="gt-locknote">🔒 Live orders locked — paper collection only. Manual approval required.</div>';
    return '<div class="gt-card"><h3>Trade Ticket</h3><div class="pad">' +
      '<span class="gt-side ' + sideCls + '">' + sideTxt + "</span>" +
      '<div class="gt-tk">' + rows + "</div>" + btn + lock + "</div></div>";
  }

  function whyCard(m) {
    var title = m.canEnter ? "Why it's green" : "What we're waiting for";
    var items = [];
    if (m.canEnter) {
      (m.reasons || []).slice(0, 6).forEach(function (r) { items.push(["pass", r]); });
      if (!items.length) items.push(["pass", "Grade " + m.grade + " " + m.side.toUpperCase() + " passes all gates."]);
    } else {
      (m.blockers || []).slice(0, 6).forEach(function (b) { items.push(["block", b]); });
      (m.watching || []).slice(0, 3).forEach(function (w) { items.push(["warn", w]); });
      if (!items.length) items.push(["warn", "Scanning — no blockers, no setup yet."]);
    }
    var li = items.map(function (it) { return '<li><span class="b ' + it[0] + '"></span><span>' + esc(it[1]) + "</span></li>"; }).join("");
    return '<div class="gt-card"><h3>' + esc(title) + '</h3><div class="pad"><ul class="gt-why">' + li + "</ul></div></div>";
  }

  function factsCard(m) {
    function f(label, val) { return '<div class="gt-fact"><span>' + esc(label) + "</span><b>" + esc(val) + "</b></div>"; }
    var ls = m.d.live_sentiment || {};
    return '<div class="gt-card"><h3>Key Facts</h3><div class="pad"><div class="gt-facts">' +
      f("Grade", m.grade) + f("Score", m.score + "/100") +
      f("Engine", m.engine) + f("Data", m.source) +
      f("Macro regime", ls.macro_regime || "—") + f("Sync age", m.age != null ? m.age + "s" : "—") +
      "</div></div></div>";
  }

  function chartCard() {
    var meta = state.candleMeta || {};
    var btns = TF_LIST.map(function (t) {
      return '<button class="' + (t === state.tf ? "active" : "") + '" data-tf="' + t + '">' + t + "</button>";
    }).join("");
    var note = meta.count != null
      ? esc(meta.count) + " candles · " + esc(meta.provider || meta.source || "feed") + (meta.chart_preview_only ? " · chart preview only" : "")
      : (meta.error ? esc(meta.error) : "loading…");
    return '<div class="gt-card"><h3>Chart <span class="gt-tfbtns">' + btns + "</span></h3>" +
      '<div class="gt-chartmeta">' + note + "</div>" +
      '<canvas id="gt-canvas" width="900" height="330"></canvas></div>';
  }

  function advSection(m) {
    var toggle = '<div class="gt-advbar"><button class="gt-advtoggle" id="gt-adv">' +
      (state.showAdv ? "▲ Hide engine details" : "▼ Show engine details (timeframes · health · journal · JSON)") + "</button></div>";
    if (!state.showAdv) return toggle;
    var tabs = [["alignment", "Timeframes"], ["health", "Provider health"], ["journal", "Journal"], ["json", "Decision JSON"]];
    var tabBtns = tabs.map(function (t) {
      return '<button class="gt-tab ' + (state.advTab === t[0] ? "active" : "") + '" data-tab="' + t[0] + '">' + t[1] + "</button>";
    }).join("");
    var body = state.advTab === "alignment" ? advAlignment(m)
      : state.advTab === "health" ? advHealth()
      : state.advTab === "journal" ? advJournal()
      : advJSON(m);
    return toggle + '<div class="gt-adv"><div class="gt-tabs">' + tabBtns + "</div>" + body + "</div>";
  }

  function advAlignment(m) {
    var reads = m.d.timeframe_reads || [];
    if (!reads.length) return '<div class="gt-card"><div class="pad gt-empty">No timeframe reads in current decision.</div></div>';
    var cells = reads.map(function (r) {
      var aligned = r.aligned || (num(r.score) || 0) >= 50;
      return '<div class="gt-tfcell ' + (aligned ? "aligned" : "") + '"><h5>' + esc(r.timeframe || "TF") + "</h5>" +
        "<p>Bias: " + esc(r.bias || "—") + "</p><p>IFVG: " + esc(r.ifvg_side || "none") + "</p><p>Score: " + esc(num(r.score) || 0) + "</p></div>";
    }).join("");
    return '<div class="gt-card"><h3>Timeframe Alignment</h3><div class="pad"><div class="gt-tfgrid">' + cells + "</div></div></div>";
  }

  function advHealth() {
    var h = state.health || {};
    function f(label, val) { return '<div class="gt-fact"><span>' + esc(label) + "</span><b>" + esc(val) + "</b></div>"; }
    var rows = "";
    Object.keys(h).filter(function (k) { return typeof h[k] !== "object"; }).slice(0, 12).forEach(function (k) { rows += f(k, h[k]); });
    if (!rows) rows = '<div class="gt-empty">Provider health (chart/context enrichment only — does not gate the decision).</div>';
    return '<div class="gt-card"><h3>Provider Health <span style="font-size:10px;color:var(--mut)">enrichment only</span></h3><div class="pad"><div class="gt-kv">' + rows + "</div></div></div>";
  }

  function advJournal() {
    var p = state.perf || {};
    function f(label, val) { return '<div class="gt-fact"><span>' + esc(label) + "</span><b>" + esc(val) + "</b></div>"; }
    var rows = f("Total signals", p.total_signals != null ? p.total_signals : "—") +
      f("Closed", p.closed_signals != null ? p.closed_signals : "—") +
      f("Open", p.open_signals != null ? p.open_signals : "—") +
      f("TP1 hit-rate", p.tp1_hit_rate != null ? (p.tp1_hit_rate * 100).toFixed(0) + "%" : "—") +
      f("Expectancy R", p.expectancy_r != null ? p.expectancy_r : "—") +
      f("Avg adverse R", p.average_max_adverse_r != null ? p.average_max_adverse_r : "—");
    return '<div class="gt-card"><h3>Paper Journal · 20-trade go/no-go</h3><div class="pad"><div class="gt-kv">' + rows +
      '</div><p class="gt-empty" style="margin-top:12px">Live promotion gate: 20+ forward Grade-A trades, 60%+ WR, positive R. Grade-B 4H logged but excluded.</p></div></div>';
  }

  function advJSON(m) {
    return '<div class="gt-card"><h3>Decision JSON</h3><div class="pad"><div class="gt-json">' + esc(JSON.stringify(m.d, null, 2)) + "</div></div></div>";
  }

  /* ---------- interactions ---------- */
  function wire(m) {
    var r = document.getElementById("gt-refresh"); if (r) r.onclick = load;
    var a = document.getElementById("gt-adv"); if (a) a.onclick = function () { state.showAdv = !state.showAdv; render(); };
    Array.prototype.forEach.call(document.querySelectorAll("[data-tab]"), function (b) {
      b.onclick = function () { state.advTab = b.getAttribute("data-tab"); render(); };
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-tf]"), function (b) {
      b.onclick = function () { state.tf = b.getAttribute("data-tf"); loadCandles(state.tf).then(render); };
    });
    var e = document.getElementById("gt-enter");
    if (e && m.canEnter) e.onclick = function () {
      state.toast = "Paper entry noted — " + m.side.toUpperCase() + " " + fmtPx(m.entry) + " · SL " + fmtPx(m.stop) + " · TP " + fmtPx(m.target) + " · " + (m.lot ? m.lot.toFixed(2) : "?") + " lot.";
      render();
      setTimeout(function () { state.toast = null; render(); }, 5000);
    };
  }

  /* ---------- candle chart ---------- */
  function drawChart(m) {
    var cv = document.getElementById("gt-canvas");
    if (!cv) return;
    var rows = (state.candles || []).filter(function (k) {
      return Number.isFinite(Number(k.open)) && Number.isFinite(Number(k.high)) && Number.isFinite(Number(k.low)) && Number.isFinite(Number(k.close));
    });
    var dpr = window.devicePixelRatio || 1;
    var W = cv.clientWidth || 900, H = 330;
    cv.width = W * dpr; cv.height = H * dpr;
    var ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);
    if (!rows.length) {
      ctx.fillStyle = "#5b6b7e"; ctx.font = "14px Inter,sans-serif"; ctx.textAlign = "center";
      ctx.fillText("No candles for " + state.tf, W / 2, H / 2); return;
    }
    rows = rows.slice(-120);
    var overlays = [];
    if (m.entry != null) overlays.push(m.entry);
    if (m.stop != null) overlays.push(m.stop);
    if (m.target != null) overlays.push(m.target);
    var hi = -Infinity, lo = Infinity;
    rows.forEach(function (k) { hi = Math.max(hi, Number(k.high)); lo = Math.min(lo, Number(k.low)); });
    overlays.forEach(function (v) { hi = Math.max(hi, v); lo = Math.min(lo, v); });
    var padTop = 14, padBot = 18, padR = 64, padL = 8;
    var span = hi - lo || 1; hi += span * 0.04; lo -= span * 0.04; span = hi - lo;
    function y(v) { return padTop + (hi - v) / span * (H - padTop - padBot); }
    var cw = (W - padL - padR) / rows.length;
    ctx.strokeStyle = "rgba(27,42,59,.6)"; ctx.fillStyle = "#5f6f82"; ctx.font = "10px ui-monospace,monospace"; ctx.textAlign = "left";
    for (var g = 0; g <= 4; g++) {
      var gv = hi - (span * g / 4), gy = y(gv);
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
      ctx.fillText(gv.toFixed(1), W - padR + 6, gy + 3);
    }
    rows.forEach(function (k, i) {
      var o = y(Number(k.open)), h = y(Number(k.high)), l = y(Number(k.low)), c = y(Number(k.close));
      var up = Number(k.close) >= Number(k.open);
      var x = padL + i * cw + cw / 2;
      ctx.strokeStyle = up ? "#1fd18a" : "#ff5d77"; ctx.fillStyle = up ? "#1fd18a" : "#ff5d77";
      ctx.beginPath(); ctx.moveTo(x, h); ctx.lineTo(x, l); ctx.stroke();
      var bw = Math.max(1, cw * 0.6);
      var top = Math.min(o, c), bh = Math.max(1, Math.abs(c - o));
      ctx.globalAlpha = 0.9; ctx.fillRect(x - bw / 2, top, bw, bh); ctx.globalAlpha = 1;
    });
    function line(v, color, label) {
      if (v == null) return;
      var yy = y(v);
      ctx.setLineDash([5, 4]); ctx.strokeStyle = color; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
      ctx.fillStyle = color; ctx.font = "10px ui-monospace,monospace"; ctx.textAlign = "right";
      ctx.fillText(label + " " + v.toFixed(2), W - padR - 4, yy - 3);
    }
    line(m.entry, "#f7c948", "ENTRY");
    line(m.stop, "#ff5d77", "STOP");
    line(m.target, "#1fd18a", "TARGET");
  }

  /* ---------- boot ---------- */
  injectStyles();
  load();
  setInterval(load, REFRESH_MS);
})();

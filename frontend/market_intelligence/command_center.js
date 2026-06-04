(function () {
  "use strict";

  var TFs = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"];
  var candleSeq = 0;
  var state = {
    page: "trade",
    tf: "M15",
    decision: null,
    health: null,
    candles: [],
    candleMeta: null,
    loadError: null,
    chartLoading: false,
  };

  function $(sel) {
    return document.querySelector(sel);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function itemText(x) {
    return typeof x === "string" ? x : JSON.stringify(x);
  }

  function fmt(n, d) {
    d = d === undefined ? 2 : d;
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    return Number(n).toLocaleString(undefined, {
      maximumFractionDigits: d,
      minimumFractionDigits: d,
    });
  }

  function safe(v, f) {
    f = f === undefined ? "—" : f;
    return v === null || v === undefined || v === "" ? f : v;
  }

  function actionLabel(a) {
    a = String(a || "WAIT").toUpperCase();
    if (a.indexOf("HARD") >= 0) return "WAIT HARD BLOCK";
    if (a.indexOf("TRADE_READY") >= 0) return "PAPER TRADE READY";
    return a.replace(/_/g, " ");
  }

  function scoreMap(d) {
    var sd = d.score_decomposition || {};
    if (Array.isArray(sd)) {
      var out = {};
      for (var i = 0; i < sd.length; i++) {
        var row = sd[i] || {};
        out[row.key || row.label || String(i)] = row;
      }
      return out;
    }
    return sd;
  }

  function j(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(url + " " + r.status);
      return r.json();
    });
  }

  function renderLoading() {
    var root = document.getElementById("root");
    if (!root) return;
    root.innerHTML =
      '<div class="app boot-shell" style="min-height:100vh;display:grid;place-items:center">' +
      '<div style="text-align:center"><h2 style="letter-spacing:.16em;margin:0 0 12px">Gold Trader</h2>' +
      '<p style="color:#92a0b2;margin:0">Loading command center…</p></div></div>';
  }

  function loadCandles(tf) {
    state.tf = tf;
    state.chartLoading = true;
    var seq = ++candleSeq;
    return j("/api/candles?tf=" + encodeURIComponent(tf))
      .then(function (c) {
        if (seq !== candleSeq) return;
        state.chartLoading = false;
        state.candles = c.candles || [];
        state.candleMeta = c;
        if (!state.candles.length) {
          state.candleMeta = {
            ok: false,
            tf: tf,
            error: (c && c.error) || "No candles returned for " + tf,
            count: 0,
          };
        }
      })
      .catch(function (e) {
        if (seq !== candleSeq) return;
        state.chartLoading = false;
        state.candles = [];
        state.candleMeta = { ok: false, tf: tf, error: String(e), count: 0 };
      })
      .then(function () {
        if (seq !== candleSeq) return;
        if (state.page === "trade") render();
        drawSoon();
      });
  }

  function load() {
    if (!state.decision) renderLoading();
    state.loadError = null;
    return Promise.all([
      j("/api/decision").then(function (d) {
        state.decision = d;
      }),
      j("/api/provider-health").then(function (h) {
        state.health = h;
      }),
    ])
      .catch(function (e) {
        state.loadError = String(e);
        console.error(e);
      })
      .then(function () {
        render();
        loadCandles(state.tf);
      });
  }

  function nav(page) {
    state.page = page;
    render();
    drawSoon();
  }

  function top(d) {
    var age = d.source_age_status || {};
    var navItems = [
      "trade:Trade Cockpit",
      "market:Market Context",
      "signal:Signal Engine",
      "risk:Risk & Orders",
      "journal:Journal",
      "settings:Settings",
      "json:Decision JSON",
    ];
    var ageSec = age.age_seconds;
    var ageLabel = ageSec != null && ageSec !== "" ? ageSec : "—";
    return (
      '<aside class="side"><div class="brand"><div class="coin"></div><div><h1>Gold Trader</h1><p>COMMAND CENTER</p></div></div><nav class="nav">' +
      navItems
        .map(function (x) {
          var parts = x.split(":");
          var k = parts[0];
          var v = parts[1];
          return (
            '<a class="' +
            (state.page === k ? "active" : "") +
            '" href="#" data-page="' +
            esc(k) +
            '"><span>' +
            esc(v) +
            "</span>" +
            (k === "trade" ? "<b>XAU</b>" : "") +
            "</a>"
          );
        })
        .join("") +
      '</nav><div class="mini">Verdict <b>' +
      esc(actionLabel(d.action)) +
      "</b><br/>Score <b>" +
      esc(safe(d.final_score, 0)) +
      '/100</b><br/>Mode <b>' +
      esc(((d.cloud_status || {}).execution_mode) || "paper") +
      '</b><br/>Age <b>' +
      esc(ageLabel) +
      's</b></div></aside><main class="main"><div class="top"><div class="title"><h2>Gold Trader Command Center</h2><p>Full-system IFVG analysis, market awareness, and execution safety.</p></div><div class="chips"><span class="chip">Symbol <b>' +
      esc(safe(d.symbol, "XAUUSD")) +
      '</b></span><span class="chip">Data <b>' +
      esc((d.cloud_status || {}).data_provider || "twelvedata") +
      '</b></span><span class="chip lock">Orders <b>' +
      esc(d.live_orders_enabled ? "open" : "locked") +
      '</b></span><span class="chip"><i class="dot ' +
      esc(age.severity || "warning") +
      '"></i> ' +
      esc(age.label || "unknown") +
      '</span><button type="button" class="chip ok" id="gt-refresh">Refresh</button></div></div>'
    );
  }

  function hero(d) {
    var sd = scoreMap(d);
    var ageSec = (d.source_age_status || {}).age_seconds;
    var ageLabel = ageSec != null && ageSec !== "" ? ageSec : "—";
    return (
      '<section class="card hero"><div class="heroRow"><div><div class="label">Live Verdict</div><div class="verdict">' +
      esc(actionLabel(d.action)) +
      "</div><div class=\"meta\">" +
      esc(safe(d.symbol, "XAUUSD")) +
      " · " +
      esc(String(d.side || "none").toUpperCase()) +
      " · Grade " +
      esc(safe(d.final_grade, "—")) +
      " · Source age " +
      esc(ageLabel) +
      's</div><div class="brief">' +
      esc(safe(d.next_update, "Waiting for a fresh full-system scan.")) +
      '</div></div><div class="scoreRing" style="--score:' +
      Math.max(0, Math.min(100, Number(d.final_score || 0))) +
      '"><div class="scoreInner"><div><strong>' +
      esc(safe(d.final_score, 0)) +
      '</strong><br/><span>/100</span><br/><small>' +
      esc(safe(d.final_grade, "—")) +
      '</small></div></div></div></div><div class="stats"><div class="stat"><span>Current</span><b>' +
      esc(fmt(d.current_price)) +
      '</b></div><div class="stat"><span>Entry</span><b>' +
      esc(fmt(d.entry_low)) +
      " – " +
      esc(fmt(d.entry_high)) +
      '</b></div><div class="stat"><span>Stop</span><b>' +
      esc(fmt(d.stop_loss)) +
      '</b></div><div class="stat"><span>Targets</span><b>' +
      esc(fmt(d.tp1)) +
      " / " +
      esc(fmt(d.tp2)) +
      " / " +
      esc(fmt(d.tp3)) +
      '</b></div></div><div class="scoreGrid">' +
      Object.keys(sd)
        .map(function (k) {
          var v = sd[k] || {};
          return (
            '<div class="scoreBox ' +
            (Number(v.score || 0) === 0 ? "bad" : "ok") +
            '"><div class="name">' +
            esc(v.label || k) +
            '</div><div class="num">' +
            esc(v.score || 0) +
            "<small>/" +
            esc(v.max || 0) +
            "</small></div></div>"
          );
        })
        .join("") +
      "</div>" +
      (d.data_quality_penalty
        ? '<div class="brief danger">-' +
          esc(d.data_quality_penalty) +
          " pts data-quality penalty. Missing: " +
          esc(
            (d.missing_inputs || [])
              .map(function (x) {
                return typeof x === "string" ? x : x.label || "";
              })
              .join(", ")
          ) +
          "</div>"
        : "") +
      "</section>"
    );
  }

  function drawSoon() {
    requestAnimationFrame(function () {
      requestAnimationFrame(draw);
    });
  }

  function chartStatusLine() {
    if (state.chartLoading) {
      return "Loading " + esc(state.tf) + " candles…";
    }
    var meta = state.candleMeta || {};
    if (meta.ok === false || !(state.candles && state.candles.length)) {
      return '<span class="danger">' + esc(meta.error || "feed error") + "</span>";
    }
    var line =
      esc(meta.count || state.candles.length) +
      " candles · " +
      esc(meta.provider || "twelvedata") +
      " · live feed";
    if (meta.cache_note) {
      line += ' · <span class="amber">' + esc(meta.cache_note) + "</span>";
    }
    return line;
  }

  function chart() {
    var meta = state.candleMeta || {};
    return (
      '<section class="card"><div class="chartHead"><h3 style="border:0;padding:0">Live Candlestick Workbench</h3><div class="tfBtns">' +
      TFs.map(function (tf) {
        return (
          '<button type="button" class="' +
          (state.tf === tf ? "active" : "") +
          '" data-tf="' +
          esc(tf) +
          '">' +
          esc(tf) +
          "</button>"
        );
      }).join("") +
      '</div></div><div class="chartMeta">' +
      esc(state.tf) +
      " · " +
      chartStatusLine() +
      " · Volume note: " +
      esc(meta.volume_note || "—") +
      '</div><canvas id="chart" width="1100" height="420"></canvas></section>'
    );
  }

  function draw() {
    var c = $("#chart");
    if (!c || !state.candles || !state.candles.length || state.chartLoading) return;
    var data = state.candles.filter(function (row) {
      return (
        Number.isFinite(Number(row.high)) &&
        Number.isFinite(Number(row.low)) &&
        Number.isFinite(Number(row.open)) &&
        Number.isFinite(Number(row.close))
      );
    });
    if (!data.length) return;
    data = data.slice(-180);
    var ctx = c.getContext("2d");
    var W = c.width;
    var H = c.height;
    var p = 28;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#071019";
    ctx.fillRect(0, 0, W, H);
    var hi = Math.max.apply(
      null,
      data.map(function (x) {
        return Number(x.high);
      })
    );
    var lo = Math.min.apply(
      null,
      data.map(function (x) {
        return Number(x.low);
      })
    );
    function y(v) {
      return p + ((hi - v) / (hi - lo || 1)) * (H - p * 2);
    }
    function x(i) {
      return p + (i * (W - p * 2)) / Math.max(1, data.length - 1);
    }
    ctx.strokeStyle = "#162334";
    ctx.lineWidth = 1;
    for (var i = 0; i < 6; i++) {
      var yy = p + (i * (H - p * 2)) / 5;
      ctx.beginPath();
      ctx.moveTo(p, yy);
      ctx.lineTo(W - p, yy);
      ctx.stroke();
    }
    data.forEach(function (k, idx) {
      var xx = x(idx);
      var o = y(Number(k.open));
      var h = y(Number(k.high));
      var l = y(Number(k.low));
      var cl = y(Number(k.close));
      var up = Number(k.close) >= Number(k.open);
      ctx.strokeStyle = up ? "#16d68f" : "#ff5470";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(xx, h);
      ctx.lineTo(xx, l);
      ctx.stroke();
      var bw = Math.max(2, ((W - p * 2) / data.length) * 0.55);
      ctx.fillRect(xx - bw / 2, Math.min(o, cl), bw, Math.max(1, Math.abs(cl - o)));
    });
    ctx.fillStyle = "#94a3b8";
    ctx.font = "12px ui-monospace";
    ctx.fillText(fmt(hi), W - 80, p + 8);
    ctx.fillText(fmt(lo), W - 80, H - p);
  }

  function lists(d) {
    return (
      '<div class="right"><div class="panel control"><h4>Operator Control</h4><div class="body"><b>' +
      esc(d.live_orders_enabled ? "LIVE ORDERS OPEN" : "LIVE ORDERS LOCKED") +
      "</b><p>" +
      esc(
        d.live_orders_enabled
          ? "Execution is enabled. Confirm broker/spread before firing."
          : "Paper/alert only. Live execution cannot fire from this UI."
      ) +
      '</p></div></div><div class="panel"><h4>Watching For</h4><div class="body"><ul>' +
      ((d.watching_for || [])
        .map(function (x) {
          return "<li>" + esc(itemText(x)) + "</li>";
        })
        .join("") || "<li>No active checklist</li>") +
      '</ul></div></div><div class="panel"><h4>Why</h4><div class="body"><ul>' +
      ((d.readable_reasons || [])
        .map(function (x) {
          return "<li>" + esc(itemText(x)) + "</li>";
        })
        .join("") || "<li>No items</li>") +
      '</ul></div></div><div class="panel"><h4>Blockers</h4><div class="body"><ul>' +
      ((d.readable_blockers || [])
        .map(function (x) {
          return "<li>" + esc(itemText(x)) + "</li>";
        })
        .join("") || "<li>No blockers</li>") +
      "</ul></div></div>" +
      contextPanel(d) +
      "</div>"
    );
  }

  function feedLabel(value) {
    var text = String(value == null ? "unknown" : value).toLowerCase();
    if (text === "dead") return "compressed";
    if (text === "unknown_nonfatal_in_paper") return "unavailable (paper)";
    if (text === "ok_no_high_impact") return "clear (no high-impact events)";
    return String(value == null ? "unknown" : value);
  }

  function contextPanel(d) {
    var m = d.market_intelligence_summary || {};
    var c = d.cloud_status || {};
    return (
      '<div class="panel"><h4>Live Context</h4><div class="body contextGrid"><div class="ctx"><span>Analysis</span><b class="okText">online</b></div><div class="ctx"><span>Candles</span><b>' +
      esc(safe(c.candles_loaded, "—")) +
      '</b></div><div class="ctx"><span>Provider</span><b>' +
      esc(safe(c.data_provider, "—")) +
      '</b></div><div class="ctx"><span>Volatility</span><b>' +
      esc(feedLabel(safe(m.volatility, "unknown"))) +
      '</b></div><div class="ctx"><span>Spread</span><b class="' +
      (String(m.spread || "").indexOf("unknown") >= 0 ? "amber" : "") +
      '">' +
      esc(feedLabel(safe(m.spread, "unknown"))) +
      '</b></div><div class="ctx"><span>Macro</span><b class="' +
      (m.macro === "unknown" ? "amber" : "") +
      '">' +
      esc(feedLabel(safe(m.macro, "unknown"))) +
      '</b></div><div class="ctx"><span>Sentiment</span><b>' +
      esc(safe(m.sentiment, "unknown")) +
      '</b></div><div class="ctx"><span>Orders</span><b class="amber">' +
      esc(d.live_orders_enabled ? "open" : "locked") +
      '</b></div><div class="ctx"><span>CME</span><b>' +
      esc(safe(m.cme, "not_connected")) +
      '</b></div><div class="ctx"><span>Options</span><b>' +
      esc(safe(m.options, "not_connected")) +
      '</b></div><div class="ctx"><span>COT</span><b>' +
      esc(feedLabel(safe(m.cot, "unknown"))) +
      '</b></div><div class="ctx"><span>DXY / Yields / VIX</span><b>' +
      esc(feedLabel(safe(m.cross_market, "unknown"))) +
      "</b></div></div></div>"
    );
  }

  function tfGrid(d) {
    var t = d.tf_align || {};
    return (
      '<section class="card wide"><h3>Timeframe Alignment</h3><div class="tfGrid">' +
      TFs.map(function (tf) {
        var r = t[tf] || {};
        return (
          '<div class="tfCard ' +
          (r.aligned ? "aligned" : "") +
          '"><h5>' +
          esc(tf) +
          "</h5><p>Bias: " +
          esc(safe(r.bias, "—")) +
          "</p><p>IFVG: " +
          esc(safe(r.ifvg_side, "—")) +
          "</p><p>Score: " +
          esc(safe(r.score, "—")) +
          "</p><p>Candles: " +
          esc(safe(r.candles, 0)) +
          (r.data_state === "unavailable" ? ' <span class="amber">(feed unavailable)</span>' : "") +
          "</p></div>"
        );
      }).join("") +
      "</div></section>"
    );
  }

  function tradePage(d) {
    return (
      '<div class="grid"><div>' +
      hero(d) +
      '<div style="height:16px"></div>' +
      chart() +
      "</div>" +
      lists(d) +
      tfGrid(d) +
      "</div>"
    );
  }

  function marketLevels(d) {
    var summary = d.market_levels_summary || {};
    var levels = summary.levels || [];
    return (
      '<section class="card wide"><h3>Options / OI Levels</h3><div class="tfGrid">' +
      (levels.length
        ? levels
            .slice(0, 12)
            .map(function (level) {
              var distance = level.distance_points;
              return (
                '<div class="tfCard"><h5>' +
                esc(fmt(level.price, 2)) +
                "</h5><p>" +
                esc(level.kind || "level") +
                (level.strength ? " · " + esc(level.strength) : "") +
                "</p><p>" +
                esc(level.label || "Manual market level") +
                "</p><p>Distance: " +
                esc(distance === undefined || distance === null ? "—" : fmt(distance, 2)) +
                "</p><p>" +
                esc(summary.state || "missing") +
                " · " +
                esc(summary.source || "none") +
                "</p></div>"
              );
            })
            .join("")
        : '<div class="tfCard"><h5>No Levels</h5><p>' +
          esc(summary.state || "missing") +
          "</p><p>" +
          esc(summary.source || "config/market_levels.json") +
          "</p></div>") +
      "</div></section>"
    );
  }

  function marketPage(d) {
    var ph = state.health || d.provider_health_summary || {};
    function providerDetail(v) {
      var parts = [];
      if (v.source) parts.push("Source: " + v.source);
      if (v.configured !== undefined) parts.push("Configured: " + (v.configured ? "yes" : "no"));
      if (v.age_seconds !== undefined && v.age_seconds !== null) parts.push("Age: " + v.age_seconds + "s");
      if (v.latency_ms !== undefined && v.latency_ms !== null) parts.push("Latency: " + v.latency_ms + "ms");
      if (v.required_env && v.required_env.length) parts.push("Needs: " + v.required_env.join(", "));
      if (v.message) parts.push(v.message);
      return parts;
    }
    return (
      '<div class="grid"><section class="card"><h3>Market Intelligence</h3><div class="tfGrid">' +
      Object.keys(ph)
        .map(function (k) {
          var v = ph[k] || {};
          var detail = providerDetail(v);
          return (
            '<div class="tfCard"><h5>' +
            esc(v.label || k) +
            "</h5><p>State: " +
            esc(v.state || "unknown") +
            (v.severity ? " · " + esc(v.severity) : "") +
            "</p>" +
            detail.map(function (x) { return "<p>" + esc(x) + "</p>"; }).join("") +
            "<p>" +
            esc(k) +
            "</p></div>"
          );
        })
        .join("") +
      "</div></section>" +
      marketLevels(d) +
      lists(d) +
      "</div>"
    );
  }

  function signalPage(d) {
    return (
      '<div class="grid"><section class="card"><h3>Signal Engine</h3><div class="body" style="padding:18px"><h2>Alignment Audit</h2><pre class="json">' +
      esc(JSON.stringify(d.alignment_audit || {}, null, 2)) +
      "</pre></div></section>" +
      lists(d) +
      tfGrid(d) +
      "</div>"
    );
  }

  function riskPage(d) {
    return (
      '<div class="grid"><section class="card"><h3>Risk &amp; Orders</h3><div class="body" style="padding:18px"><pre class="json">' +
      esc(
        JSON.stringify(
          {
            daily_guard: d.daily_guard,
            live_orders_enabled: d.live_orders_enabled,
            missing_inputs: d.missing_inputs,
          },
          null,
          2
        )
      ) +
      "</pre></div></section>" +
      lists(d) +
      "</div>"
    );
  }

  function journalPage() {
    return (
      '<section class="card"><h3>Journal &amp; Evidence</h3><div class="body" style="padding:18px"><p>Decision snapshots are written to <b>logs/decision_snapshots/</b> on each hardening pass.</p><p>Next: wire paper-trade entries, R-multiple tracking, expectancy, and screenshot/evidence capture.</p></div></section>'
    );
  }

  function settingsPage(d) {
    return (
      '<section class="card"><h3>Settings &amp; Health</h3><div class="body" style="padding:18px"><pre class="json">' +
      esc(JSON.stringify(d.provider_health_summary || {}, null, 2)) +
      "</pre></div></section>"
    );
  }

  function bindUi(root) {
    var refresh = root.querySelector("#gt-refresh");
    if (refresh) refresh.addEventListener("click", load);
    var navLinks = root.querySelectorAll("[data-page]");
    for (var i = 0; i < navLinks.length; i++) {
      navLinks[i].addEventListener("click", function (ev) {
        ev.preventDefault();
        nav(this.getAttribute("data-page"));
      });
    }
    var tfBtns = root.querySelectorAll(".tfBtns button[data-tf]");
    for (var j = 0; j < tfBtns.length; j++) {
      tfBtns[j].addEventListener("click", function () {
        var tf = this.getAttribute("data-tf");
        state.tf = tf;
        state.chartLoading = true;
        render();
        loadCandles(tf);
      });
    }
  }

  function render() {
    var root = document.getElementById("root");
    if (!root) return;
    var d = state.decision || {};
    var body = "";
    if (state.loadError) {
      body =
        '<section class="card"><h3>Load error</h3><div class="body" style="padding:18px"><p class="danger">' +
        esc(state.loadError) +
        '</p><button type="button" class="chip ok" id="gt-retry">Retry</button></div></section>';
    } else if (state.page === "trade") body = tradePage(d);
    else if (state.page === "market") body = marketPage(d);
    else if (state.page === "signal") body = signalPage(d);
    else if (state.page === "risk") body = riskPage(d);
    else if (state.page === "journal") body = journalPage();
    else if (state.page === "settings") body = settingsPage(d);
    else if (state.page === "json")
      body =
        '<section class="card"><h3>Decision JSON</h3><pre class="json">' +
        esc(JSON.stringify(d, null, 2)) +
        "</pre></section>";
    try {
      root.innerHTML = '<div class="app">' + top(d) + body + "</main></div>";
      bindUi(root);
      var retry = root.querySelector("#gt-retry");
      if (retry) retry.addEventListener("click", load);
      drawSoon();
    } catch (e) {
      root.innerHTML =
        '<div style="padding:40px;color:#ff5770;font-family:monospace">Render error: ' +
        esc(String(e)) +
        "</div>";
    }
  }

  function boot() {
    renderLoading();
    load();
    setInterval(load, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

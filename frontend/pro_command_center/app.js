(() => {
  const { useEffect, useRef, useState } = React;
  const TFS = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"];
  const pages = ["Trade Cockpit", "Market Context", "Signal Engine", "Risk & Orders", "Journal", "Settings", "Decision JSON"];
  function fmt(v, d = 2) {
    if (v === null || v === void 0 || Number.isNaN(v)) return "\u2014";
    if (typeof v === "number") return v.toLocaleString(void 0, { maximumFractionDigits: d, minimumFractionDigits: d });
    return String(v);
  }
  function compactAction(a) {
    a = String(a || "WAIT").toUpperCase();
    if (a.includes("TRADE_READY")) return "PAPER TRADE READY";
    return a.replaceAll("_", " ");
  }
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }
  function drawChart(canvas, candles) {
    const ctx = canvas.getContext("2d");
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#08101a";
    ctx.fillRect(0, 0, w, h);
    if (!candles || candles.length < 2) {
      ctx.fillStyle = "#91a0b6";
      ctx.fillText("No candles", 20, 30);
      return;
    }
    const pad = { l: 18, r: 64, t: 40, b: 26 };
    const vals = candles.flatMap((c) => [+c.high, +c.low]).filter(Number.isFinite);
    const hi = Math.max(...vals), lo = Math.min(...vals);
    const range = hi - lo || 1;
    const xStep = (w - pad.l - pad.r) / candles.length;
    ctx.strokeStyle = "#162335";
    ctx.lineWidth = 1;
    ctx.font = "12px ui-monospace,monospace";
    ctx.fillStyle = "#8291a6";
    for (let i = 0; i < 6; i++) {
      const y2 = pad.t + i * (h - pad.t - pad.b) / 5;
      ctx.beginPath();
      ctx.moveTo(pad.l, y2);
      ctx.lineTo(w - pad.r, y2);
      ctx.stroke();
      const val = hi - i * range / 5;
      ctx.fillText(fmt(val, 2), w - pad.r + 8, y2 + 4);
    }
    function y(v) {
      return pad.t + (hi - v) / range * (h - pad.t - pad.b);
    }
    candles.forEach((c, i) => {
      const x = pad.l + i * xStep + xStep / 2;
      const o = +c.open, cl = +c.close, hh = +c.high, ll = +c.low;
      const up = cl >= o;
      ctx.strokeStyle = up ? "#18d48a" : "#ff5267";
      ctx.fillStyle = up ? "#18d48a" : "#ff5267";
      ctx.beginPath();
      ctx.moveTo(x, y(hh));
      ctx.lineTo(x, y(ll));
      ctx.stroke();
      const body = Math.max(2, Math.abs(y(o) - y(cl)));
      ctx.fillRect(x - Math.max(2, xStep * 0.28), Math.min(y(o), y(cl)), Math.max(3, xStep * 0.56), body);
    });
  }
  function App() {
    const [page, setPage] = useState("Trade Cockpit");
    const [decision, setDecision] = useState(null);
    const [candles, setCandles] = useState([]);
      // Deprecated: not the active local command center. Use src/gold_trader/web/static/index.html
      const [tf, setTf] = useState("M15");
    const [alerts, setAlerts] = useState([]);
    const [err, setErr] = useState("");
    const canvas = useRef(null);
    async function refresh() {
      try {
        const d2 = await getJSON("/api/decision");
        setDecision(d2);
        const a = await getJSON("/api/alerts");
        setAlerts(a.alerts || []);
        setErr("");
      } catch (e) {
        setErr(e.message);
      }
    }
    async function loadCandles(t = tf) {
      try {
        const c = await getJSON("/api/candles?tf=" + encodeURIComponent(t) + "&count=280");
        setCandles(c.candles || []);
      } catch (e) {
        setCandles([]);
      }
    }
    useEffect(() => {
      refresh();
      loadCandles();
      const id = setInterval(() => {
        refresh();
        loadCandles(tf);
      }, 3e4);
      return () => clearInterval(id);
    }, []);
    useEffect(() => {
      loadCandles(tf);
    }, [tf]);
    useEffect(() => {
      if (canvas.current) drawChart(canvas.current, candles);
    }, [candles, tf]);
    const d = decision || {};
    const score = Number(d.final_score || 0);
    const ready = !!d.is_trade_ready;
    const reads = d.timeframe_reads_ordered || TFS.map((timeframe) => ({ timeframe, candles: 0 }));
    const ctx = d.cloud_status || {};
    const mc = d.market_context || {};
    return /* @__PURE__ */ React.createElement("div", { className: "app" }, /* @__PURE__ */ React.createElement("aside", { className: "side" }, /* @__PURE__ */ React.createElement("div", { className: "brand" }, /* @__PURE__ */ React.createElement("div", { className: "logo" }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h1", null, "Gold Trader"), /* @__PURE__ */ React.createElement("p", null, "COMMAND CENTER"))), /* @__PURE__ */ React.createElement("nav", { className: "nav" }, pages.map((p) => /* @__PURE__ */ React.createElement("button", { className: page === p ? "active" : "", onClick: () => setPage(p), key: p }, /* @__PURE__ */ React.createElement("span", null, p), p === "Trade Cockpit" ? /* @__PURE__ */ React.createElement("b", null, "XAU") : null))), /* @__PURE__ */ React.createElement("div", { className: "mini" }, /* @__PURE__ */ React.createElement("div", { className: "miniRow" }, /* @__PURE__ */ React.createElement("span", null, "Verdict"), /* @__PURE__ */ React.createElement("b", null, compactAction(d.action))), /* @__PURE__ */ React.createElement("div", { className: "miniRow" }, /* @__PURE__ */ React.createElement("span", null, "Score"), /* @__PURE__ */ React.createElement("b", null, score, "/100")), /* @__PURE__ */ React.createElement("div", { className: "miniRow" }, /* @__PURE__ */ React.createElement("span", null, "Mode"), /* @__PURE__ */ React.createElement("b", null, String(d.execution_mode || "paper").toUpperCase())), /* @__PURE__ */ React.createElement("div", { className: "miniRow" }, /* @__PURE__ */ React.createElement("span", null, "Age"), /* @__PURE__ */ React.createElement("b", null, d._source_age_seconds ?? "\u2014", "s")))), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement(Top, { d, refresh }), page === "Trade Cockpit" && /* @__PURE__ */ React.createElement(Trade, { d, reads, score, ready, tf, setTf, candles, canvas, ctx, mc }), " ", page === "Market Context" && /* @__PURE__ */ React.createElement(Market, { d, ctx, mc }), " ", page === "Signal Engine" && /* @__PURE__ */ React.createElement(Signals, { reads, d }), " ", page === "Risk & Orders" && /* @__PURE__ */ React.createElement(Risk, { d }), " ", page === "Journal" && /* @__PURE__ */ React.createElement(Journal, { alerts }), " ", page === "Settings" && /* @__PURE__ */ React.createElement(Settings, { d, err }), " ", page === "Decision JSON" && /* @__PURE__ */ React.createElement("pre", { className: "jsonBox" }, JSON.stringify(d, null, 2)), /* @__PURE__ */ React.createElement("div", { className: "footerSpace" })));
  }
  function Top({ d, refresh }) {
    return /* @__PURE__ */ React.createElement("div", { className: "top" }, /* @__PURE__ */ React.createElement("div", { className: "title" }, /* @__PURE__ */ React.createElement("h2", null, "Gold Trader Command Center"), /* @__PURE__ */ React.createElement("p", null, "Full-system IFVG analysis, market awareness, and execution safety.")), /* @__PURE__ */ React.createElement("div", { className: "pills" }, /* @__PURE__ */ React.createElement("div", { className: "pill" }, "Symbol ", /* @__PURE__ */ React.createElement("b", null, d.symbol || "XAU/USD")), /* @__PURE__ */ React.createElement("div", { className: "pill" }, "Data ", /* @__PURE__ */ React.createElement("b", null, d.data_provider || "twelvedata")), /* @__PURE__ */ React.createElement("div", { className: "pill" }, "Orders ", /* @__PURE__ */ React.createElement("b", null, d.orders_locked ? "locked" : "enabled")), /* @__PURE__ */ React.createElement("div", { className: "pill" }, "Updated ", /* @__PURE__ */ React.createElement("b", null, d._source_age_seconds ?? "\u2014", "s")), /* @__PURE__ */ React.createElement("button", { className: "refresh", onClick: refresh }, "Refresh")));
  }
  function Trade({ d, reads, score, ready, tf, setTf, candles, canvas, ctx, mc }) {
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "gridTrade" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("section", { className: "card hero" }, /* @__PURE__ */ React.createElement("div", { className: "heroMain" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "LIVE VERDICT"), /* @__PURE__ */ React.createElement("h1", { className: "verdict " + (ready ? "ready" : "wait") }, compactAction(d.action)), /* @__PURE__ */ React.createElement("div", { className: "meta" }, d.symbol || "XAU/USD", " \xB7 ", String(d.side || "none").toUpperCase(), " \xB7 Grade ", d.final_grade || "\u2014", " \xB7 Source age ", d._source_age_seconds ?? "\u2014", "s"), /* @__PURE__ */ React.createElement("div", { className: "what" }, d.next_update || "Waiting for a fresh full-system scan.")), /* @__PURE__ */ React.createElement("div", { className: "score", style: { "--score": score } }, /* @__PURE__ */ React.createElement("span", null, score, /* @__PURE__ */ React.createElement("small", null, "/100")))), /* @__PURE__ */ React.createElement("div", { className: "tradeLevels" }, /* @__PURE__ */ React.createElement(Box, { label: "Current", value: fmt(d.current_price) }), /* @__PURE__ */ React.createElement(Box, { label: "Entry", value: `${fmt(d.entry_low)} \u2013 ${fmt(d.entry_high)}` }), /* @__PURE__ */ React.createElement(Box, { label: "Stop", value: fmt(d.stop_loss) }), /* @__PURE__ */ React.createElement(Box, { label: "Targets", value: `${fmt(d.tp1)} / ${fmt(d.tp2)} / ${fmt(d.tp3)}` }))), /* @__PURE__ */ React.createElement("section", { className: "card chartCard", style: { marginTop: 18 } }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "LIVE CANDLESTICK WORKBENCH"), /* @__PURE__ */ React.createElement("div", { className: "tfTabs" }, TFS.map((x) => /* @__PURE__ */ React.createElement("button", { key: x, onClick: () => setTf(x), className: tf === x ? "active" : "" }, x)))), /* @__PURE__ */ React.createElement("div", { className: "chartWrap" }, /* @__PURE__ */ React.createElement("div", { className: "chartMeta" }, tf, " \xB7 ", candles.length, " candles \xB7 twelvedata"), candles.length === 0 ? /* @__PURE__ */ React.createElement("div", { className: "chartError" }, "No candle data returned for ", tf, ". Check API key / symbol / provider quota.") : null, /* @__PURE__ */ React.createElement("canvas", { ref: canvas })))), /* @__PURE__ */ React.createElement("aside", { className: "sideStack" }, /* @__PURE__ */ React.createElement(ListCard, { title: "WHAT TO DO NOW", items: [d.next_update || "Wait for a Grade-A aligned IFVG setup."] }), /* @__PURE__ */ React.createElement(ListCard, { title: "WHY", items: d.reasons || [] }), /* @__PURE__ */ React.createElement(ListCard, { title: "BLOCKERS", items: d.blockers && d.blockers.length ? d.blockers : ["No blockers"] }), /* @__PURE__ */ React.createElement(Context, { ctx, mc }))), /* @__PURE__ */ React.createElement("section", { className: "card", style: { marginTop: 18 } }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "TIMEFRAME ALIGNMENT"), /* @__PURE__ */ React.createElement("div", { className: "subtle" }, reads.filter((r) => r.ifvg_side && r.ifvg_side !== "none").length, "/7 active IFVG reads")), /* @__PURE__ */ React.createElement(TFGrid, { reads })));
  }
  function Box({ label, value }) {
    return /* @__PURE__ */ React.createElement("div", { className: "level" }, /* @__PURE__ */ React.createElement("label", null, label), /* @__PURE__ */ React.createElement("strong", null, value));
  }
  function ListCard({ title, items }) {
    return /* @__PURE__ */ React.createElement("section", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, title)), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("div", { className: "list" }, (items && items.length ? items : ["No items"]).map((x, i) => /* @__PURE__ */ React.createElement("div", { className: "listItem", key: i }, String(x))))));
  }
  function Context({ ctx, mc }) {
    const items = [["Analysis", ctx.analysis || "online"], ["Candles", ctx.candles_loaded || 0], ["Provider", ctx.data_provider || "twelvedata"], ["Volatility", ctx.volatility || mc.volatility_state || "unknown"], ["Spread", ctx.spread || mc.spread_points || "unknown"], ["Macro", ctx.macro || mc.macro_state || "unknown"], ["Sentiment", ctx.sentiment || mc.sentiment_state || "unknown"], ["Orders", ctx.orders || "locked"]];
    return /* @__PURE__ */ React.createElement("section", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "LIVE CONTEXT")), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("div", { className: "contextGrid" }, items.map(([k, v]) => /* @__PURE__ */ React.createElement("div", { className: "context", key: k }, /* @__PURE__ */ React.createElement("label", null, k), /* @__PURE__ */ React.createElement("strong", { className: String(v).match(/online|normal|locked|twelvedata/i) ? "ok" : "warn" }, String(v)))))));
  }
  function TFGrid({ reads }) {
    return /* @__PURE__ */ React.createElement("div", { className: "tfGrid" }, reads.map((r) => /* @__PURE__ */ React.createElement("div", { key: r.timeframe, className: "tfCard " + (r.ifvg_side || "") }, /* @__PURE__ */ React.createElement("h3", null, r.timeframe), /* @__PURE__ */ React.createElement("p", null, "Bias: ", /* @__PURE__ */ React.createElement("b", null, r.bias || "\u2014")), /* @__PURE__ */ React.createElement("p", null, "IFVG: ", /* @__PURE__ */ React.createElement("b", null, r.ifvg_side || "\u2014")), /* @__PURE__ */ React.createElement("p", null, "Score: ", /* @__PURE__ */ React.createElement("b", null, r.score ?? "\u2014")), /* @__PURE__ */ React.createElement("p", null, "Candles: ", /* @__PURE__ */ React.createElement("b", null, r.candles || 0)))));
  }
  function Market({ d, ctx, mc }) {
    return /* @__PURE__ */ React.createElement("div", { className: "pageGrid" }, /* @__PURE__ */ React.createElement("section", { className: "card big" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "MARKET AWARENESS")), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("table", { className: "table" }, /* @__PURE__ */ React.createElement("tbody", null, [["Data provider", d.data_provider || ctx.data_provider], ["Current price", fmt(d.current_price)], ["Spread", mc.spread_points || ctx.spread || "unknown"], ["Session", mc.session || "unknown"], ["Volatility", mc.volatility_state || ctx.volatility || "unknown"], ["Macro", mc.macro_state || ctx.macro || "unknown"], ["Sentiment", mc.sentiment_state || ctx.sentiment || "unknown"], ["COT / Options / CME", "pending provider access"]].map(([a, b]) => /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, a), /* @__PURE__ */ React.createElement("td", null, b || "\u2014"))))))), /* @__PURE__ */ React.createElement(ListCard, { title: "MARKET NOTES", items: [...mc.notes || [], ...mc.warnings || []] }));
  }
  function Signals({ reads, d }) {
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("section", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "SIGNAL ENGINE"), /* @__PURE__ */ React.createElement("div", { className: "subtle" }, "IFVG-only, Grade-A policy")), /* @__PURE__ */ React.createElement(TFGrid, { reads })), /* @__PURE__ */ React.createElement("div", { className: "pageGrid", style: { marginTop: 16 } }, /* @__PURE__ */ React.createElement(ListCard, { title: "REASONS", items: d.reasons || [] }), /* @__PURE__ */ React.createElement(ListCard, { title: "BLOCKERS", items: d.blockers || [] }), /* @__PURE__ */ React.createElement(ListCard, { title: "NEXT ACTION", items: [d.next_update || "Wait."] })));
  }
  function Risk({ d }) {
    const g = d.daily_guard || {};
    return /* @__PURE__ */ React.createElement("div", { className: "pageGrid" }, /* @__PURE__ */ React.createElement("section", { className: "card big" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "RISK & ORDER SAFETY")), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("table", { className: "table" }, /* @__PURE__ */ React.createElement("tbody", null, [["Execution mode", d.execution_mode || "paper"], ["Live orders", d.orders_locked ? "locked" : "enabled"], ["Trades today", g.trades_taken ?? 0], ["Losses today", g.losses_taken ?? 0], ["Open positions", g.open_positions ?? 0], ["Guard blocked", String(!!g.blocked)]].map(([a, b]) => /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, a), /* @__PURE__ */ React.createElement("td", null, b))))))), /* @__PURE__ */ React.createElement(ListCard, { title: "GUARD REASONS", items: g.reasons || ["No guard block"] }));
  }
  function Journal({ alerts }) {
    return /* @__PURE__ */ React.createElement("section", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "OPERATOR JOURNAL"), /* @__PURE__ */ React.createElement("div", { className: "subtle" }, "Latest alerts and paper evidence")), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("table", { className: "table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Time"), /* @__PURE__ */ React.createElement("th", null, "Type"), /* @__PURE__ */ React.createElement("th", null, "Message"))), /* @__PURE__ */ React.createElement("tbody", null, (alerts.length ? alerts : [{ timestamp: "\u2014", type: "system", message: "No alerts yet" }]).map((a, i) => /* @__PURE__ */ React.createElement("tr", { key: i }, /* @__PURE__ */ React.createElement("td", null, a.timestamp || a.timestamp_utc || "\u2014"), /* @__PURE__ */ React.createElement("td", null, a.type || a.level || "alert"), /* @__PURE__ */ React.createElement("td", null, a.message || a.operator_message || JSON.stringify(a).slice(0, 200))))))));
  }
  function Settings({ d, err }) {
    return /* @__PURE__ */ React.createElement("div", { className: "healthGrid" }, /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, /* @__PURE__ */ React.createElement("span", { className: "statusDot" }), " API online")), /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, "Decision source", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("b", null, d._source_path || "none"))), /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, "Last error", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("b", null, err || "none"))), /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, "Orders", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("b", null, d.orders_locked ? "locked" : "enabled"))));
  }
  ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
})();

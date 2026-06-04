(() => {
  const { useEffect, useMemo, useRef, useState } = React;
  const API = { decision: "/api/decision", candles: (tf) => `/api/candles?tf=${tf}&count=280`, alerts: "/api/alerts" };
  const TFS = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"];
  const NAV = ["Trade Cockpit", "Market Context", "Signal Engine", "Risk & Orders", "Journal", "Settings", "Decision JSON"];
  function fmt(n, d = 2) {
    if (n === null || n === void 0 || Number.isNaN(Number(n))) return "\u2014";
    return Number(n).toLocaleString(void 0, { maximumFractionDigits: d, minimumFractionDigits: d });
  }
  function clsAction(a) {
    const s = String(a || "").toLowerCase();
    if (s.includes("trade") || s.includes("buy") || s.includes("sell")) return "trade";
    if (s.includes("block")) return "block";
    return "wait";
  }
  function cleanAction(a) {
    const s = String(a || "WAIT");
    if (s.includes("TRADE_READY")) return "PAPER TRADE READY";
    return s.replaceAll("_", " ");
  }
  function ageText(meta) {
    const a = meta?.source_age_seconds;
    if (a == null) return "\u2014";
    if (a < 60) return `${Math.round(a)}s`;
    if (a < 3600) return `${Math.round(a / 60)}m`;
    return `${Math.round(a / 3600)}h`;
  }
  async function getJson(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  }
  function useDecision() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    async function load() {
      try {
        const j = await getJson(API.decision);
        setData(j);
        setErr(null);
      } catch (e) {
        setErr(e.message);
      }
    }
    useEffect(() => {
      load();
      const t = setInterval(load, 15e3);
      return () => clearInterval(t);
    }, []);
    return { data, err, reload: load };
  }
  function useCandles(tf) {
    const [data, setData] = useState({ candles: [], loading: true, error: null, source: null });
    async function load() {
      setData((x) => ({ ...x, loading: true }));
      try {
        const j = await getJson(API.candles(tf));
        setData({ candles: j.candles || [], loading: false, error: j.error || null, source: j.source, count: j.count });
      } catch (e) {
        setData({ candles: [], loading: false, error: e.message, source: null });
      }
    }
    useEffect(() => {
      load();
      const t = setInterval(load, 6e4);
      return () => clearInterval(t);
    }, [tf]);
    return data;
  }
  function CandleChart({ tf }) {
    const canvasRef = useRef(null);
    const boxRef = useRef(null);
    const data = useCandles(tf);
    useEffect(() => {
      const canvas = canvasRef.current, box = boxRef.current;
      if (!canvas || !box || !data.candles.length) return;
      const dpr = window.devicePixelRatio || 1;
      const rect = box.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const pad = { l: 26, r: 54, t: 42, b: 34 };
      const w = rect.width - pad.l - pad.r, h = rect.height - pad.t - pad.b;
      const candles = data.candles.slice(-Math.min(180, data.candles.length));
      const highs = candles.map((c) => +c.high), lows = candles.map((c) => +c.low);
      const max = Math.max(...highs), min = Math.min(...lows);
      const range = max - min || 1;
      const y = (v) => pad.t + (max - v) / range * h;
      const x = (i) => pad.l + i * (w / Math.max(1, candles.length - 1));
      ctx.strokeStyle = "rgba(255,255,255,.055)";
      ctx.lineWidth = 1;
      ctx.fillStyle = "rgba(139,152,170,.9)";
      ctx.font = "11px ui-monospace, monospace";
      for (let i = 0; i < 6; i++) {
        const yy = pad.t + h / 5 * i;
        ctx.beginPath();
        ctx.moveTo(pad.l, yy);
        ctx.lineTo(pad.l + w, yy);
        ctx.stroke();
        const val = max - range / 5 * i;
        ctx.fillText(fmt(val, 2), pad.l + w + 8, yy + 4);
      }
      const cw = Math.max(3, Math.min(10, w / candles.length * 0.58));
      candles.forEach((c, i) => {
        const open = +c.open, close = +c.close, high = +c.high, low = +c.low;
        const xx = x(i);
        const up = close >= open;
        ctx.strokeStyle = up ? "#14c784" : "#ff5c6c";
        ctx.fillStyle = up ? "#14c784" : "#ff5c6c";
        ctx.beginPath();
        ctx.moveTo(xx, y(high));
        ctx.lineTo(xx, y(low));
        ctx.stroke();
        const top = y(Math.max(open, close));
        const bh = Math.max(1, Math.abs(y(open) - y(close)));
        ctx.fillRect(xx - cw / 2, top, cw, bh);
      });
      ctx.fillStyle = "rgba(139,152,170,.85)";
      ctx.fillText(`${tf} \xB7 ${candles.length} shown \xB7 ${data.source || "provider"}`, pad.l, pad.t - 14);
      ctx.fillStyle = "rgba(243,246,251,.95)";
      ctx.fillText(fmt(candles[candles.length - 1]?.close, 2), pad.l + w - 16, pad.t - 14);
    }, [data.candles, tf]);
    return /* @__PURE__ */ React.createElement("div", { className: "chart-wrap", ref: boxRef }, data.loading && /* @__PURE__ */ React.createElement("div", { className: "chart-error" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("b", null, "Loading ", tf, " candles\u2026"), /* @__PURE__ */ React.createElement("span", null, "Fetching live chart data."))), !data.loading && data.error && /* @__PURE__ */ React.createElement("div", { className: "chart-error" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("b", null, "Chart data unavailable"), /* @__PURE__ */ React.createElement("span", null, data.error))), /* @__PURE__ */ React.createElement("canvas", { className: "chart-canvas", ref: canvasRef }), /* @__PURE__ */ React.createElement("div", { className: "footer-note" }, "Source: ", data.source || "\u2014", " \xB7 Returned: ", data.count ?? data.candles.length));
  }
  function Verdict({ d }) {
    const score = Number(d?.final_score || 0);
    const stale = (d?._meta?.source_age_seconds || 0) > 900;
    return /* @__PURE__ */ React.createElement("div", { className: `card ${stale ? "stale" : ""}` }, /* @__PURE__ */ React.createElement("div", { className: "verdict" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "label" }, "LIVE VERDICT"), /* @__PURE__ */ React.createElement("div", { className: `action ${clsAction(d?.action)}` }, cleanAction(d?.action)), /* @__PURE__ */ React.createElement("div", { className: "subline" }, d?.symbol || "XAU/USD", " \xB7 ", (d?.side || "none").toUpperCase(), " \xB7 Grade ", d?.final_grade || "\u2014"), /* @__PURE__ */ React.createElement("div", { className: "next" }, d?.next_update || "Waiting for the next scan.")), /* @__PURE__ */ React.createElement("div", { className: "score" }, /* @__PURE__ */ React.createElement("div", { className: "ring", style: { "--score": score } }, /* @__PURE__ */ React.createElement("div", { style: { position: "relative", textAlign: "center" } }, /* @__PURE__ */ React.createElement("strong", null, score), /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("span", null, "/100"))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 12, color: "var(--gold)", fontWeight: 900 } }, d?.final_grade || "\u2014"))), /* @__PURE__ */ React.createElement("div", { className: "levels" }, /* @__PURE__ */ React.createElement("div", { className: "level" }, /* @__PURE__ */ React.createElement("small", null, "Current"), /* @__PURE__ */ React.createElement("b", null, fmt(d?.current_price))), /* @__PURE__ */ React.createElement("div", { className: "level" }, /* @__PURE__ */ React.createElement("small", null, "Entry"), /* @__PURE__ */ React.createElement("b", null, fmt(d?.entry_low), " \u2013 ", fmt(d?.entry_high))), /* @__PURE__ */ React.createElement("div", { className: "level" }, /* @__PURE__ */ React.createElement("small", null, "Stop"), /* @__PURE__ */ React.createElement("b", null, fmt(d?.stop_loss))), /* @__PURE__ */ React.createElement("div", { className: "level" }, /* @__PURE__ */ React.createElement("small", null, "Targets"), /* @__PURE__ */ React.createElement("b", null, fmt(d?.tp1), " / ", fmt(d?.tp2), " / ", fmt(d?.tp3)))));
  }
  function TextCard({ title, items, empty }) {
    return /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "card-head" }, title), /* @__PURE__ */ React.createElement("div", { className: "text-card" }, items?.length ? /* @__PURE__ */ React.createElement("ul", null, items.map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x))) : /* @__PURE__ */ React.createElement("div", { className: "empty" }, empty || "No items")));
  }
  function Context({ d }) {
    const c = d?.cloud_status || {}, m = d?.market_context || {}, live = d?.live_market_context || {};
    const cells = [["Analysis", c.analysis || "online"], ["Candles", c.candles_loaded || 0], ["Provider", c.data_provider || "\u2014"], ["Volatility", m.volatility_state || c.volatility || "unknown"], ["Spread", m.spread_points ?? c.spread ?? "unknown"], ["Macro", m.macro_state || c.macro || live?.macro_state?.state || "unknown"], ["Sentiment", m.sentiment_state || c.sentiment || live?.sentiment_state?.state || "unknown"], ["Orders", c.orders || "locked"]];
    return /* @__PURE__ */ React.createElement("div", { className: "card" }, /* @__PURE__ */ React.createElement("div", { className: "card-head" }, "LIVE CONTEXT"), /* @__PURE__ */ React.createElement("div", { className: "context-grid" }, cells.map(([k, v]) => /* @__PURE__ */ React.createElement("div", { className: "context", key: k }, /* @__PURE__ */ React.createElement("small", null, k), /* @__PURE__ */ React.createElement("b", { className: String(v).includes("unknown") ? "warn" : String(v).includes("locked") ? "warn" : "ok" }, String(v))))));
  }
  function TFAlignment({ d }) {
    const reads = d?.timeframe_reads || [];
    const by = Object.fromEntries(reads.map((r) => [r.timeframe, r]));
    const active = reads.filter((r) => r.ifvg_side && r.ifvg_side !== "none").length;
    return /* @__PURE__ */ React.createElement("div", { className: "card pages" }, /* @__PURE__ */ React.createElement("div", { className: "card-head" }, /* @__PURE__ */ React.createElement("span", null, "TIMEFRAME ALIGNMENT"), /* @__PURE__ */ React.createElement("span", { className: "muted" }, active, "/7 active IFVG reads")), /* @__PURE__ */ React.createElement("div", { className: "tf-grid" }, TFS.map((tf) => {
      const r = by[tf] || {};
      return /* @__PURE__ */ React.createElement("div", { className: `tf ${r.ifvg_side || ""}`, key: tf }, /* @__PURE__ */ React.createElement("h4", null, tf), /* @__PURE__ */ React.createElement("p", null, "Bias: ", /* @__PURE__ */ React.createElement("b", null, r.bias || "\u2014")), /* @__PURE__ */ React.createElement("p", null, "IFVG: ", /* @__PURE__ */ React.createElement("b", null, r.ifvg_side || "\u2014")), /* @__PURE__ */ React.createElement("p", null, "Score: ", /* @__PURE__ */ React.createElement("b", null, r.score ?? "\u2014")), /* @__PURE__ */ React.createElement("p", null, "Candles: ", /* @__PURE__ */ React.createElement("b", null, r.candles ?? 0)));
    })));
  }
  function Trade({ d }) {
    const [tf, setTf] = useState("M15");
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "grid" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(Verdict, { d }), /* @__PURE__ */ React.createElement("div", { className: "card chart-card pages" }, /* @__PURE__ */ React.createElement("div", { className: "card-head" }, /* @__PURE__ */ React.createElement("span", null, "LIVE CANDLESTICK WORKBENCH"), /* @__PURE__ */ React.createElement("div", { className: "tf-tabs" }, TFS.map((x) => /* @__PURE__ */ React.createElement("button", { key: x, onClick: () => setTf(x), className: tf === x ? "active" : "" }, x)))), /* @__PURE__ */ React.createElement(CandleChart, { tf }))), /* @__PURE__ */ React.createElement("div", { className: "side-col" }, /* @__PURE__ */ React.createElement(TextCard, { title: "WHAT TO DO NOW", items: [d?.next_update].filter(Boolean), empty: "Waiting for a fresh full-system scan." }), /* @__PURE__ */ React.createElement(TextCard, { title: "WHY", items: d?.reasons || [] }), /* @__PURE__ */ React.createElement(TextCard, { title: "BLOCKERS", items: d?.blockers || [], empty: "No blockers" }), /* @__PURE__ */ React.createElement(Context, { d }))), /* @__PURE__ */ React.createElement(TFAlignment, { d }));
  }
  function GenericPage({ title, children }) {
    return /* @__PURE__ */ React.createElement("div", { className: "card pages" }, /* @__PURE__ */ React.createElement("div", { className: "card-head" }, title), children);
  }
  function App() {
    const { data, err, reload } = useDecision();
    const [page, setPage] = useState("Trade Cockpit");
    const d = data || {};
    const mode = d?.cloud_status?.execution_mode || "paper";
    return /* @__PURE__ */ React.createElement("div", { className: "app" }, /* @__PURE__ */ React.createElement("aside", { className: "sidebar" }, /* @__PURE__ */ React.createElement("div", { className: "brand" }, /* @__PURE__ */ React.createElement("div", { className: "logo" }, "Au"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h1", null, "Gold Trader"), /* @__PURE__ */ React.createElement("span", null, "COMMAND CENTER"))), /* @__PURE__ */ React.createElement("nav", { className: "nav" }, NAV.map((n) => /* @__PURE__ */ React.createElement("button", { key: n, className: page === n ? "active" : "", onClick: () => setPage(n) }, n))), /* @__PURE__ */ React.createElement("div", { className: "side-status" }, /* @__PURE__ */ React.createElement("div", { className: "kv" }, /* @__PURE__ */ React.createElement("span", null, "Verdict"), /* @__PURE__ */ React.createElement("b", null, cleanAction(d.action || "WAIT"))), /* @__PURE__ */ React.createElement("div", { className: "kv" }, /* @__PURE__ */ React.createElement("span", null, "Score"), /* @__PURE__ */ React.createElement("b", null, d.final_score ?? 0, "/100")), /* @__PURE__ */ React.createElement("div", { className: "kv" }, /* @__PURE__ */ React.createElement("span", null, "Mode"), /* @__PURE__ */ React.createElement("b", null, String(mode).toUpperCase())), /* @__PURE__ */ React.createElement("div", { className: "kv" }, /* @__PURE__ */ React.createElement("span", null, "Age"), /* @__PURE__ */ React.createElement("b", null, ageText(d._meta))))), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement("div", { className: "top" }, /* @__PURE__ */ React.createElement("div", { className: "title" }, /* @__PURE__ */ React.createElement("h2", null, "Gold Trader Command Center"), /* @__PURE__ */ React.createElement("p", null, "Live IFVG analysis, market awareness, and execution safety."), err && /* @__PURE__ */ React.createElement("p", { className: "bad" }, "API error: ", err)), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("div", { className: "chip" }, "Symbol ", /* @__PURE__ */ React.createElement("b", null, d.symbol || "XAU/USD")), /* @__PURE__ */ React.createElement("div", { className: "chip" }, "Data ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.data_provider || "twelvedata")), /* @__PURE__ */ React.createElement("div", { className: "chip" }, "Orders ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.orders || "locked")), /* @__PURE__ */ React.createElement("div", { className: "chip" }, "Updated ", /* @__PURE__ */ React.createElement("b", null, ageText(d._meta))), /* @__PURE__ */ React.createElement("button", { className: "refresh", onClick: reload }, "Refresh"))), page === "Trade Cockpit" && /* @__PURE__ */ React.createElement(Trade, { d }), " ", page === "Market Context" && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Context, { d }), /* @__PURE__ */ React.createElement(GenericPage, { title: "MARKET NOTES" }, /* @__PURE__ */ React.createElement("div", { className: "context-grid" }, /* @__PURE__ */ React.createElement("div", { className: "text-card" }, /* @__PURE__ */ React.createElement("h3", null, "Warnings"), /* @__PURE__ */ React.createElement("ul", null, (d.market_context?.warnings || []).map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x)))), /* @__PURE__ */ React.createElement("div", { className: "text-card" }, /* @__PURE__ */ React.createElement("h3", null, "Notes"), /* @__PURE__ */ React.createElement("ul", null, (d.market_context?.notes || []).map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x))))))), " ", page === "Signal Engine" && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(TFAlignment, { d }), /* @__PURE__ */ React.createElement(TextCard, { title: "SIGNAL REASONS", items: d.reasons || [] })), " ", page === "Risk & Orders" && /* @__PURE__ */ React.createElement(GenericPage, { title: "RISK & ORDERS" }, /* @__PURE__ */ React.createElement("div", { className: "context-grid" }, /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Trades today"), /* @__PURE__ */ React.createElement("b", null, d.daily_guard?.trades_taken ?? 0)), /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Losses today"), /* @__PURE__ */ React.createElement("b", null, d.daily_guard?.losses_taken ?? 0)), /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Open positions"), /* @__PURE__ */ React.createElement("b", null, d.daily_guard?.open_positions ?? 0)), /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Guard"), /* @__PURE__ */ React.createElement("b", null, d.daily_guard?.blocked ? "blocked" : "clear")), /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Live orders"), /* @__PURE__ */ React.createElement("b", { className: "warn" }, d.cloud_status?.orders || "locked")), /* @__PURE__ */ React.createElement("div", { className: "context" }, /* @__PURE__ */ React.createElement("small", null, "Mode"), /* @__PURE__ */ React.createElement("b", null, mode)))), " ", page === "Journal" && /* @__PURE__ */ React.createElement(Journal, null), " ", page === "Settings" && /* @__PURE__ */ React.createElement(GenericPage, { title: "SETTINGS" }, /* @__PURE__ */ React.createElement("div", { className: "jsonbox" }, JSON.stringify({ provider: d.cloud_status?.data_provider, execution_mode: mode, orders: d.cloud_status?.orders, source: d._meta?.source, render: d._meta?.render }, null, 2))), " ", page === "Decision JSON" && /* @__PURE__ */ React.createElement(GenericPage, { title: "DECISION JSON" }, /* @__PURE__ */ React.createElement("div", { className: "jsonbox" }, JSON.stringify(d, null, 2)))));
  }
  function Journal() {
    const [alerts, setAlerts] = useState([]);
    useEffect(() => {
      getJson(API.alerts).then((j) => setAlerts(j.alerts || [])).catch(() => setAlerts([]));
    }, []);
    return /* @__PURE__ */ React.createElement(GenericPage, { title: "JOURNAL & ALERTS" }, /* @__PURE__ */ React.createElement("div", { className: "jsonbox" }, alerts.length ? JSON.stringify(alerts, null, 2) : "No alerts yet."));
  }
  ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
})();

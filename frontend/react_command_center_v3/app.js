(() => {
  const { useEffect, useMemo, useRef, useState } = React;
  const TFS = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"];
  const fmt = (v, d = 2) => v === null || v === void 0 || Number.isNaN(Number(v)) ? "\u2014" : Number(v).toLocaleString(void 0, { maximumFractionDigits: d, minimumFractionDigits: d });
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }
  function drawCandles(canvas, candles) {
    const ctx = canvas.getContext("2d"), dpr = window.devicePixelRatio || 1, rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#07101a";
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(148,163,184,.10)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 6; i++) {
      let y2 = h * i / 6;
      ctx.beginPath();
      ctx.moveTo(0, y2);
      ctx.lineTo(w, y2);
      ctx.stroke();
    }
    if (!candles || !candles.length) {
      ctx.fillStyle = "#7f8da3";
      ctx.font = "14px monospace";
      ctx.fillText("No candle data returned for this timeframe", 20, 30);
      return;
    }
    const highs = candles.map((c) => +c.high), lows = candles.map((c) => +c.low);
    let max = Math.max(...highs), min = Math.min(...lows);
    if (max === min) {
      max += 1;
      min -= 1;
    }
    const pad = 18, plotH = h - pad * 2, xStep = w / candles.length;
    const y = (v) => pad + (max - v) / (max - min) * plotH;
    ctx.font = "11px monospace";
    ctx.fillStyle = "#94a3b8";
    [max, (max + min) / 2, min].forEach((v) => {
      ctx.fillText(fmt(v), w - 72, y(v) - 4);
    });
    candles.forEach((c, i) => {
      const x = i * xStep + xStep * 0.5, open = +c.open, close = +c.close, high = +c.high, low = +c.low, up = close >= open;
      ctx.strokeStyle = up ? "#12d78f" : "#ff5164";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(x, y(high));
      ctx.lineTo(x, y(low));
      ctx.stroke();
      const bw = Math.max(2, Math.min(8, xStep * 0.62));
      const top = Math.min(y(open), y(close)), bh = Math.max(1, Math.abs(y(open) - y(close)));
      ctx.fillRect(x - bw / 2, top, bw, bh);
    });
  }
  function Score({ score }) {
    const s = Math.max(0, Math.min(100, Number(score) || 0));
    return /* @__PURE__ */ React.createElement("div", { className: "scoreRing", style: { "--score": s } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("b", null, Math.round(s)), /* @__PURE__ */ React.createElement("span", null, "/100")));
  }
  function Hero({ d }) {
    const cls = d.tone || "wait";
    return /* @__PURE__ */ React.createElement("section", { className: "hero" }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow" }, "Live verdict"), /* @__PURE__ */ React.createElement("div", { className: "verdict " + cls }, d.verdict || "WAIT"), /* @__PURE__ */ React.createElement("div", { className: "sub" }, d.symbol || "XAU/USD", " \xB7 ", (d.side || "none").toUpperCase(), " \xB7 Grade ", d.final_grade || "\u2014", " \xB7 Source age ", d._source_age_seconds ?? "\u2014", "s"), /* @__PURE__ */ React.createElement(Score, { score: d.final_score }), /* @__PURE__ */ React.createElement("div", { className: "next" }, d.next_update || "Waiting for a fresh full-system scan."), /* @__PURE__ */ React.createElement("div", { className: "metrics" }, /* @__PURE__ */ React.createElement("div", { className: "metric" }, /* @__PURE__ */ React.createElement("label", null, "Current"), /* @__PURE__ */ React.createElement("b", null, fmt(d.current_price))), /* @__PURE__ */ React.createElement("div", { className: "metric" }, /* @__PURE__ */ React.createElement("label", null, "Entry"), /* @__PURE__ */ React.createElement("b", null, fmt(d.entry_low), " \u2013 ", fmt(d.entry_high))), /* @__PURE__ */ React.createElement("div", { className: "metric" }, /* @__PURE__ */ React.createElement("label", null, "Stop"), /* @__PURE__ */ React.createElement("b", null, fmt(d.stop_loss))), /* @__PURE__ */ React.createElement("div", { className: "metric" }, /* @__PURE__ */ React.createElement("label", null, "Targets"), /* @__PURE__ */ React.createElement("b", null, fmt(d.tp1), " / ", fmt(d.tp2), " / ", fmt(d.tp3)))));
  }
  function Chart() {
    const [tf, setTf] = useState("M15"), [candles, setCandles] = useState([]), [meta, setMeta] = useState({ loading: true });
    const ref = useRef(null);
    useEffect(() => {
      let ok = true;
      setMeta({ loading: true });
      getJSON("/api/candles?tf=" + tf + "&count=260").then((j) => {
        if (!ok) return;
        setCandles(j.candles || []);
        setMeta(j);
      }).catch((e) => {
        if (!ok) return;
        setCandles([]);
        setMeta({ error: e.message, provider: "error" });
      });
      return () => {
        ok = false;
      };
    }, [tf]);
    useEffect(() => {
      drawCandles(ref.current, candles);
    }, [candles, tf]);
    return /* @__PURE__ */ React.createElement("section", { className: "chartCard" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("h3", null, "Live Candlestick Workbench"), /* @__PURE__ */ React.createElement("div", { className: "tfButtons" }, TFS.map((t) => /* @__PURE__ */ React.createElement("button", { key: t, onClick: () => setTf(t), className: tf === t ? "active" : "" }, t)))), /* @__PURE__ */ React.createElement("div", { className: "chartWrap" }, /* @__PURE__ */ React.createElement("div", { className: "chartMeta" }, /* @__PURE__ */ React.createElement("span", null, tf, " \xB7 ", candles.length, " candles \xB7 ", meta.provider || "\u2014", " ", meta.error ? /* @__PURE__ */ React.createElement("b", { className: "warn" }, " \xB7 ", meta.error) : ""), /* @__PURE__ */ React.createElement("span", null, candles.length ? fmt(candles[candles.length - 1].close) : "\u2014")), /* @__PURE__ */ React.createElement("canvas", { ref })));
  }
  function Panel({ title, children }) {
    return /* @__PURE__ */ React.createElement("section", { className: "panel" }, /* @__PURE__ */ React.createElement("h3", null, title), /* @__PURE__ */ React.createElement("div", { className: "panelBody" }, children));
  }
  function DecisionSide({ d }) {
    return /* @__PURE__ */ React.createElement("div", { className: "sidePanels" }, /* @__PURE__ */ React.createElement(Panel, { title: "What to do now" }, /* @__PURE__ */ React.createElement("p", null, d.next_update || "Waiting for a fresh full-system scan.")), /* @__PURE__ */ React.createElement(Panel, { title: "Why" }, (d.reasons || []).length ? /* @__PURE__ */ React.createElement("ul", null, d.reasons.map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x))) : /* @__PURE__ */ React.createElement("span", { className: "empty" }, "No items yet")), /* @__PURE__ */ React.createElement(Panel, { title: "Blockers" }, (d.blockers || []).length ? /* @__PURE__ */ React.createElement("ul", null, d.blockers.map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x))) : /* @__PURE__ */ React.createElement("span", null, "No blockers")), /* @__PURE__ */ React.createElement(Panel, { title: "Live Context" }, /* @__PURE__ */ React.createElement("div", { className: "contextGrid" }, /* @__PURE__ */ React.createElement(Ctx, { label: "Analysis", val: d.cloud_status?.analysis || "waiting", good: d.cloud_status?.analysis === "online" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Candles", val: d.candles_loaded || d.cloud_status?.candles_loaded || 0 }), /* @__PURE__ */ React.createElement(Ctx, { label: "Provider", val: d.cloud_status?.data_provider || "\u2014" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Volatility", val: d.market_context?.volatility_state || d.cloud_status?.volatility || "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Spread", val: d.market_context?.spread_points ?? d.cloud_status?.spread ?? "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Macro", val: d.market_context?.macro_state || d.cloud_status?.macro || "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Sentiment", val: d.market_context?.sentiment_state || d.cloud_status?.sentiment || "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Orders", val: d.cloud_status?.orders || "locked", warn: true }))));
  }
  function Ctx({ label, val, good, warn }) {
    let c = good ? "green" : warn ? "gold" : "";
    return /* @__PURE__ */ React.createElement("div", { className: "contextBox" }, /* @__PURE__ */ React.createElement("label", null, label), /* @__PURE__ */ React.createElement("b", { className: c }, String(val)));
  }
  function TFGrid({ reads }) {
    return /* @__PURE__ */ React.createElement("section", { className: "chartCard" }, /* @__PURE__ */ React.createElement("div", { className: "cardHead" }, /* @__PURE__ */ React.createElement("h3", null, "Timeframe Alignment"), /* @__PURE__ */ React.createElement("span", { className: "statusLine" }, (reads || []).filter((r) => (r.ifvg_side || "none") !== "none").length, "/7 active IFVG reads")), /* @__PURE__ */ React.createElement("div", { className: "tfGrid", style: { padding: "14px 16px 18px", marginTop: 0 } }, (reads || []).map((r) => /* @__PURE__ */ React.createElement("div", { key: r.timeframe, className: "tfCard " + ((r.ifvg_side || "none") !== "none" ? "good" : "") }, /* @__PURE__ */ React.createElement("h4", null, r.timeframe), /* @__PURE__ */ React.createElement("p", null, "Bias: ", /* @__PURE__ */ React.createElement("b", null, r.bias || "\u2014")), /* @__PURE__ */ React.createElement("p", null, "IFVG: ", /* @__PURE__ */ React.createElement("b", null, r.ifvg_side || "none")), /* @__PURE__ */ React.createElement("p", null, "Score: ", /* @__PURE__ */ React.createElement("b", null, r.score ?? "\u2014")), /* @__PURE__ */ React.createElement("p", null, "Candles: ", /* @__PURE__ */ React.createElement("b", null, r.candles ?? 0))))));
  }
  function Trade({ d }) {
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "grid" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(Hero, { d }), /* @__PURE__ */ React.createElement(Chart, null)), /* @__PURE__ */ React.createElement(DecisionSide, { d })), /* @__PURE__ */ React.createElement(TFGrid, { reads: d.timeframe_reads }));
  }
  function Market({ d }) {
    return /* @__PURE__ */ React.createElement("div", { className: "grid" }, /* @__PURE__ */ React.createElement(Panel, { title: "Market Awareness" }, /* @__PURE__ */ React.createElement("div", { className: "contextGrid" }, /* @__PURE__ */ React.createElement(Ctx, { label: "Data Provider", val: d.cloud_status?.data_provider || "\u2014" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Candles Loaded", val: d.candles_loaded || 0 }), /* @__PURE__ */ React.createElement(Ctx, { label: "Volatility", val: d.market_context?.volatility_state || "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Spread", val: d.market_context?.spread_points ?? "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Macro", val: d.market_context?.macro_state || "unknown" }), /* @__PURE__ */ React.createElement(Ctx, { label: "Sentiment", val: d.market_context?.sentiment_state || "unknown" }))), /* @__PURE__ */ React.createElement(Panel, { title: "Notes / Warnings" }, /* @__PURE__ */ React.createElement("ul", null, [...d.market_context?.notes || [], ...d.market_context?.warnings || []].map((x, i) => /* @__PURE__ */ React.createElement("li", { key: i }, x)))));
  }
  function Risk({ d }) {
    const g = d.daily_guard || {};
    return /* @__PURE__ */ React.createElement("div", { className: "grid" }, /* @__PURE__ */ React.createElement(Panel, { title: "Daily Guard" }, /* @__PURE__ */ React.createElement("div", { className: "contextGrid" }, /* @__PURE__ */ React.createElement(Ctx, { label: "Trades Taken", val: g.trades_taken ?? 0 }), /* @__PURE__ */ React.createElement(Ctx, { label: "Losses Taken", val: g.losses_taken ?? 0 }), /* @__PURE__ */ React.createElement(Ctx, { label: "Open Positions", val: g.open_positions ?? 0 }), /* @__PURE__ */ React.createElement(Ctx, { label: "Blocked", val: String(!!g.blocked), warn: g.blocked }))), /* @__PURE__ */ React.createElement(Panel, { title: "Execution Safety" }, /* @__PURE__ */ React.createElement("p", null, "Execution mode: ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.execution_mode || "paper")), /* @__PURE__ */ React.createElement("p", null, "Orders: ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.orders || "locked")), /* @__PURE__ */ React.createElement("p", null, "Live order placement remains locked unless GOLD_ENABLE_LIVE_ORDERS=true.")));
  }
  function JSONPage({ d }) {
    return /* @__PURE__ */ React.createElement("pre", { className: "jsonBox" }, JSON.stringify(d, null, 2));
  }
  function App() {
    const [page, setPage] = useState("Trade Cockpit"), [d, setD] = useState(null), [err, setErr] = useState(null), [loading, setLoading] = useState(true);
    const load = () => {
      setLoading(true);
      return getJSON("/api/decision").then((j) => {
        setD(j);
        setErr(null);
      }).catch((e) => setErr(String(e.message || e))).finally(() => setLoading(false));
    };
    useEffect(() => {
      load();
      const id = setInterval(load, 15e3);
      return () => clearInterval(id);
    }, []);
    const nav = ["Trade Cockpit", "Market Context", "Signal Engine", "Risk & Orders", "Journal", "Settings", "Decision JSON"];
    if (loading && !d && !err) return /* @__PURE__ */ React.createElement("div", { className: "app" }, /* @__PURE__ */ React.createElement("aside", { className: "side" }, /* @__PURE__ */ React.createElement("div", { className: "brand" }, /* @__PURE__ */ React.createElement("div", { className: "logo" }, "Au"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h1", null, "Gold Trader"), /* @__PURE__ */ React.createElement("span", null, "COMMAND CENTER")))), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement("h2", null, "Loading command center\u2026")));
    if (!d) return /* @__PURE__ */ React.createElement("div", { className: "app" }, /* @__PURE__ */ React.createElement("aside", { className: "side" }, /* @__PURE__ */ React.createElement("div", { className: "brand" }, /* @__PURE__ */ React.createElement("div", { className: "logo" }, "Au"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h1", null, "Gold Trader"), /* @__PURE__ */ React.createElement("span", null, "COMMAND CENTER")))), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement("h2", null, "Could not load live decision"), /* @__PURE__ */ React.createElement("p", { className: "warn" }, err || "Unknown API error"), /* @__PURE__ */ React.createElement("button", { className: "refresh", onClick: load }, "Retry")));
    return /* @__PURE__ */ React.createElement("div", { className: "app" }, /* @__PURE__ */ React.createElement("aside", { className: "side" }, /* @__PURE__ */ React.createElement("div", { className: "brand" }, /* @__PURE__ */ React.createElement("div", { className: "logo" }, "Au"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h1", null, "Gold Trader"), /* @__PURE__ */ React.createElement("span", null, "COMMAND CENTER"))), /* @__PURE__ */ React.createElement("nav", { className: "nav" }, nav.map((n) => /* @__PURE__ */ React.createElement("button", { key: n, className: page === n ? "active" : "", onClick: () => setPage(n) }, n))), /* @__PURE__ */ React.createElement("div", { className: "mini" }, "Verdict ", /* @__PURE__ */ React.createElement("b", null, d.verdict), /* @__PURE__ */ React.createElement("br", null), "Score ", /* @__PURE__ */ React.createElement("b", null, d.final_score, "/100"), /* @__PURE__ */ React.createElement("br", null), "Mode ", /* @__PURE__ */ React.createElement("b", null, (d.cloud_status?.execution_mode || "paper").toUpperCase()), /* @__PURE__ */ React.createElement("br", null), "Age ", /* @__PURE__ */ React.createElement("b", null, d._source_age_seconds ?? "\u2014", "s"))), /* @__PURE__ */ React.createElement("main", { className: "main" }, /* @__PURE__ */ React.createElement("div", { className: "top" }, /* @__PURE__ */ React.createElement("div", { className: "title" }, /* @__PURE__ */ React.createElement("h2", null, "Gold Trader Command Center"), /* @__PURE__ */ React.createElement("p", null, "Full-system IFVG analysis, live market awareness, and execution safety.")), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", { className: "chip" }, "Symbol ", /* @__PURE__ */ React.createElement("b", null, d.symbol || "XAU/USD")), /* @__PURE__ */ React.createElement("span", { className: "chip" }, "Data ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.data_provider || "\u2014")), /* @__PURE__ */ React.createElement("span", { className: "chip" }, "Orders ", /* @__PURE__ */ React.createElement("b", null, d.cloud_status?.orders || "locked")), /* @__PURE__ */ React.createElement("span", { className: "chip" }, "Updated ", /* @__PURE__ */ React.createElement("b", null, d._source_age_seconds ?? "\u2014", "s")), /* @__PURE__ */ React.createElement("button", { className: "refresh", onClick: load }, "Refresh"))), page === "Trade Cockpit" ? /* @__PURE__ */ React.createElement(Trade, { d }) : page === "Market Context" ? /* @__PURE__ */ React.createElement(Market, { d }) : page === "Risk & Orders" ? /* @__PURE__ */ React.createElement(Risk, { d }) : page === "Decision JSON" ? /* @__PURE__ */ React.createElement(JSONPage, { d }) : /* @__PURE__ */ React.createElement("div", { className: "pages" }, /* @__PURE__ */ React.createElement(Panel, { title: page }, /* @__PURE__ */ React.createElement("p", null, "This page is wired to the same live decision state. Detailed modules can expand here without changing the core data contract.")))));
  }
  ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
})();

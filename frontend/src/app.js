const { useEffect, useMemo, useRef, useState } = React;
const h = React.createElement;

const TFs = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"];
const Pages = [
  ["trade", "Trade Cockpit"],
  ["markets", "Market Context"],
  ["signals", "Signal Engine"],
  ["risk", "Risk & Orders"],
  ["journal", "Journal"],
  ["settings", "Settings"],
];

function cls(...items) { return items.filter(Boolean).join(" "); }
function fmt(n, d=2) { const x = Number(n); return Number.isFinite(x) ? x.toLocaleString(undefined,{maximumFractionDigits:d, minimumFractionDigits:d}) : "—"; }
function compactAction(action) {
  if (!action) return "WAIT";
  if (String(action).includes("TRADE_READY")) return "PAPER TRADE READY";
  return String(action).replaceAll("_", " ");
}
function verdictClass(action, side) {
  const a = String(action || "").toLowerCase();
  if (a.includes("trade_ready")) return side === "sell" ? "verdict sell" : "verdict buy";
  if (a.includes("wait")) return "verdict wait";
  return "verdict watch";
}
function ageText(ts) {
  if (!ts) return "—";
  const t = new Date(ts).getTime();
  if (!Number.isFinite(t)) return "—";
  const s = Math.max(0, Math.round((Date.now()-t)/1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s/60)}m ago`;
  return `${Math.round(s/3600)}h ago`;
}

async function getJson(url, fallback) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (e) { return fallback; }
}

function useLiveData(tf) {
  const [decision, setDecision] = useState(null);
  const [candles, setCandles] = useState({ candles: [], source: "loading" });
  const [alerts, setAlerts] = useState([]);
  useEffect(() => {
    let alive = true;
    async function loadDecision() {
      const d = await getJson("/api/decision", {});
      if (alive) setDecision(d);
    }
    loadDecision();
    const id = setInterval(loadDecision, 15000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  useEffect(() => {
    let alive = true;
    async function loadCandles() {
      const c = await getJson(`/api/candles?tf=${encodeURIComponent(tf)}&count=280`, { candles: [], source: "error" });
      if (alive) setCandles(c);
    }
    loadCandles();
    const id = setInterval(loadCandles, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [tf]);
  useEffect(() => {
    let alive = true;
    async function loadAlerts() {
      const a = await getJson("/api/alerts?limit=20", { alerts: [] });
      if (alive) setAlerts(a.alerts || []);
    }
    loadAlerts();
    const id = setInterval(loadAlerts, 60000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return { decision: decision || {}, candles, alerts };
}

function Sidebar({ page, setPage, decision }) {
  return h("aside", { className: "sidebar" },
    h("div", { className: "brand" },
      h("div", { className: "brandMark" }, "Au"),
      h("div", null, h("strong", null, "Gold Trader"), h("span", null, "Command Center"))
    ),
    h("nav", null, Pages.map(([id, label]) => h("button", { key:id, onClick:()=>setPage(id), className: cls(page===id && "active") }, label))),
    h("div", { className: "sideStatus" },
      h("span", null, "Verdict"), h("b", null, compactAction(decision.action)),
      h("span", null, "Score"), h("b", null, `${decision.final_score ?? 0}/100`),
      h("span", null, "Mode"), h("b", null, (decision.cloud_status?.execution_mode || "paper").toUpperCase())
    )
  );
}

function TopBar({ decision, refresh }) {
  const cs = decision.cloud_status || {};
  return h("header", { className: "topbar" },
    h("div", null,
      h("h1", null, "Gold Trader Command Center"),
      h("p", null, "Full-system IFVG analysis, market awareness, and execution safety.")),
    h("div", { className: "topPills" },
      h("span", null, "Symbol ", h("b", null, decision.symbol || "XAU/USD")),
      h("span", null, "Data ", h("b", null, cs.data_provider || "twelvedata")),
      h("span", null, "Orders ", h("b", null, cs.orders || "locked")),
      h("span", null, "Updated ", h("b", null, ageText(decision.timestamp_utc))),
      h("button", { onClick: refresh, className: "refresh" }, "Refresh")
    )
  );
}

function VerdictHero({ decision }) {
  const clsName = verdictClass(decision.action, decision.side);
  const hasTrade = String(decision.action || "").includes("TRADE_READY");
  return h("section", { className: clsName },
    h("div", { className: "verdictLeft" },
      h("span", { className: "eyebrow" }, "LIVE VERDICT"),
      h("h2", null, compactAction(decision.action)),
      h("p", null, `${decision.symbol || "XAU/USD"} · ${String(decision.side || "none").toUpperCase()} · Grade ${decision.final_grade || "—"}`),
      h("div", { className: "verdictAdvice" }, decision.next_update || "Waiting for the next scan.")
    ),
    h("div", { className: "scoreTower" },
      h("div", { className: "scoreRing", style: {"--score": Math.min(100, Number(decision.final_score||0))} },
        h("strong", null, decision.final_score ?? 0), h("span", null, "/100")),
      h("div", { className: "grade" }, decision.final_grade || "—")
    ),
    h("div", { className: "priceGrid" },
      h(MiniMetric, { label:"Current", value:fmt(decision.current_price, 2) }),
      h(MiniMetric, { label:"Entry", value:`${fmt(decision.entry_low,2)} – ${fmt(decision.entry_high,2)}` }),
      h(MiniMetric, { label:"Stop", value:fmt(decision.stop_loss,2) }),
      h(MiniMetric, { label:"Targets", value:`${fmt(decision.tp1,2)} / ${fmt(decision.tp2,2)} / ${fmt(decision.tp3,2)}` })
    ),
    hasTrade && h("div", { className: "ribbon" }, "Paper alert only · live execution locked")
  );
}
function MiniMetric({ label, value }) { return h("div", { className:"miniMetric" }, h("span", null, label), h("b", null, value)); }

function CandleChart({ candles, tf }) {
  const ref = useRef(null);
  useEffect(() => {
    const canvas = ref.current; if (!canvas) return;
    const parent = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    const w = parent.clientWidth; const hgt = parent.clientHeight;
    canvas.width = w * ratio; canvas.height = hgt * ratio; canvas.style.width = w+"px"; canvas.style.height = hgt+"px";
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0,0,w,hgt);
    const data = (candles || []).filter(c => Number.isFinite(+c.open) && Number.isFinite(+c.high) && Number.isFinite(+c.low) && Number.isFinite(+c.close)).slice(-180);
    ctx.fillStyle = "#080d14"; ctx.fillRect(0,0,w,hgt);
    ctx.strokeStyle = "rgba(148,163,184,.13)"; ctx.lineWidth = 1;
    for (let i=0;i<6;i++){ const y=30+i*(hgt-58)/5; ctx.beginPath(); ctx.moveTo(18,y); ctx.lineTo(w-18,y); ctx.stroke(); }
    if (!data.length) { ctx.fillStyle="#94a3b8"; ctx.font="14px ui-monospace, monospace"; ctx.fillText("Waiting for candle feed…", 28, 52); return; }
    const highs = data.map(c=>+c.high), lows = data.map(c=>+c.low);
    const max = Math.max(...highs), min = Math.min(...lows), pad = (max-min)*0.08 || 1;
    const y = v => 22 + (max+pad-v)/(max-min+pad*2)*(hgt-50);
    const gap = (w-44)/data.length;
    const bodyW = Math.max(2, Math.min(10, gap*.62));
    data.forEach((c,i)=>{
      const x = 22+i*gap+gap/2; const o=+c.open, close=+c.close, hi=+c.high, lo=+c.low;
      const up = close >= o;
      ctx.strokeStyle = up ? "#21d07a" : "#ff5364"; ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath(); ctx.moveTo(x,y(hi)); ctx.lineTo(x,y(lo)); ctx.stroke();
      const top = Math.min(y(o), y(close)); const bh = Math.max(1, Math.abs(y(o)-y(close)));
      ctx.fillRect(x-bodyW/2, top, bodyW, bh);
    });
    ctx.fillStyle = "#94a3b8"; ctx.font="12px ui-monospace, monospace";
    ctx.fillText(`${tf} · ${data.length} candles · ${data[data.length-1].time || ""}`, 22, 20);
    ctx.fillStyle="#f8fafc"; ctx.fillText(fmt(data[data.length-1].close,2), w-100, 20);
  }, [candles, tf]);
  return h("div", { className:"chartBox" }, h("canvas", { ref }));
}

function TimeframeStrip({ reads }) {
  const list = TFs.map(tf => (reads || []).find(r => r.timeframe === tf) || { timeframe: tf });
  return h("section", { className:"panel tfPanel" },
    h("div", { className:"panelHead" }, h("h3", null, "Timeframe alignment"), h("span", null, `${list.filter(x=>x.ifvg_side && x.ifvg_side !== "none").length}/7 active IFVG reads`)),
    h("div", { className:"tfStrip" }, list.map(r => h("div", { key:r.timeframe, className: cls("tfCard", r.ifvg_side, r.bias) },
      h("b", null, r.timeframe), h("span", null, r.bias || "unknown"), h("strong", null, `IFVG: ${r.ifvg_side || "none"}`), h("em", null, `${r.score ?? 0} pts`)
    )))
  );
}

function TextPanel({ title, items, empty="No items" }) {
  const arr = Array.isArray(items) ? items : [];
  return h("section", { className:"panel textPanel" }, h("div", { className:"panelHead" }, h("h3", null, title)),
    arr.length ? h("ul", null, arr.map((x,i)=>h("li", { key:i }, String(x)))) : h("p", { className:"muted" }, empty));
}

function ContextGrid({ decision }) {
  const mc = decision.market_context || {}; const cs = decision.cloud_status || {}; const live = decision.live_market_context || {};
  const cells = [
    ["Analysis", cs.analysis || "online"], ["Candles", cs.candles_loaded ?? "—"], ["Data", cs.data_provider || "twelvedata"],
    ["Volatility", mc.volatility_state || cs.volatility || "unknown"], ["Spread", mc.spread_points ?? cs.spread ?? "unknown"],
    ["Macro", mc.macro_state || cs.macro || live.macro_state?.state || "unknown"], ["Sentiment", mc.sentiment_state || cs.sentiment || live.sentiment_state?.state || "unknown"],
    ["Broker", cs.broker || "preview"], ["Orders", cs.orders || "locked"]
  ];
  return h("section", { className:"panel" }, h("div", { className:"panelHead" }, h("h3", null, "Live context")),
    h("div", { className:"contextGrid" }, cells.map(([k,v]) => h("div", { className:"contextCell", key:k }, h("span", null, k), h("b", null, String(v))))));
}

function TradePage({ decision, candles, tf, setTf }) {
  return h("main", { className:"page tradePage" },
    h(VerdictHero, { decision }),
    h("section", { className:"workbench" },
      h("div", { className:"chartPanel panel" },
        h("div", { className:"panelHead" }, h("h3", null, "Live candlestick workbench"),
          h("div", { className:"tfButtons" }, TFs.map(x => h("button", { key:x, onClick:()=>setTf(x), className: cls(tf===x && "active") }, x)))) ,
        h(CandleChart, { candles: candles.candles || [], tf }),
        h("div", { className:"chartFoot" }, h("span", null, `Source: ${candles.source || "—"}`), h("span", null, `${candles.count || 0} candles`))
      ),
      h("div", { className:"rightStack" },
        h(TextPanel, { title:"What to do now", items:[decision.next_update || "Wait for a Grade-A aligned IFVG setup."] }),
        h(TextPanel, { title:"Why", items:decision.reasons }),
        h(TextPanel, { title:"Blockers", items:decision.blockers, empty:"No blockers" })
      )
    ),
    h(TimeframeStrip, { reads: decision.timeframe_reads })
  );
}

function MarketsPage({ decision }) { return h("main", { className:"page" }, h("div", { className:"grid2" }, h(ContextGrid,{decision}), h(TextPanel,{title:"Market warnings", items: decision.market_context?.warnings || []}), h(TextPanel,{title:"Context notes", items: decision.market_context?.notes || []}), h(TextPanel,{title:"Cross-market / institutional", items:["CME/options: pending provider credentials", "COT: pending live context feed", "DXY/yields/VIX: available after live context merge"]}))); }
function SignalsPage({ decision }) { return h("main", { className:"page" }, h(TimeframeStrip,{reads:decision.timeframe_reads}), h("div", { className:"grid2" }, h(TextPanel,{title:"IFVG reasons", items:decision.reasons}), h(TextPanel,{title:"Current blockers", items:decision.blockers, empty:"No blockers"}))); }
function RiskPage({ decision }) { const g=decision.daily_guard||{}; return h("main", { className:"page" }, h("div", { className:"grid4" }, h(Metric,{label:"Trades today", value:g.trades_taken??0}), h(Metric,{label:"Losses", value:g.losses_taken??0}), h(Metric,{label:"Open positions", value:g.open_positions??0}), h(Metric,{label:"Daily guard", value:g.blocked?"Blocked":"Clear"})), h(TextPanel,{title:"Guard reasons", items:g.reasons||[], empty:"Daily guard is clear"}), h(TextPanel,{title:"Execution policy", items:["Maximum 3 trades per day", "One open position", "Stop after losses", "Live orders locked unless GOLD_ENABLE_LIVE_ORDERS=true"]})); }
function JournalPage({ alerts }) { return h("main", { className:"page" }, h("section", { className:"panel" }, h("div", { className:"panelHead" }, h("h3", null, "Operator alerts")), alerts.length ? h("div", { className:"alertList" }, alerts.map((a,i)=>h("pre", { key:i }, JSON.stringify(a,null,2)))) : h("p", { className:"muted" }, "No alerts yet."))); }
function SettingsPage({ decision }) { return h("main", { className:"page" }, h("div", { className:"grid2" }, h(ContextGrid,{decision}), h(TextPanel,{title:"Runtime safety", items:[`Mode: ${decision.cloud_status?.execution_mode || "paper"}`, `Orders: ${decision.cloud_status?.orders || "locked"}`, "Secrets are never displayed in this UI."]}))); }
function Metric({label,value}) { return h("div", { className:"metric" }, h("span", null, label), h("b", null, value)); }

function App() {
  const [page, setPage] = useState("trade"); const [tf, setTf] = useState("M15");
  const data = useLiveData(tf);
  const d = data.decision || {};
  const refresh = () => { window.location.reload(); };
  let content = null;
  if (page === "trade") content = h(TradePage, { decision:d, candles:data.candles, tf, setTf });
  else if (page === "markets") content = h(MarketsPage, { decision:d });
  else if (page === "signals") content = h(SignalsPage, { decision:d });
  else if (page === "risk") content = h(RiskPage, { decision:d });
  else if (page === "journal") content = h(JournalPage, { alerts:data.alerts });
  else content = h(SettingsPage, { decision:d });
  return h("div", { className:"app" }, h(Sidebar,{page,setPage,decision:d}), h("div", { className:"shell" }, h(TopBar,{decision:d,refresh}), content));
}

ReactDOM.createRoot(document.getElementById("root")).render(h(App));

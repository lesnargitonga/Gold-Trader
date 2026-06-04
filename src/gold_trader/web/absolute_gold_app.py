from __future__ import annotations

import html
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", str(Path(__file__).resolve().parents[3]))
).resolve()
LOGS = ROOT / "logs"
DECISION_PATH = Path(os.getenv("GOLD_DECISION_STATE_PATH", str(LOGS / "ifvg_mtf_decision_state.json")))
BRIEF_PATH = Path(os.getenv("GOLD_OPERATOR_BRIEF_PATH", str(LOGS / "ifvg_mtf_operator_brief.md")))
ALERTS_PATH = Path(os.getenv("GOLD_OPERATOR_ALERTS_PATH", str(LOGS / "operator_alerts.jsonl")))
JOURNAL_PATH = Path(os.getenv("GOLD_TRADE_JOURNAL_PATH", str(LOGS / "trade_journal.jsonl")))

TF_ORDER = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def _read_jsonl(path: Path, limit: int = 40) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _decision() -> dict[str, Any]:
    data = _read_json(DECISION_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("timestamp_utc", None)
    data.setdefault("symbol", os.getenv("GOLD_SYMBOL", "XAU/USD"))
    data.setdefault("action", "WAIT")
    data.setdefault("side", "none")
    data.setdefault("final_grade", "—")
    data.setdefault("final_score", 0)
    data.setdefault("current_price", None)
    data.setdefault("reasons", [])
    data.setdefault("blockers", [])
    data.setdefault("next_update", "Waiting for the next full-system scan.")
    data.setdefault("timeframe_reads", [])
    data.setdefault("market_context", {})
    data.setdefault("daily_guard", {})
    data.setdefault("operator_message", _read_text(BRIEF_PATH, ""))
    data["ui_status"] = _system_status(data)
    live_ctx = _read_json(LOGS / "live_market_context.json", {})
    if live_ctx:
        data["live_market_context"] = live_ctx
    return data


def _system_status(decision: dict[str, Any]) -> dict[str, Any]:
    tf_reads = decision.get("timeframe_reads") or []
    candle_total = sum(int((r or {}).get("candles") or 0) for r in tf_reads if isinstance(r, dict))
    market = decision.get("market_context") or {}
    provider = os.getenv("GOLD_MARKET_DATA_PROVIDER") or ("twelvedata" if os.getenv("TWELVE_DATA_API_KEY") else "unknown")
    orders_enabled = os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true"
    execution = os.getenv("GOLD_EXECUTION_MODE", "paper")
    return {
        "data_provider": provider,
        "cloud_candles": candle_total > 0,
        "candles_total": candle_total,
        "execution_mode": execution,
        "live_orders": orders_enabled,
        "order_lock": "UNLOCKED" if orders_enabled else "LOCKED",
        "spread_state": "available" if market.get("spread_points") is not None else "unknown",
        "macro_state": market.get("macro_state") or "unknown",
        "sentiment_state": market.get("sentiment_state") or "unknown",
        "volatility_state": market.get("volatility_state") or "unknown",
        "broker_state": "cTrader pending / paper mode" if os.getenv("GOLD_BROKER", "paper") == "ctrader" else os.getenv("GOLD_BROKER", "paper"),
        "updated_utc": _now(),
    }


def _candles(tf: str, count: int = 220) -> dict[str, Any]:
    tf = tf.upper()
    if tf not in TF_ORDER:
        tf = "M15"
    count = max(20, min(int(count), 500))
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL") or "XAU/USD"
    source = "none"
    candles: list[dict[str, Any]] = []
    error: str | None = None
    try:
        from gold_trader.data.twelvedata import fetch_candles
        candles = fetch_candles(tf, symbol=symbol, count=count)  # type: ignore[arg-type]
        source = "twelvedata"
    except Exception as exc:
        error = str(exc)
        # Fallback: use timeframe read price if candle source is unavailable.
        candles = []
    return {"symbol": symbol, "timeframe": tf, "source": source, "candles": candles, "error": error, "timestamp_utc": _now()}


def _fmt_price(v: Any) -> str:
    try:
        if v is None:
            return "—"
        return f"{float(v):,.2f}"
    except Exception:
        return "—"


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _age(ts: Any) -> str:
    if not ts:
        return "no scan yet"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        return f"{seconds // 3600}h ago"
    except Exception:
        return "unknown age"


CSS = r"""
:root{--bg:#07080c;--panel:#0d111a;--panel2:#111724;--ink:#eef3ff;--muted:#8f9bb1;--line:#202838;--gold:#f6c35b;--gold2:#7b5520;--green:#2de38d;--red:#ff5e73;--blue:#72a7ff;--warn:#ffcc66;--shadow:0 22px 70px rgba(0,0,0,.42);--r:22px}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0%,rgba(246,195,91,.12),transparent 30%),radial-gradient(circle at 85% 12%,rgba(114,167,255,.10),transparent 34%),var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;min-height:100vh}.shell{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:24px 18px;border-right:1px solid var(--line);background:linear-gradient(180deg,rgba(13,17,26,.95),rgba(7,8,12,.94));backdrop-filter:blur(20px)}.brand{display:flex;gap:13px;align-items:center;margin-bottom:28px}.mark{width:45px;height:45px;border-radius:15px;background:linear-gradient(135deg,var(--gold),#fff2bd 45%,#8b5d19);box-shadow:0 0 40px rgba(246,195,91,.22)}.brand h1{font-size:18px;margin:0;letter-spacing:.12em;text-transform:uppercase}.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}.nav a{display:flex;justify-content:space-between;align-items:center;color:#cbd4e8;text-decoration:none;padding:13px 14px;border-radius:14px;margin:5px 0;border:1px solid transparent}.nav a:hover,.nav a.active{background:rgba(246,195,91,.10);border-color:rgba(246,195,91,.24);color:#fff}.nav small{color:var(--muted)}.side-card{position:absolute;left:18px;right:18px;bottom:20px;padding:15px;border-radius:18px;background:rgba(255,255,255,.04);border:1px solid var(--line)}.side-card b{color:var(--gold)}.main{padding:24px;overflow:hidden}.topbar{display:grid;grid-template-columns:1.2fr repeat(5,auto);gap:12px;align-items:center;margin-bottom:18px}.title h2{margin:0;font-size:28px;letter-spacing:-.04em}.title p{margin:4px 0 0;color:var(--muted)}.pill{padding:10px 13px;border-radius:999px;background:rgba(255,255,255,.05);border:1px solid var(--line);font-weight:700;color:#dbe4f8}.pill.gold{color:var(--gold);border-color:rgba(246,195,91,.34)}.pill.green{color:var(--green)}.pill.red{color:var(--red)}.page{display:none}.page.active{display:block}.grid-trade{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(360px,.9fr);gap:18px}.panel{background:linear-gradient(180deg,rgba(17,23,36,.94),rgba(10,13,20,.92));border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden}.panel-h{display:flex;justify-content:space-between;align-items:center;padding:18px 20px;border-bottom:1px solid var(--line)}.panel-h h3{margin:0;font-size:14px;text-transform:uppercase;letter-spacing:.11em;color:#cfd8eb}.panel-b{padding:18px 20px}.hero{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:center;background:linear-gradient(135deg,rgba(246,195,91,.16),rgba(114,167,255,.06));border:1px solid rgba(246,195,91,.25);border-radius:24px;padding:20px;margin-bottom:18px}.action{font-size:54px;font-weight:900;letter-spacing:-.08em;line-height:.9}.action.wait{color:var(--warn)}.action.buy{color:var(--green)}.action.sell{color:var(--red)}.score{width:120px;height:120px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--gold) calc(var(--score)*1%),rgba(255,255,255,.07) 0);position:relative}.score:before{content:"";position:absolute;inset:9px;background:#0c1018;border-radius:50%}.score span{position:relative;font-size:30px;font-weight:900}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{padding:14px;border-radius:18px;background:rgba(255,255,255,.045);border:1px solid var(--line)}.metric small{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metric b{display:block;margin-top:6px;font-size:20px}.chart-wrap{height:430px;padding:12px}.chart-tools{display:flex;gap:8px;flex-wrap:wrap}.tfbtn{background:#0b0f18;color:#dce5f8;border:1px solid var(--line);border-radius:12px;padding:8px 11px;cursor:pointer;font-weight:800}.tfbtn.active{background:rgba(246,195,91,.16);border-color:rgba(246,195,91,.45);color:var(--gold)}canvas{width:100%;height:100%;display:block}.tf-strip{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-top:14px}.tf-card{padding:13px;border-radius:16px;background:rgba(255,255,255,.045);border:1px solid var(--line);min-height:92px}.tf-card strong{display:flex;justify-content:space-between}.tf-card small{color:var(--muted)}.tf-card.buy{border-color:rgba(45,227,141,.35)}.tf-card.sell{border-color:rgba(255,94,115,.35)}.brief{display:grid;gap:12px}.brief-box{padding:15px;border-radius:18px;background:rgba(255,255,255,.045);border:1px solid var(--line)}.brief-box h4{margin:0 0 8px;color:var(--gold);font-size:13px;text-transform:uppercase;letter-spacing:.1em}.brief-box ul{margin:0;padding-left:18px;color:#dbe4f8}.brief-box li{margin:6px 0}.context-grid,.market-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.context-card{padding:17px;border-radius:18px;background:rgba(255,255,255,.045);border:1px solid var(--line)}.context-card small{color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.context-card b{display:block;margin-top:8px;font-size:18px}.table{width:100%;border-collapse:collapse}.table th,.table td{text-align:left;border-bottom:1px solid var(--line);padding:12px;color:#dbe4f8}.table th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.settings{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.status-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--green);margin-right:7px}.muted{color:var(--muted)}.kbd{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#070a10;border:1px solid var(--line);padding:2px 6px;border-radius:8px}.footer-note{margin-top:12px;color:var(--muted);font-size:12px}@media(max-width:1100px){.shell{grid-template-columns:1fr}.sidebar{height:auto;position:relative}.side-card{position:static;margin-top:16px}.topbar{grid-template-columns:1fr 1fr}.grid-trade,.context-grid,.market-grid,.settings{grid-template-columns:1fr}.tf-strip{grid-template-columns:repeat(2,1fr)}}
"""


JS = r"""
const state={tf:'M15',decision:null,candles:[]};
const qs=(s)=>document.querySelector(s);const qsa=(s)=>Array.from(document.querySelectorAll(s));
function money(v){if(v===null||v===undefined||Number.isNaN(Number(v)))return '—';return Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});}
function setPage(p){qsa('.page').forEach(x=>x.classList.remove('active'));qsa('.nav a').forEach(x=>x.classList.remove('active'));qs('#page-'+p)?.classList.add('active');qs(`[data-page="${p}"]`)?.classList.add('active');history.replaceState(null,'','#'+p)}
async function api(path){const r=await fetch(path,{cache:'no-store'});return await r.json();}
async function loadDecision(){state.decision=await api('/api/decision');renderDecision();}
async function loadCandles(tf=state.tf){state.tf=tf;qsa('.tfbtn').forEach(b=>b.classList.toggle('active',b.dataset.tf===tf));const res=await api('/api/candles?tf='+encodeURIComponent(tf)+'&count=260');state.candles=res.candles||[];drawChart();}
function renderDecision(){const d=state.decision||{};const ui=d.ui_status||{};const action=String(d.action||'WAIT').toLowerCase();qs('#action').textContent=d.action||'WAIT';qs('#action').className='action '+action;qs('#score').style.setProperty('--score',Math.max(0,Math.min(100,Number(d.final_score||0))));qs('#scoreNum').textContent=Number(d.final_score||0).toFixed(0);qs('#grade').textContent='Grade '+(d.final_grade||'—');qs('#symbol').textContent=d.symbol||'XAU/USD';qs('#price').textContent=money(d.current_price);qs('#updated').textContent=d.timestamp_utc?('Updated '+d.timestamp_utc.replace('T',' ').slice(0,19)+' UTC'):'Waiting for first scan';qs('#provider').textContent=(ui.data_provider||'unknown').toUpperCase();qs('#mode').textContent=(ui.execution_mode||'paper').toUpperCase();qs('#lock').textContent=(ui.order_lock||'LOCKED');qs('#entry').textContent=(money(d.entry_low)+' – '+money(d.entry_high));qs('#sl').textContent=money(d.stop_loss);qs('#tp1').textContent=money(d.tp1);qs('#tp2').textContent=money(d.tp2);qs('#tp3').textContent=money(d.tp3);qs('#next').textContent=d.next_update||'Waiting for clean Grade-A alignment.';list('#reasons',d.reasons||[]);list('#blockers',d.blockers||[]);renderTF(d.timeframe_reads||[]);renderContext(d);renderRisk(d);}
function list(sel,items){const el=qs(sel);el.innerHTML='';if(!items.length){el.innerHTML='<li class="muted">None</li>';return}items.slice(0,7).forEach(x=>{const li=document.createElement('li');li.textContent=x;el.appendChild(li)})}
function renderTF(reads){const by={};reads.forEach(r=>by[r.timeframe]=r);const el=qs('#tfstrip');el.innerHTML='';['D1','H4','H1','M30','M15','M5','M1'].forEach(tf=>{const r=by[tf]||{};const side=r.ifvg_side||'none';const div=document.createElement('div');div.className='tf-card '+side;div.innerHTML=`<strong><span>${tf}</span><span>${side}</span></strong><small>${r.bias||'unknown'} · ${r.candles||0} candles</small><br><small>Score ${r.score||0}</small>`;el.appendChild(div)})}
function renderContext(d){const m=d.market_context||{},ui=d.ui_status||{};const rows=[['Data provider',ui.data_provider||'unknown',ui.cloud_candles?'Live candles active':'No candle source'],['Volatility',m.volatility_state||'unknown',(m.notes||[]).find(x=>String(x).includes('volatility'))||'—'],['Spread',m.spread_points??'unknown',(m.warnings||[]).find(x=>String(x).includes('spread'))||'—'],['Macro',m.macro_state||'unknown',(m.notes||[]).find(x=>String(x).includes('calendar'))||'—'],['Sentiment',m.sentiment_state||'unknown',m.sentiment_score==null?'score unavailable':'score '+m.sentiment_score],['Broker / Orders',ui.broker_state||'paper',ui.live_orders?'Live orders enabled':'Live orders locked']];const el=qs('#context');el.innerHTML='';rows.forEach(r=>{const div=document.createElement('div');div.className='context-card';div.innerHTML=`<small>${r[0]}</small><b>${r[1]}</b><p class="muted">${r[2]}</p>`;el.appendChild(div)})}
function renderRisk(d){const g=d.daily_guard||{};qs('#risk').innerHTML=`<tr><td>Trades today</td><td>${g.trades_taken??0}/3</td></tr><tr><td>Losses today</td><td>${g.losses_taken??0}/2</td></tr><tr><td>Open positions</td><td>${g.open_positions??0}/1</td></tr><tr><td>Guard</td><td>${g.blocked?'BLOCKED':'CLEAR'}</td></tr>`;}
function drawChart(){const canvas=qs('#chart');if(!canvas)return;const rect=canvas.getBoundingClientRect();const dpr=window.devicePixelRatio||1;canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,rect.width,rect.height);c.fillStyle='#080b12';c.fillRect(0,0,rect.width,rect.height);const candles=state.candles.slice(-160);if(candles.length<2){c.fillStyle='#8f9bb1';c.font='15px Inter, sans-serif';c.fillText('Waiting for live candles…',28,48);return}const highs=candles.map(x=>Number(x.high)),lows=candles.map(x=>Number(x.low));let hi=Math.max(...highs),lo=Math.min(...lows);const pad=(hi-lo)*.08||1;hi+=pad;lo-=pad;const xstep=rect.width/candles.length;function y(v){return 18+(hi-v)/(hi-lo)*(rect.height-40)}c.strokeStyle='rgba(255,255,255,.06)';c.lineWidth=1;for(let i=0;i<6;i++){const yy=18+i*(rect.height-40)/5;c.beginPath();c.moveTo(0,yy);c.lineTo(rect.width,yy);c.stroke()}candles.forEach((k,i)=>{const o=Number(k.open),h=Number(k.high),l=Number(k.low),cl=Number(k.close);const x=i*xstep+xstep/2;const up=cl>=o;c.strokeStyle=up?'#2de38d':'#ff5e73';c.fillStyle=c.strokeStyle;c.beginPath();c.moveTo(x,y(h));c.lineTo(x,y(l));c.stroke();const bh=Math.max(2,Math.abs(y(o)-y(cl)));c.fillRect(x-xstep*.31,Math.min(y(o),y(cl)),Math.max(2,xstep*.62),bh)});c.fillStyle='#f6c35b';c.font='12px Inter, sans-serif';c.fillText(`${state.tf} · ${candles.length} candles · ${money(candles[candles.length-1].close)}`,18,20)}
async function boot(){const page=(location.hash||'#trade').slice(1);setPage(page);qsa('.nav a').forEach(a=>a.onclick=(e)=>{e.preventDefault();setPage(a.dataset.page)});qsa('.tfbtn').forEach(b=>b.onclick=()=>loadCandles(b.dataset.tf));await loadDecision();await loadCandles('M15');setInterval(loadDecision,15000);setInterval(()=>loadCandles(state.tf),60000);}
window.addEventListener('resize',drawChart);window.addEventListener('load',boot);
"""


def _layout(page: str = "trade") -> str:
    d = _decision()
    action = _esc(d.get("action") or "WAIT")
    price = _fmt_price(d.get("current_price"))
    score = int(float(d.get("final_score") or 0))
    grade = _esc(d.get("final_grade") or "—")
    symbol = _esc(d.get("symbol") or os.getenv("GOLD_SYMBOL", "XAU/USD"))
    updated = _esc(_age(d.get("timestamp_utc")))
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Gold Trader Command Center</title><style>{CSS}</style></head><body><div class='shell'><aside class='sidebar'><div class='brand'><div class='mark'></div><div><h1>Gold Trader</h1><p>Absolute command center</p></div></div><nav class='nav'><a href='#trade' data-page='trade' class='active'>Trade <small>live</small></a><a href='#market' data-page='market'>Market <small>context</small></a><a href='#signals' data-page='signals'>Signals <small>IFVG</small></a><a href='#risk' data-page='risk'>Risk <small>guards</small></a><a href='#journal' data-page='journal'>Journal <small>paper</small></a><a href='#settings' data-page='settings'>Settings <small>cloud</small></a></nav><div class='side-card'><span class='status-dot'></span><b>Cloud analysis active</b><p class='muted'>Twelve Data candles feed the engine when MT5 is offline. Broker execution remains locked until explicitly enabled.</p></div></aside><main class='main'><div class='topbar'><div class='title'><h2>Command Center</h2><p id='updated'>Updated {updated}</p></div><div class='pill gold' id='symbol'>{symbol}</div><div class='pill' id='price'>{price}</div><div class='pill' id='grade'>Grade {grade}</div><div class='pill' id='provider'>DATA</div><div class='pill red' id='lock'>LOCKED</div></div>{_trade_page(score, action)}{_market_page()}{_signals_page()}{_risk_page()}{_journal_page()}{_settings_page()}</main></div><script>{JS}</script></body></html>"""


def _trade_page(score: int, action: str) -> str:
    return f"""<section id='page-trade' class='page active'><div class='grid-trade'><div><div class='panel'><div class='panel-h'><h3>Live Candlestick Workbench</h3><div class='chart-tools'>{''.join(f"<button class='tfbtn {'active' if tf=='M15' else ''}' data-tf='{tf}'>{tf}</button>" for tf in TF_ORDER)}</div></div><div class='chart-wrap'><canvas id='chart'></canvas></div></div><div id='tfstrip' class='tf-strip'></div></div><div><div class='hero'><div><div id='action' class='action {action.lower()}'>{action}</div><p class='muted'>Execution stays paper until every risk and broker switch is intentionally unlocked.</p><span class='pill' id='mode'>PAPER</span></div><div id='score' class='score' style='--score:{score}'><span id='scoreNum'>{score}</span></div></div><div class='panel'><div class='panel-h'><h3>Trade Decision</h3><span class='pill gold'>IFVG-only</span></div><div class='panel-b'><div class='metrics'><div class='metric'><small>Entry</small><b id='entry'>—</b></div><div class='metric'><small>Stop</small><b id='sl'>—</b></div><div class='metric'><small>TP1</small><b id='tp1'>—</b></div><div class='metric'><small>TP2 / TP3</small><b><span id='tp2'>—</span> / <span id='tp3'>—</span></b></div></div><div class='brief' style='margin-top:14px'><div class='brief-box'><h4>What to do now</h4><p id='next' class='muted'>Waiting for scan.</p></div><div class='brief-box'><h4>Why</h4><ul id='reasons'></ul></div><div class='brief-box'><h4>Blockers</h4><ul id='blockers'></ul></div></div></div></div></div></div></section>"""


def _market_page() -> str:
    return """<section id='page-market' class='page'><div class='panel'><div class='panel-h'><h3>Live Market Context</h3><span class='pill gold'>Cloud-first</span></div><div class='panel-b'><div id='context' class='context-grid'></div><p class='footer-note'>Macro, sentiment, spread, COT, DXY, yields, and VIX should enter here as separate live context feeds. Unknown context blocks live execution but does not prevent paper analysis.</p></div></div></section>"""


def _signals_page() -> str:
    return """<section id='page-signals' class='page'><div class='panel'><div class='panel-h'><h3>IFVG Signal Matrix</h3><span class='pill'>7 TF confirmation</span></div><div class='panel-b'><div id='tfstrip-copy' class='market-grid'></div><table class='table'><thead><tr><th>Layer</th><th>Requirement</th><th>Status</th></tr></thead><tbody><tr><td>Execution model</td><td>Inversion FVG only</td><td>Active</td></tr><tr><td>Confirmation</td><td>D1→M1 voting, minimum 5 aligned</td><td>Enforced</td></tr><tr><td>Quality</td><td>Grade A score gate</td><td>Enforced</td></tr><tr><td>Safety</td><td>Max 3 trades/day, max 1 open position</td><td>Enforced</td></tr></tbody></table></div></div></section>"""


def _risk_page() -> str:
    return """<section id='page-risk' class='page'><div class='panel'><div class='panel-h'><h3>Risk Command</h3><span class='pill red'>Live orders locked</span></div><div class='panel-b'><table class='table'><tbody id='risk'></tbody></table><p class='footer-note'>Risk controls must remain server-side. The UI never unlocks live orders; only environment and broker policy can do that.</p></div></div></section>"""


def _journal_page() -> str:
    alerts = _read_jsonl(ALERTS_PATH, 20)
    rows = "".join(f"<tr><td>{_esc(a.get('timestamp_utc') or a.get('time') or '')}</td><td>{_esc(a.get('action') or a.get('event') or 'alert')}</td><td>{_esc(a.get('message') or a.get('summary') or '')}</td></tr>" for a in alerts) or "<tr><td colspan='3' class='muted'>No alerts recorded yet.</td></tr>"
    return f"""<section id='page-journal' class='page'><div class='panel'><div class='panel-h'><h3>Journal & Alerts</h3><span class='pill'>Paper evidence</span></div><div class='panel-b'><table class='table'><thead><tr><th>Time</th><th>Event</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table></div></div></section>"""


def _settings_page() -> str:
    keys = ["GOLD_MARKET_DATA_PROVIDER", "GOLD_SYMBOL", "GOLD_EXECUTION_MODE", "GOLD_ENABLE_LIVE_ORDERS", "GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "GOLD_BROKER"]
    cards = "".join(f"<div class='context-card'><small>{_esc(k)}</small><b>{_esc(os.getenv(k, 'not set'))}</b></div>" for k in keys)
    return f"""<section id='page-settings' class='page'><div class='panel'><div class='panel-h'><h3>Cloud Runtime</h3><span class='pill green'>Render</span></div><div class='panel-b'><div class='settings'>{cards}</div><p class='footer-note'>Secrets are intentionally not displayed. API keys, tokens, and broker credentials should live only in Render Environment.</p></div></div></section>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AbsoluteGold/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("GOLD_UI_ACCESS_LOGS", "false").lower() == "true":
            super().log_message(fmt, *args)

    def _send(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8"), code, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path in {"/", "/trade", "/market", "/signals", "/risk", "/journal", "/settings"}:
                self._send(_layout().encode("utf-8"))
            elif path == "/api/decision":
                self._json(_decision())
            elif path == "/api/candles":
                self._json(_candles(params.get("tf", ["M15"])[0], int(params.get("count", ["220"])[0])))
            elif path == "/api/alerts":
                self._json({"alerts": _read_jsonl(ALERTS_PATH, 50), "timestamp_utc": _now()})
            elif path == "/health":
                d = _decision()
                self._json({"ok": True, "service": "absolute-gold", "decision_age": _age(d.get("timestamp_utc")), "status": d.get("ui_status", {})})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": "command center request failed", "detail": str(exc) if os.getenv("GOLD_UI_DEBUG") == "true" else "hidden"}, 500)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("HOST", "0.0.0.0")
    port = int(port or os.getenv("PORT", "8770"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"absolute-gold UI: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

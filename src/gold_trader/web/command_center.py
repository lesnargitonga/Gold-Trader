from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(
    os.getenv("GOLD_PROJECT_ROOT", str(Path(__file__).resolve().parents[3]))
).resolve()
LOG_DIR = ROOT / "logs"
STATE_PATH = Path(os.getenv("GOLD_DECISION_STATE_PATH", str(LOG_DIR / "ifvg_mtf_decision_state.json")))
BRIEF_PATH = Path(os.getenv("GOLD_OPERATOR_BRIEF_PATH", str(LOG_DIR / "ifvg_mtf_operator_brief.md")))
ALERTS_PATH = Path(os.getenv("GOLD_ALERTS_PATH", str(LOG_DIR / "operator_alerts.jsonl")))
CACHE_DIR = ROOT / "data" / "cache" / "command_center"

TF_ORDER = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]
TD_INTERVALS = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        f = float(value)
        if f != f:
            return default
        return f
    except Exception:
        return default


def fmt_price(value: Any) -> str:
    f = safe_float(value)
    if f is None:
        return "—"
    return f"{f:,.2f}"


def decision_state() -> dict[str, Any]:
    state = read_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    state.setdefault("timestamp_utc", utc_now())
    state.setdefault("symbol", os.getenv("GOLD_SYMBOL", "XAU/USD"))
    state.setdefault("action", "WAIT")
    state.setdefault("side", "none")
    state.setdefault("final_grade", "—")
    state.setdefault("final_score", 0)
    state.setdefault("timeframe_reads", [])
    state.setdefault("market_context", {})
    state.setdefault("daily_guard", {})
    state.setdefault("reasons", [])
    state.setdefault("blockers", [])
    state.setdefault("next_update", "Waiting for the next full-system scan.")

    state["cloud_status"] = cloud_status(state)
    return state


def cloud_status(state: dict[str, Any]) -> dict[str, Any]:
    reads = state.get("timeframe_reads") or []
    candle_counts = [int(r.get("candles") or 0) for r in reads if isinstance(r, dict)]
    has_candles = any(c > 0 for c in candle_counts)
    market = state.get("market_context") or {}
    live_orders = os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true"
    execution_mode = os.getenv("GOLD_EXECUTION_MODE", "paper")
    provider = os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata")

    return {
        "analysis": "online" if has_candles else "waiting_for_data",
        "data_provider": provider,
        "candles_loaded": sum(candle_counts),
        "orders": "unlocked" if live_orders else "locked",
        "execution_mode": execution_mode,
        "broker": os.getenv("GOLD_BROKER", "preview"),
        "macro": market.get("macro_state") or "unknown",
        "sentiment": market.get("sentiment_state") or "unknown",
        "volatility": market.get("volatility_state") or "unknown",
        "spread": market.get("spread_points"),
    }


def read_alerts(limit: int = 20) -> list[dict[str, Any]]:
    if not ALERTS_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = ALERTS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        for line in lines:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    except Exception:
        return []
    return out[::-1]


def fetch_twelvedata_candles(tf: str, outputsize: int = 160) -> list[dict[str, Any]]:
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        return []
    interval = TD_INTERVALS.get(tf.upper())
    if not interval:
        return []
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    symbol = {"XAUUSD": "XAU/USD", "GOLD": "XAU/USD"}.get(symbol.upper().replace(" ", ""), symbol)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"candles_{tf.upper()}.json"
    ttl = int(os.getenv("GOLD_TWELVE_DATA_CACHE_SECONDS", "120"))
    try:
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
    except Exception:
        pass
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "outputsize": str(outputsize), "apikey": key})
    url = f"https://api.twelvedata.com/time_series?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        values = payload.get("values") or []
        candles: list[dict[str, Any]] = []
        for row in reversed(values):
            try:
                candles.append({
                    "time": row.get("datetime") or row.get("time") or "",
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
            except Exception:
                continue
        cache_path.write_text(json.dumps(candles), encoding="utf-8")
        return candles
    except Exception:
        return []


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, allow_nan=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Gold Trader Command Center</title>
<style>
:root{--bg:#05070b;--panel:#0b1018;--panel2:#101722;--line:#1f2a37;--text:#f5f7fb;--muted:#93a4b7;--gold:#f4c430;--gold2:#a77b19;--green:#2ee58b;--red:#ff5a66;--blue:#53a7ff;--amber:#ffbe45;--shadow:0 24px 70px rgba(0,0,0,.45);--radius:22px}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 16% -10%,rgba(244,196,48,.16),transparent 35%),radial-gradient(circle at 100% 0,rgba(83,167,255,.13),transparent 35%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}.app{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.side{border-right:1px solid var(--line);background:rgba(6,9,14,.82);backdrop-filter:blur(18px);padding:24px 18px;position:sticky;top:0;height:100vh}.brand{display:flex;gap:12px;align-items:center;margin-bottom:28px}.mark{width:44px;height:44px;border-radius:15px;background:linear-gradient(135deg,var(--gold),#ffd86d 45%,#7a5710);box-shadow:0 0 40px rgba(244,196,48,.35)}.brand h1{font-size:19px;line-height:1.05;margin:0}.brand small{color:var(--muted);letter-spacing:.12em;text-transform:uppercase}.nav a{display:flex;justify-content:space-between;align-items:center;color:#c9d4e3;text-decoration:none;padding:13px 14px;border-radius:14px;margin:4px 0}.nav a.active,.nav a:hover{background:linear-gradient(90deg,rgba(244,196,48,.15),rgba(244,196,48,.04));color:#fff}.badge{font-size:11px;color:#111;background:var(--gold);padding:3px 7px;border-radius:20px;font-weight:800}.foot{position:absolute;bottom:22px;color:var(--muted);font-size:12px}.main{padding:22px 28px 40px}.top{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:center;margin-bottom:20px}.title h2{font-size:28px;margin:0 0 5px}.title p{margin:0;color:var(--muted)}.statusbar{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.pill{border:1px solid var(--line);background:rgba(16,23,34,.8);border-radius:999px;padding:9px 12px;font-size:12px;color:#dce6f2}.pill strong{color:#fff}.grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(360px,.55fr);gap:18px}.card{background:linear-gradient(180deg,rgba(16,23,34,.96),rgba(8,12,18,.96));border:1px solid rgba(255,255,255,.08);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}.card-h{display:flex;justify-content:space-between;align-items:center;padding:18px 20px;border-bottom:1px solid var(--line)}.card-h h3{margin:0;font-size:14px;letter-spacing:.08em;text-transform:uppercase}.card-b{padding:20px}.hero{display:grid;grid-template-columns:1fr 130px;gap:18px}.action{font-size:58px;letter-spacing:-.05em;font-weight:900;margin:0}.action.WAIT,.action.NONE{color:var(--amber)}.action.BUY{color:var(--green)}.action.SELL{color:var(--red)}.subline{color:var(--muted);font-size:15px}.score{width:124px;height:124px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--gold) calc(var(--score)*1%),rgba(255,255,255,.08) 0);position:relative}.score:after{content:"";position:absolute;inset:10px;border-radius:50%;background:var(--panel)}.score div{position:relative;z-index:1;text-align:center}.score b{font-size:30px}.score span{display:block;color:var(--muted);font-size:11px}.levels{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}.metric{border:1px solid var(--line);background:rgba(255,255,255,.03);border-radius:16px;padding:12px}.metric span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metric b{display:block;margin-top:5px;font-size:16px}.chart-wrap{height:430px;position:relative}.chart-empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);text-align:center}.chart-empty b{color:#fff}.tfgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}.tf{border:1px solid var(--line);border-radius:18px;padding:12px;background:rgba(255,255,255,.025)}.tf .name{font-weight:900}.tf .bias{margin-top:8px;font-size:12px;color:var(--muted)}.tf.buy{border-color:rgba(46,229,139,.45);box-shadow:inset 0 0 0 1px rgba(46,229,139,.12)}.tf.sell{border-color:rgba(255,90,102,.45);box-shadow:inset 0 0 0 1px rgba(255,90,102,.12)}.tf.none{opacity:.82}.list{margin:0;padding-left:18px;color:#dbe6f3}.list li{margin:9px 0}.blockers li{color:#ffd0d3}.context{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.ctx{padding:13px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.025)}.ctx span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.ctx b{display:block;margin-top:6px}.next{font-size:16px;line-height:1.55;color:#eaf1fa}.rightcol{display:grid;gap:18px}.alert{padding:12px 14px;border-radius:14px;background:rgba(244,196,48,.08);border:1px solid rgba(244,196,48,.22);color:#fff}.muted{color:var(--muted)}.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.refresh{cursor:pointer;border:1px solid rgba(244,196,48,.45);background:rgba(244,196,48,.12);color:#ffe59a;border-radius:12px;padding:10px 14px;font-weight:800}@media(max-width:1100px){.app{grid-template-columns:1fr}.side{height:auto;position:relative}.grid{grid-template-columns:1fr}.levels{grid-template-columns:repeat(2,1fr)}.tfgrid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="app"><aside class="side"><div class="brand"><div class="mark"></div><div><h1>Gold Trader</h1><small>Command Center</small></div></div><nav class="nav"><a class="active" href="/"><span>Trade cockpit</span><span class="badge">XAU</span></a><a href="/api/decision"><span>Decision JSON</span></a><a href="/health"><span>Health</span></a></nav><div class="foot"><div id="footTime">—</div><div>Paper-first · Live locked</div></div></aside><main class="main"><section class="top"><div class="title"><h2>Gold Trader Command Center</h2><p>Full-system IFVG analysis, market context, and execution safety in one cockpit.</p></div><div class="statusbar"><span class="pill">Data: <strong id="dataProvider">—</strong></span><span class="pill">Mode: <strong id="mode">—</strong></span><span class="pill">Orders: <strong id="orders">—</strong></span><button class="refresh" onclick="loadAll()">Refresh</button></div></section><section class="grid"><div><div class="card"><div class="card-h"><h3>Live decision</h3><span class="muted" id="timestamp">—</span></div><div class="card-b"><div class="hero"><div><h1 id="action" class="action WAIT">WAIT</h1><div class="subline"><span id="symbol">XAU/USD</span> · <span id="side">none</span> · Grade <b id="grade">—</b></div></div><div class="score" id="scoreRing" style="--score:0"><div><b id="score">0</b><span>score</span></div></div></div><div class="levels"><div class="metric"><span>Current</span><b id="price">—</b></div><div class="metric"><span>Entry</span><b id="entry">—</b></div><div class="metric"><span>Stop</span><b id="stop">—</b></div><div class="metric"><span>Targets</span><b id="targets">—</b></div></div></div></div><div class="card" style="margin-top:18px"><div class="card-h"><h3>Chart</h3><span class="muted">M15 live preview</span></div><div class="chart-wrap"><canvas id="chart" width="1200" height="430"></canvas><div id="chartEmpty" class="chart-empty"><div><b>Loading cloud candles</b><br/>If empty, verify Twelve Data key and symbol.</div></div></div></div><div class="card" style="margin-top:18px"><div class="card-h"><h3>Timeframe alignment</h3><span class="muted" id="alignText">—</span></div><div class="card-b"><div class="tfgrid" id="tfgrid"></div></div></div></div><div class="rightcol"><div class="card"><div class="card-h"><h3>What to do now</h3></div><div class="card-b"><div class="next" id="nextUpdate">Waiting for scan.</div></div></div><div class="card"><div class="card-h"><h3>Why</h3></div><div class="card-b"><ul class="list" id="reasons"></ul></div></div><div class="card"><div class="card-h"><h3>Blockers</h3></div><div class="card-b"><ul class="list blockers" id="blockers"></ul></div></div><div class="card"><div class="card-h"><h3>Live context</h3></div><div class="card-b"><div class="context" id="context"></div></div></div><div class="card"><div class="card-h"><h3>Daily guard</h3></div><div class="card-b"><div class="context" id="guard"></div></div></div></div></section></main></div>
<script>
const $=id=>document.getElementById(id);const fmt=v=>{if(v===null||v===undefined||Number.isNaN(Number(v)))return '—';return Number(v).toLocaleString(undefined,{maximumFractionDigits:2,minimumFractionDigits:2})};function li(items,el,empty='None'){el.innerHTML='';(items&&items.length?items:[empty]).forEach(x=>{const n=document.createElement('li');n.textContent=x;el.appendChild(n)})}function ctx(label,value,klass=''){return `<div class="ctx"><span>${label}</span><b class="${klass}">${value??'—'}</b></div>`}function setDecision(d){const action=(d.action||'WAIT').toUpperCase();$('action').textContent=action;$('action').className='action '+action;$('symbol').textContent=d.symbol||'XAU/USD';$('side').textContent=d.side||'none';$('grade').textContent=d.final_grade||'—';let score=Math.max(0,Math.min(100,Number(d.final_score||0)));$('score').textContent=score;$('scoreRing').style.setProperty('--score',score);$('price').textContent=fmt(d.current_price);$('entry').textContent=(d.entry_low||d.entry_high)?`${fmt(d.entry_low)} – ${fmt(d.entry_high)}`:'—';$('stop').textContent=fmt(d.stop_loss);$('targets').textContent=[d.tp1,d.tp2,d.tp3].map(fmt).join(' / ');$('timestamp').textContent=d.timestamp_utc||'—';$('nextUpdate').textContent=d.next_update||'Waiting for scan.';li(d.reasons,$('reasons'));li(d.blockers,$('blockers'),'No blockers');const cs=d.cloud_status||{};$('dataProvider').textContent=cs.data_provider||'—';$('mode').textContent=cs.execution_mode||'paper';$('orders').textContent=cs.orders||'locked';$('footTime').textContent=new Date().toISOString().slice(0,19)+'Z';const market=d.market_context||{};$('context').innerHTML=ctx('Analysis',cs.analysis,cs.analysis==='online'?'good':'warn')+ctx('Candles',cs.candles_loaded||0)+ctx('Volatility',market.volatility_state||'unknown')+ctx('Spread',market.spread_points??'unknown')+ctx('Macro',market.macro_state||'unknown')+ctx('Sentiment',market.sentiment_state||'unknown')+ctx('Broker',cs.broker||'preview')+ctx('Orders',cs.orders||'locked',cs.orders==='locked'?'warn':'good');const g=d.daily_guard||{};$('guard').innerHTML=ctx('Trades today',g.trades_taken??0)+ctx('Losses today',g.losses_taken??0)+ctx('Open positions',g.open_positions??0)+ctx('Blocked',g.blocked?'yes':'no',g.blocked?'bad':'good');renderTF(d.timeframe_reads||[])}function renderTF(reads){const by={};reads.forEach(r=>by[r.timeframe]=r);let aligned=0,total=0;const html=['D1','H4','H1','M30','M15','M5','M1'].map(tf=>{const r=by[tf]||{};const side=(r.ifvg_side||'none').toLowerCase();if(r.candles>0) total++;if(side==='buy'||side==='sell') aligned++;return `<div class="tf ${side}"><div class="name">${tf}</div><div class="bias">${r.bias||'unknown'}</div><div class="bias">IFVG: ${r.ifvg_side||'none'}</div><div class="bias">Candles: ${r.candles||0}</div><div class="bias">Score: ${r.score||0}</div></div>`}).join('');$('tfgrid').innerHTML=html;$('alignText').textContent=`${aligned}/${total} active IFVG reads`}function draw(c){const canvas=$('chart'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);if(!c||!c.length){$('chartEmpty').style.display='grid';return}$('chartEmpty').style.display='none';const pad=36;const highs=c.map(x=>x.high),lows=c.map(x=>x.low);const max=Math.max(...highs),min=Math.min(...lows);const xstep=(w-pad*2)/c.length;const y=v=>h-pad-((v-min)/(max-min||1))*(h-pad*2);ctx.strokeStyle='rgba(255,255,255,.08)';ctx.lineWidth=1;for(let i=0;i<6;i++){let yy=pad+i*(h-pad*2)/5;ctx.beginPath();ctx.moveTo(pad,yy);ctx.lineTo(w-pad,yy);ctx.stroke()}c.forEach((k,i)=>{const x=pad+i*xstep+xstep/2;const up=k.close>=k.open;ctx.strokeStyle=up?'#2ee58b':'#ff5a66';ctx.fillStyle=up?'rgba(46,229,139,.72)':'rgba(255,90,102,.72)';ctx.beginPath();ctx.moveTo(x,y(k.high));ctx.lineTo(x,y(k.low));ctx.stroke();const top=Math.min(y(k.open),y(k.close)),bot=Math.max(y(k.open),y(k.close));ctx.fillRect(x-xstep*.28,top,Math.max(2,xstep*.56),Math.max(2,bot-top))})}async function loadAll(){try{const d=await fetch('/api/decision',{cache:'no-store'}).then(r=>r.json());setDecision(d)}catch(e){}try{const c=await fetch('/api/candles?tf=M15',{cache:'no-store'}).then(r=>r.json());draw(c.candles||[])}catch(e){draw([])}}loadAll();setInterval(loadAll,30000);
</script>
</body></html>'''


class CommandCenterHandler(BaseHTTPRequestHandler):
    server_version = "GoldTraderCommandCenter/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[command-center] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/trade"}:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/decision":
            json_response(self, decision_state())
            return
        if parsed.path == "/api/alerts":
            json_response(self, {"alerts": read_alerts()})
            return
        if parsed.path == "/api/candles":
            query = urllib.parse.parse_qs(parsed.query)
            tf = (query.get("tf") or ["M15"])[0]
            json_response(self, {"timeframe": tf, "candles": fetch_twelvedata_candles(tf)})
            return
        if parsed.path == "/health":
            d = decision_state()
            json_response(self, {"ok": True, "timestamp_utc": utc_now(), "state_exists": STATE_PATH.exists(), "cloud_status": d.get("cloud_status")})
            return
        json_response(self, {"error": "not found"}, status=404)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("HOST", "0.0.0.0")
    port = int(port or os.getenv("PORT", "8770"))
    httpd = ThreadingHTTPServer((host, port), CommandCenterHandler)
    print(f"gold-trader command center: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

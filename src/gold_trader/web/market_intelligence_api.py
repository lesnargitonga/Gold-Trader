from __future__ import annotations

import json
import os
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gold_trader.core.market_intelligence_ux import (
    PROVIDER_HEALTH_PATH,
    find_decision_path,
    harden_decision,
    read_json,
)

ROOT = Path(os.getenv("GOLD_TRADER_ROOT", "."))


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, allow_nan=False, indent=2).encode("utf-8")


def _candles(tf: str) -> dict[str, Any]:
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    try:
        from gold_trader.data.twelvedata import fetch_candles
        candles = fetch_candles(tf.upper(), symbol=symbol, count=int(os.getenv("GOLD_CHART_CANDLE_COUNT", "280")))
        return {"ok": True, "tf": tf.upper(), "provider": "twelvedata", "candles": candles, "count": len(candles), "volume_note": "XAU/USD volume may be 0.0 on this feed."}
    except Exception as exc:
        return {"ok": False, "tf": tf.upper(), "provider": "twelvedata", "error": str(exc), "candles": [], "count": 0}


INDEX = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gold Trader Command Center</title>
<style>
:root{--bg:#06090d;--panel:#0d141e;--panel2:#101b28;--line:#1c2b3b;--text:#eef3fb;--muted:#92a0b2;--gold:#f7c948;--red:#ff5770;--green:#19d18f;--amber:#f5b942;--blue:#69a7ff;--shadow:0 24px 80px rgba(0,0,0,.38)}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at top left,#16202d 0,#06090d 35%,#020409 100%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;letter-spacing:.01em}button{font:inherit} .app{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{background:#070b11;border-right:1px solid var(--line);padding:24px 16px;position:sticky;top:0;height:100vh}.brand{display:flex;align-items:center;gap:14px;margin-bottom:28px}.coin{width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,#ffe27a,#b98716);box-shadow:0 0 24px rgba(247,201,72,.25)}.brand h1{font-size:16px;margin:0;letter-spacing:.08em}.brand p{font-size:11px;margin:3px 0 0;color:var(--muted);letter-spacing:.24em}.nav a{display:flex;justify-content:space-between;align-items:center;padding:13px 14px;margin:6px 0;border-radius:12px;color:#cbd5e1;text-decoration:none;border:1px solid transparent}.nav a.active,.nav a:hover{background:rgba(247,201,72,.10);border-color:rgba(247,201,72,.28);color:#fff}.mini{position:absolute;left:16px;right:16px;bottom:22px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px;color:var(--muted);font-size:12px}.mini b{float:right;color:white}.main{padding:26px 30px}.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:18px}.title h2{font-size:28px;margin:0;letter-spacing:.16em}.title p{margin:8px 0 0;color:var(--muted)}.chips{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.chip{border:1px solid var(--line);background:#0b121b;border-radius:999px;padding:9px 13px;color:var(--muted);font-size:12px}.chip b{color:white}.chip.lock{background:rgba(255,87,112,.15);border-color:rgba(255,87,112,.45);color:#ffd2da}.chip.ok{background:rgba(25,209,143,.12);border-color:rgba(25,209,143,.35)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}.dot.ok{background:var(--green)}.dot.warning{background:var(--amber)}.dot.danger{background:var(--red)}.grid{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:18px}.card{background:linear-gradient(180deg,rgba(16,27,40,.96),rgba(9,15,23,.96));border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden}.card h3{margin:0;padding:16px 18px;border-bottom:1px solid var(--line);font-size:13px;letter-spacing:.23em;text-transform:uppercase}.hero{padding:24px 24px 20px;border-color:rgba(247,201,72,.28)}.heroRow{display:grid;grid-template-columns:1fr 160px;gap:18px}.label{color:var(--gold);font-size:12px;text-transform:uppercase;letter-spacing:.25em}.verdict{font-size:52px;line-height:.95;margin:12px 0 6px;font-weight:900;letter-spacing:.13em}.meta{color:var(--muted)}.brief{margin-top:14px;max-width:760px;border:1px solid var(--line);background:#0b121b;border-radius:12px;padding:12px 14px;color:#d8e0ea}.scoreRing{width:126px;height:126px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--gold) calc(var(--score)*1%),#1b2939 0);padding:10px}.scoreInner{width:104px;height:104px;border-radius:50%;background:#08101a;display:grid;place-items:center;text-align:center}.scoreInner strong{font-size:30px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}.stat{background:#0c1520;border:1px solid var(--line);border-radius:14px;padding:13px}.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.18em}.stat b{display:block;margin-top:7px;font-size:16px}.chartHead{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}.tfBtns button{background:#0b121b;border:1px solid var(--line);color:#b7c2d1;border-radius:9px;padding:8px 10px;margin-left:5px;cursor:pointer}.tfBtns button.active{background:var(--gold);color:#171100;border-color:var(--gold);font-weight:800}.chartMeta{font-size:12px;color:var(--muted);padding:12px 18px 0}canvas{width:100%;height:390px;display:block}.right{display:flex;flex-direction:column;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden}.panel h4{margin:0;padding:14px 16px;border-bottom:1px solid var(--line);font-size:12px;text-transform:uppercase;letter-spacing:.25em}.panel .body{padding:14px 16px;color:#cbd5e1}.panel ul{margin:0;padding-left:18px}.panel li{margin:8px 0}.control{border-color:rgba(255,87,112,.45);background:linear-gradient(180deg,rgba(70,20,32,.65),rgba(14,18,26,.95))}.scoreGrid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-top:18px}.scoreBox{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b121b}.scoreBox .name{color:var(--muted);font-size:11px;min-height:32px;text-transform:uppercase}.scoreBox .num{font-size:20px;font-weight:900}.scoreBox.bad{border-color:rgba(255,87,112,.4)}.scoreBox.ok{border-color:rgba(25,209,143,.35)}.tfGrid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;padding:14px 16px}.tfCard{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b121b}.tfCard.aligned{border-color:rgba(25,209,143,.45);box-shadow:inset 0 0 0 1px rgba(25,209,143,.08)}.tfCard h5{margin:0 0 8px;font-size:15px}.tfCard p{margin:4px 0;color:var(--muted);font-size:12px}.contextGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.ctx{background:#0c1520;border:1px solid var(--line);border-radius:13px;padding:12px}.ctx span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.18em}.ctx b{display:block;margin-top:7px}.danger{color:var(--red)}.okText{color:var(--green)}.amber{color:var(--amber)}.page{display:none}.page.active{display:block}.wide{grid-column:1/-1}.json{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#b9c8d8;background:#060a10;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto;max-height:70vh}@media(max-width:1200px){.app{grid-template-columns:1fr}.side{position:relative;height:auto}.mini{position:static;margin-top:20px}.grid{grid-template-columns:1fr}.stats,.scoreGrid,.tfGrid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body><div id="root"></div><script>
const TFs=['D1','H4','H1','M30','M15','M5','M1'];
let state={page:'trade',tf:'M15',decision:null,health:null,candles:[],candleMeta:null};
const $=s=>document.querySelector(s);
function fmt(n,d=2){return n===null||n===undefined||Number.isNaN(Number(n))?'—':Number(n).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d})}
function safe(v,f='—'){return v===null||v===undefined||v===''?f:v}
function actionLabel(a){a=String(a||'WAIT').toUpperCase(); if(a.includes('HARD')) return 'WAIT HARD BLOCK'; if(a.includes('TRADE_READY')) return 'PAPER TRADE READY'; return a.replaceAll('_',' ')}
async function j(url){const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' '+r.status); return r.json()}
async function load(){try{state.decision=await j('/api/decision');state.health=await j('/api/provider-health')}catch(e){console.error(e)} await loadCandles(state.tf); render()}
async function loadCandles(tf){state.tf=tf; try{const c=await j('/api/candles?tf='+encodeURIComponent(tf));state.candles=c.candles||[];state.candleMeta=c}catch(e){state.candles=[];state.candleMeta={ok:false,error:String(e)}} drawSoon()}
function nav(page){state.page=page; render(); drawSoon()}
function top(d){const age=d.source_age_status||{};const health=d.provider_health_summary||{};return `<aside class="side"><div class="brand"><div class="coin"></div><div><h1>Gold Trader</h1><p>COMMAND CENTER</p></div></div><nav class="nav">${['trade:Trade Cockpit','market:Market Context','signal:Signal Engine','risk:Risk & Orders','journal:Journal','settings:Settings','json:Decision JSON'].map(x=>{const [k,v]=x.split(':');return `<a class="${state.page===k?'active':''}" href="#" onclick="nav('${k}')"><span>${v}</span>${k==='trade'?'<b>XAU</b>':''}</a>`}).join('')}</nav><div class="mini">Verdict <b>${actionLabel(d.action)}</b><br/>Score <b>${safe(d.final_score,0)}/100</b><br/>Mode <b>${(d.cloud_status||{}).execution_mode||'paper'}</b><br/>Age <b>${safe(age.age_seconds,'—')}s</b></div></aside><main class="main"><div class="top"><div class="title"><h2>Gold Trader Command Center</h2><p>Full-system IFVG analysis, market awareness, and execution safety.</p></div><div class="chips"><span class="chip">Symbol <b>${safe(d.symbol,'XAUUSD')}</b></span><span class="chip">Data <b>${((d.cloud_status||{}).data_provider)||'twelvedata'}</b></span><span class="chip lock">Orders <b>${d.live_orders_enabled?'open':'locked'}</b></span><span class="chip"><i class="dot ${age.severity||'warning'}"></i>${age.label||'unknown'}</span><button class="chip ok" onclick="load()">Refresh</button></div></div>`}
function hero(d){const sd=d.score_decomposition||{};return `<section class="card hero"><div class="heroRow"><div><div class="label">Live Verdict</div><div class="verdict">${actionLabel(d.action)}</div><div class="meta">${safe(d.symbol,'XAUUSD')} · ${String(d.side||'none').toUpperCase()} · Grade ${safe(d.final_grade,'—')} · Source age ${(d.source_age_status||{}).age_seconds??'—'}s</div><div class="brief">${safe(d.next_update,'Waiting for a fresh full-system scan.')}</div></div><div class="scoreRing" style="--score:${Math.max(0,Math.min(100,Number(d.final_score||0)))}"><div class="scoreInner"><div><strong>${safe(d.final_score,0)}</strong><br/><span>/100</span><br/><small>${safe(d.final_grade,'—')}</small></div></div></div></div><div class="stats"><div class="stat"><span>Current</span><b>${fmt(d.current_price)}</b></div><div class="stat"><span>Entry</span><b>${fmt(d.entry_low)} – ${fmt(d.entry_high)}</b></div><div class="stat"><span>Stop</span><b>${fmt(d.stop_loss)}</b></div><div class="stat"><span>Targets</span><b>${fmt(d.tp1)} / ${fmt(d.tp2)} / ${fmt(d.tp3)}</b></div></div><div class="scoreGrid">${Object.entries(sd).map(([k,v])=>`<div class="scoreBox ${Number(v.score||0)===0?'bad':'ok'}"><div class="name">${v.label||k}</div><div class="num">${v.score||0}<small>/${v.max||0}</small></div></div>`).join('')}</div>${d.data_quality_penalty?`<div class="brief danger">−${d.data_quality_penalty} pts data-quality penalty. Missing: ${(d.missing_inputs||[]).map(x=>x.label).join(', ')}</div>`:''}</section>`}
function drawSoon(){setTimeout(draw,30)}
function chart(){return `<section class="card"><div class="chartHead"><h3 style="border:0;padding:0">Live Candlestick Workbench</h3><div class="tfBtns">${TFs.map(tf=>`<button class="${state.tf===tf?'active':''}" onclick="loadCandles('${tf}')">${tf}</button>`).join('')}</div></div><div class="chartMeta">${state.tf} · ${(state.candleMeta||{}).count||state.candles.length} candles · ${(state.candleMeta||{}).provider||'—'} · ${((state.candleMeta||{}).ok===false)?'<span class="danger">'+(state.candleMeta.error||'feed error')+'</span>':'live feed'} · Volume note: ${(state.candleMeta||{}).volume_note||'—'}</div><canvas id="chart" width="1100" height="420"></canvas></section>`}
function draw(){const c=$('#chart'); if(!c||!state.candles?.length)return; const ctx=c.getContext('2d'), W=c.width,H=c.height,p=28; ctx.clearRect(0,0,W,H); ctx.fillStyle='#071019';ctx.fillRect(0,0,W,H); const data=state.candles.slice(-180); const hi=Math.max(...data.map(x=>Number(x.high))); const lo=Math.min(...data.map(x=>Number(x.low))); const y=v=>p+(hi-v)/(hi-lo||1)*(H-p*2); const x=i=>p+i*(W-p*2)/Math.max(1,data.length-1); ctx.strokeStyle='#162334';ctx.lineWidth=1; for(let i=0;i<6;i++){let yy=p+i*(H-p*2)/5;ctx.beginPath();ctx.moveTo(p,yy);ctx.lineTo(W-p,yy);ctx.stroke()} data.forEach((k,i)=>{let xx=x(i),o=y(Number(k.open)),h=y(Number(k.high)),l=y(Number(k.low)),cl=y(Number(k.close));let up=Number(k.close)>=Number(k.open);ctx.strokeStyle=up?'#16d68f':'#ff5470';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(xx,h);ctx.lineTo(xx,l);ctx.stroke();let bw=Math.max(2,(W-p*2)/data.length*.55);ctx.fillRect(xx-bw/2,Math.min(o,cl),bw,Math.max(1,Math.abs(cl-o))) }); ctx.fillStyle='#94a3b8';ctx.font='12px ui-monospace';ctx.fillText(fmt(hi),W-80,p+8);ctx.fillText(fmt(lo),W-80,H-p)}
function lists(d){return `<div class="right"><div class="panel control"><h4>Operator Control</h4><div class="body"><b>${d.live_orders_enabled?'LIVE ORDERS OPEN':'LIVE ORDERS LOCKED'}</b><p>${d.live_orders_enabled?'Execution is enabled. Confirm broker/spread before firing.':'Paper/alert only. Live execution cannot fire from this UI.'}</p></div></div><div class="panel"><h4>Watching For</h4><div class="body"><ul>${(d.watching_for||[]).map(x=>`<li>${x}</li>`).join('')||'<li>No active checklist</li>'}</ul></div></div><div class="panel"><h4>Why</h4><div class="body"><ul>${(d.readable_reasons||d.reasons||[]).map(x=>`<li>${x}</li>`).join('')||'<li>No items</li>'}</ul></div></div><div class="panel"><h4>Blockers</h4><div class="body"><ul>${(d.readable_blockers||d.blockers||[]).map(x=>`<li>${x}</li>`).join('')||'<li>No blockers</li>'}</ul></div></div>${contextPanel(d)}</div>`}
function contextPanel(d){const m=d.market_intelligence_summary||{}, c=d.cloud_status||{};return `<div class="panel"><h4>Live Context</h4><div class="body contextGrid"><div class="ctx"><span>Analysis</span><b class="okText">online</b></div><div class="ctx"><span>Candles</span><b>${safe(c.candles_loaded,'—')}</b></div><div class="ctx"><span>Provider</span><b>${safe(c.data_provider,'—')}</b></div><div class="ctx"><span>Volatility</span><b>${safe(m.volatility,'unknown')}</b></div><div class="ctx"><span>Spread</span><b class="${m.spread==='unknown'?'amber':''}">${safe(m.spread,'unknown')}</b></div><div class="ctx"><span>Macro</span><b class="${m.macro==='unknown'?'amber':''}">${safe(m.macro,'unknown')}</b></div><div class="ctx"><span>Sentiment</span><b>${safe(m.sentiment,'unknown')}</b></div><div class="ctx"><span>Orders</span><b class="amber">${d.live_orders_enabled?'open':'locked'}</b></div><div class="ctx"><span>CME</span><b>${safe(m.cme,'not_connected')}</b></div><div class="ctx"><span>Options</span><b>${safe(m.options,'not_connected')}</b></div></div></div>`}
function tfGrid(d){const t=d.tf_align||{}; return `<section class="card wide"><h3>Timeframe Alignment</h3><div class="tfGrid">${TFs.map(tf=>{const r=t[tf]||{};return `<div class="tfCard ${r.aligned?'aligned':''}"><h5>${tf}</h5><p>Bias: ${safe(r.bias,'—')}</p><p>IFVG: ${safe(r.ifvg_side,'—')}</p><p>Score: ${safe(r.score,'—')}</p><p>Candles: ${safe(r.candles,0)}</p></div>`}).join('')}</div></section>`}
function trade(d){return `<div class="grid"><div>${hero(d)}<div style="height:16px"></div>${chart()}</div>${lists(d)}${tfGrid(d)}</div>`}
function market(d){return `<div class="grid"><section class="card"><h3>Market Intelligence</h3><div class="tfGrid">${Object.entries(d.provider_health_summary||{}).map(([k,v])=>`<div class="tfCard"><h5>${v.label||k}</h5><p>State: ${v.state||'unknown'}</p><p>${k}</p></div>`).join('')}</div></section>${lists(d)}</div>`}
function signal(d){return `<div class="grid"><section class="card"><h3>Signal Engine</h3><div class="body" style="padding:18px"><h2>Alignment Audit</h2><pre class="json">${JSON.stringify(d.alignment_audit||{},null,2)}</pre></div></section>${lists(d)}${tfGrid(d)}</div>`}
function risk(d){return `<div class="grid"><section class="card"><h3>Risk & Orders</h3><div class="body" style="padding:18px"><pre class="json">${JSON.stringify({daily_guard:d.daily_guard, live_orders_enabled:d.live_orders_enabled, missing_inputs:d.missing_inputs},null,2)}</pre></div></section>${lists(d)}</div>`}
function journal(d){return `<section class="card"><h3>Journal & Evidence</h3><div class="body" style="padding:18px"><p>Decision snapshots are written to <b>logs/decision_snapshots/</b> on each hardening pass.</p><p>Next: wire paper-trade entries, R-multiple tracking, expectancy, and screenshot/evidence capture.</p></div></section>`}
function settings(d){return `<section class="card"><h3>Settings & Health</h3><div class="body" style="padding:18px"><pre class="json">${JSON.stringify(d.provider_health_summary||{},null,2)}</pre></div></section>`}
function render(){const d=state.decision||{}; let body=''; if(state.page==='trade')body=trade(d); if(state.page==='market')body=market(d); if(state.page==='signal')body=signal(d); if(state.page==='risk')body=risk(d); if(state.page==='journal')body=journal(d); if(state.page==='settings')body=settings(d); if(state.page==='json')body=`<section class="card"><h3>Decision JSON</h3><pre class="json">${JSON.stringify(d,null,2)}</pre></section>`; document.getElementById('root').innerHTML=`<div class="app">${top(d)}${body}</main></div>`; drawSoon()}
load(); setInterval(load,15000);
</script></body></html>'''


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/decision":
            return self.json(harden_decision())
        if path == "/api/provider-health" or path == "/api/health":
            data = read_json(PROVIDER_HEALTH_PATH, {})
            if not data:
                data = harden_decision().get("provider_health_summary", {})
            return self.json({"ok": True, **data} if path == "/api/health" else data)
        if path == "/api/market-intelligence":
            return self.json(harden_decision().get("market_intelligence_summary", {}))
        if path == "/api/candles":
            tf = (query.get("tf") or ["M15"])[0]
            return self.json(_candles(tf))
        if path in {"/", "/index.html", "/trade", "/market", "/markets", "/signal", "/risk", "/journal", "/settings"}:
            body = INDEX.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(_safe_json({"error": "not found"}))

    def json(self, payload: Any) -> None:
        body = _safe_json(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "0.0.0.0", port: int = 8770) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"gold-trader market intelligence UI: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8770")))

from __future__ import annotations

import json
import os
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gold_trader.core.market_intelligence_ux import (
    PROVIDER_HEALTH_PATH,
    get_decision_for_api,
    read_json,
)

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))).resolve()
COMMAND_CENTER_JS = _PKG_ROOT / "frontend" / "market_intelligence" / "command_center.js"


def _base_candidates() -> list[Path]:
    bases: list[Path] = []
    for raw in (
        os.getenv("GOLD_TRADER_ROOT"),
        os.getenv("GOLD_RUNTIME_ROOT"),
        os.getenv("RENDER_PROJECT_DIR"),
        os.getenv("PWD"),
        str(Path.cwd()),
        str(ROOT),
        str(_PKG_ROOT),
        "/opt/render/project/src",
    ):
        if not raw:
            continue
        try:
            base = Path(raw).resolve()
        except OSError:
            continue
        if base not in bases:
            bases.append(base)
    return bases


def _command_center_js_candidates() -> list[Path]:
    paths = [COMMAND_CENTER_JS]
    for base in _base_candidates():
        candidate = base / "frontend" / "market_intelligence" / "command_center.js"
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _fallback_command_center_js() -> bytes:
    return """(function(){
  'use strict';
  var root=document.getElementById('root');
  function esc(v){return String(v==null?'':v).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function get(path){return fetch(path,{cache:'no-store'}).then(function(r){if(!r.ok)throw new Error(path+' '+r.status);return r.json();});}
  function render(decision,health,candles,error){
    var d=decision||{}, h=health||{}, c=candles||{};
    root.innerHTML='<div class="app"><aside class="side"><div class="brand"><div class="coin"></div><div><h1>Gold Trader</h1><p>COMMAND CENTER</p></div></div><div class="mini">Verdict <b>'+esc((d.action||'WAIT').replace(/_/g,' '))+'</b><br/>Score <b>'+esc(d.final_score||0)+'/100</b><br/>Mode <b>'+esc(((d.cloud_status||{}).execution_mode)||'paper')+'</b></div></aside><main class="main"><div class="top"><div class="title"><h2>Gold Trader Command Center</h2><p>Full-system IFVG analysis, market awareness, and execution safety.</p></div><div class="chips"><span class="chip">Symbol <b>'+esc(d.symbol||'XAUUSD')+'</b></span><span class="chip">Data <b>'+esc(((d.cloud_status||{}).data_provider)||'twelvedata')+'</b></span><span class="chip lock">Orders <b>'+esc(d.live_orders_enabled?'open':'locked')+'</b></span><button class="chip ok" id="retry">Refresh</button></div></div><section class="card hero"><div class="heroRow"><div><div class="label">Live Verdict</div><div class="verdict">'+esc((d.action||'WAIT').replace(/_/g,' '))+'</div><div class="meta">Grade '+esc(d.final_grade||'--')+' · '+esc((d.side||'none').toUpperCase())+'</div><div class="brief">'+esc(d.next_update||error||'Waiting for a fresh full-system scan.')+'</div></div><div class="scoreRing" style="--score:'+Math.max(0,Math.min(100,Number(d.final_score||0)))+'"><div class="scoreInner"><div><strong>'+esc(d.final_score||0)+'</strong><br/><span>/100</span></div></div></div></div></section><section class="panel"><h4>Provider Health</h4><div class="body"><pre class="json">'+esc(JSON.stringify(h,null,2))+'</pre></div></section><section class="panel" style="margin-top:14px"><h4>Chart Feed</h4><div class="body">'+esc(c.count||0)+' candles · '+esc(c.provider||c.source||'none')+(c.error?'<br/><span class="danger">'+esc(c.error)+'</span>':'')+'</div></section></main></div>';
    var retry=document.getElementById('retry'); if(retry) retry.onclick=load;
  }
  function load(){
    Promise.allSettled([get('/api/decision'),get('/api/provider-health'),get('/api/candles?tf=M15')]).then(function(r){
      render(r[0].value,r[1].value,r[2].value,(r.find(function(x){return x.status==='rejected';})||{}).reason);
    });
  }
  load();
})();""".encode("utf-8")


def _command_center_js_bytes() -> tuple[bytes, bool]:
    for path in _command_center_js_candidates():
        try:
            if path.is_file():
                return path.read_bytes(), False
        except OSError:
            continue
    return _fallback_command_center_js(), True

INDEX = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gold Trader Command Center</title>
<style>
:root{--bg:#06090d;--panel:#0d141e;--panel2:#101b28;--line:#1c2b3b;--text:#eef3fb;--muted:#92a0b2;--gold:#f7c948;--red:#ff5770;--green:#19d18f;--amber:#f5b942;--blue:#69a7ff;--shadow:0 24px 80px rgba(0,0,0,.38)}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at top left,#16202d 0,#06090d 35%,#020409 100%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;letter-spacing:.01em}button{font:inherit} .app{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{background:#070b11;border-right:1px solid var(--line);padding:24px 16px;position:sticky;top:0;height:100vh}.brand{display:flex;align-items:center;gap:14px;margin-bottom:28px}.coin{width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,#ffe27a,#b98716);box-shadow:0 0 24px rgba(247,201,72,.25)}.brand h1{font-size:16px;margin:0;letter-spacing:.08em}.brand p{font-size:11px;margin:3px 0 0;color:var(--muted);letter-spacing:.24em}.nav a{display:flex;justify-content:space-between;align-items:center;padding:13px 14px;margin:6px 0;border-radius:12px;color:#cbd5e1;text-decoration:none;border:1px solid transparent}.nav a.active,.nav a:hover{background:rgba(247,201,72,.10);border-color:rgba(247,201,72,.28);color:#fff}.mini{position:absolute;left:16px;right:16px;bottom:22px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px;color:var(--muted);font-size:12px}.mini b{float:right;color:white}.main{padding:26px 30px}.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:18px}.title h2{font-size:28px;margin:0;letter-spacing:.16em}.title p{margin:8px 0 0;color:var(--muted)}.chips{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.chip{border:1px solid var(--line);background:#0b121b;border-radius:999px;padding:9px 13px;color:var(--muted);font-size:12px}.chip b{color:white}.chip.lock{background:rgba(255,87,112,.15);border-color:rgba(255,87,112,.45);color:#ffd2da}.chip.ok{background:rgba(25,209,143,.12);border-color:rgba(25,209,143,.35)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}.dot.ok{background:var(--green)}.dot.warning{background:var(--amber)}.dot.danger{background:var(--red)}.grid{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:18px}.card{background:linear-gradient(180deg,rgba(16,27,40,.96),rgba(9,15,23,.96));border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden}.card h3{margin:0;padding:16px 18px;border-bottom:1px solid var(--line);font-size:13px;letter-spacing:.23em;text-transform:uppercase}.hero{padding:24px 24px 20px;border-color:rgba(247,201,72,.28)}.heroRow{display:grid;grid-template-columns:1fr 160px;gap:18px}.label{color:var(--gold);font-size:12px;text-transform:uppercase;letter-spacing:.25em}.verdict{font-size:52px;line-height:.95;margin:12px 0 6px;font-weight:900;letter-spacing:.13em}.meta{color:var(--muted)}.brief{margin-top:14px;max-width:760px;border:1px solid var(--line);background:#0b121b;border-radius:12px;padding:12px 14px;color:#d8e0ea}.scoreRing{width:126px;height:126px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--gold) calc(var(--score)*1%),#1b2939 0);padding:10px}.scoreInner{width:104px;height:104px;border-radius:50%;background:#08101a;display:grid;place-items:center;text-align:center}.scoreInner strong{font-size:30px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}.stat{background:#0c1520;border:1px solid var(--line);border-radius:14px;padding:13px}.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.18em}.stat b{display:block;margin-top:7px;font-size:16px}.chartHead{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}.tfBtns button{background:#0b121b;border:1px solid var(--line);color:#b7c2d1;border-radius:9px;padding:8px 10px;margin-left:5px;cursor:pointer}.tfBtns button.active{background:var(--gold);color:#171100;border-color:var(--gold);font-weight:800}.chartMeta{font-size:12px;color:var(--muted);padding:12px 18px 0}canvas{width:100%;height:390px;display:block}.right{display:flex;flex-direction:column;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden}.panel h4{margin:0;padding:14px 16px;border-bottom:1px solid var(--line);font-size:12px;text-transform:uppercase;letter-spacing:.25em}.panel .body{padding:14px 16px;color:#cbd5e1}.panel ul{margin:0;padding-left:18px}.panel li{margin:8px 0}.control{border-color:rgba(255,87,112,.45);background:linear-gradient(180deg,rgba(70,20,32,.65),rgba(14,18,26,.95))}.scoreGrid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-top:18px}.scoreBox{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b121b}.scoreBox .name{color:var(--muted);font-size:11px;min-height:32px;text-transform:uppercase}.scoreBox .num{font-size:20px;font-weight:900}.scoreBox.bad{border-color:rgba(255,87,112,.4)}.scoreBox.ok{border-color:rgba(25,209,143,.35)}.tfGrid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;padding:14px 16px}.tfCard{border:1px solid var(--line);border-radius:14px;padding:12px;background:#0b121b}.tfCard.aligned{border-color:rgba(25,209,143,.45);box-shadow:inset 0 0 0 1px rgba(25,209,143,.08)}.tfCard h5{margin:0 0 8px;font-size:15px}.tfCard p{margin:4px 0;color:var(--muted);font-size:12px}.contextGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.ctx{background:#0c1520;border:1px solid var(--line);border-radius:13px;padding:12px}.ctx span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.18em}.ctx b{display:block;margin-top:7px}.danger{color:var(--red)}.okText{color:var(--green)}.amber{color:var(--amber)}.page{display:none}.page.active{display:block}.wide{grid-column:1/-1}.json{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#b9c8d8;background:#060a10;border:1px solid var(--line);border-radius:14px;padding:16px;overflow:auto;max-height:70vh}.boot-shell{grid-template-columns:1fr}@media(max-width:1200px){.app{grid-template-columns:1fr}.side{position:relative;height:auto}.mini{position:static;margin-top:20px}.grid{grid-template-columns:1fr}.stats,.scoreGrid,.tfGrid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body style="margin:0;background:#06090d;color:#eef3fb">
<div id="root">
  <div class="app boot-shell" style="min-height:100vh;display:grid;place-items:center">
    <div style="text-align:center">
      <h2 style="letter-spacing:.16em;margin:0 0 12px">Gold Trader</h2>
      <p style="color:#92a0b2;margin:0">Loading command center…</p>
    </div>
  </div>
</div>
<script src="/command-center.js" defer></script>
<noscript><p style="padding:24px;color:#ff5770">JavaScript is required for the command center.</p></noscript>
</body></html>'''


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, allow_nan=False, indent=2).encode("utf-8")


def _candles(tf: str, count: int | None = None) -> dict[str, Any]:
    from gold_trader.data.twelvedata import candles_for_chart

    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    count = count or int(os.getenv("GOLD_CHART_CANDLE_COUNT", "280"))
    try:
        return candles_for_chart(tf, symbol=symbol, count=count, repo=ROOT)
    except Exception as exc:
        return {"ok": False, "tf": tf.upper(), "provider": "twelvedata", "error": str(exc), "candles": [], "count": 0}


def _summary_payload() -> dict[str, Any]:
    decision = get_decision_for_api()
    return {
        "config": {
            "symbol": decision.get("symbol") or os.getenv("GOLD_SYMBOL", "XAUUSD"),
            "execution_mode": (decision.get("cloud_status") or {}).get("execution_mode", "paper"),
        },
        "states": [],
        "journal": {"count": 0, "rows": []},
        "decision": decision,
        "secrets": {
            "twelve_data_api_key_set": bool(os.getenv("TWELVE_DATA_API_KEY")),
            "openai_api_key_set": bool(os.getenv("OPENAI_API_KEY")),
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/decision":
            refresh = (query.get("refresh") or ["0"])[0].lower() in {"1", "true", "yes"}
            return self.json(get_decision_for_api(refresh=refresh))
        if path == "/api/provider-health" or path == "/api/health":
            data = read_json(PROVIDER_HEALTH_PATH, {})
            if not data:
                data = get_decision_for_api().get("provider_health_summary", {})
            return self.json({"ok": True, **data} if path == "/api/health" else data)
        if path == "/api/market-intelligence":
            return self.json(get_decision_for_api().get("market_intelligence_summary", {}))
        if path == "/api/summary":
            return self.json(_summary_payload())
        if path == "/api/candles":
            tf = (query.get("tf") or ["M15"])[0]
            count = int((query.get("count") or [os.getenv("GOLD_CHART_CANDLE_COUNT", "280")])[0])
            return self.json(_candles(tf, count))
        if path == "/api/live/candles":
            tf_raw = (query.get("timeframe") or query.get("tf") or ["15"])[0]
            tf = tf_raw.upper()
            if tf.isdigit():
                tf = {"1": "M1", "5": "M5", "15": "M15", "30": "M30", "60": "H1", "240": "H4", "1440": "D1"}.get(tf, f"M{tf}")
            count = int((query.get("count") or ["500"])[0])
            payload = _candles(tf, count)
            return self.json({
                "source": payload.get("provider") or payload.get("source") or "unknown",
                "symbol": payload.get("symbol") or os.getenv("GOLD_SYMBOL", "XAUUSD"),
                "timeframe": tf_raw,
                "online": payload.get("provider") == "twelvedata" and not payload.get("cache_note"),
                "bars": payload.get("candles", []),
                "count": payload.get("count", 0),
                "error": payload.get("error"),
                "note": payload.get("cache_note") or payload.get("fallback_note"),
            })
        if path in {"/command-center.js", "/static/app.js"}:
            body, fallback = _command_center_js_bytes()
            cache = "no-store" if fallback else "public, max-age=300"
            return self._send_bytes(body, "application/javascript; charset=utf-8", cache=cache)
        if path == "/favicon.ico":
            return self._send_bytes(b"", "image/x-icon", status=204)
        if path in {"/", "/index.html", "/trade", "/market", "/markets", "/signal", "/risk", "/journal", "/settings"}:
            return self._send_bytes(INDEX.encode("utf-8"), "text/html; charset=utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(_safe_json({"error": "not found"}))

    def json(self, payload: Any) -> None:
        self._send_bytes(_safe_json(payload), "application/json; charset=utf-8")


def serve(host: str = "0.0.0.0", port: int = 8770) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"gold-trader market intelligence UI: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8770")))

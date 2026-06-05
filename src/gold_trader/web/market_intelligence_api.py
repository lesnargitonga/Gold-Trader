from __future__ import annotations

import json
import math
import os
import urllib.parse
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gold_trader.core.market_intelligence_ux import (
    LIVE_CONTEXT_PATH,
    PROVIDER_HEALTH_PATH,
    get_decision_for_api,
    provider_health,
    read_json,
)

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))).resolve()
COMMAND_CENTER_JS = _PKG_ROOT / "frontend" / "market_intelligence" / "command_center.js"
CLOUD_STATE_DIR = ROOT / "data" / "cloud_state"
LATEST_CLOUD_STATE = CLOUD_STATE_DIR / "latest_cloud_state.json"
SYNC_TOKEN_HEADER = "X-Gold-Sync-Token"
try:
    CLOUD_STATE_MAX_AGE_SECONDS = max(1, int(os.getenv("GOLD_CLOUD_STATE_MAX_AGE_SECONDS", "300")))
except ValueError:
    CLOUD_STATE_MAX_AGE_SECONDS = 300

EMPTY_PERFORMANCE: dict[str, Any] = {
    "total_signals": 0,
    "open_signals": 0,
    "closed_signals": 0,
    "tp1_hits": 0,
    "tp2_hits": 0,
    "tp3_hits": 0,
    "sl_hits": 0,
    "tp1_hit_rate": 0.0,
    "tp2_hit_rate": 0.0,
    "tp3_hit_rate": 0.0,
    "sl_hit_rate": 0.0,
    "average_max_favorable_r": 0.0,
    "average_max_adverse_r": 0.0,
    "expectancy_r": 0.0,
}


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
    return json.dumps(_json_safe_value(obj), allow_nan=False, indent=2).encode("utf-8")


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe_value(v) for v in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = _json_safe_value(payload)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(safe, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            if isinstance(row, dict):
                fh.write(json.dumps(_json_safe_value(row), ensure_ascii=False, allow_nan=False) + "\n")
    tmp.replace(path)


def _read_jsonl(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-max(1, min(int(limit), 500)):]:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(_json_safe_value(payload))
    return list(reversed(rows))


def _rel_source(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _candles(tf: str, count: int | None = None) -> dict[str, Any]:
    from gold_trader.data.twelvedata import candles_for_chart

    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    count = count or int(os.getenv("GOLD_CHART_CANDLE_COUNT", "280"))
    try:
        return candles_for_chart(tf, symbol=symbol, count=count, repo=ROOT)
    except Exception as exc:
        return {"ok": False, "tf": tf.upper(), "provider": "twelvedata", "error": str(exc), "candles": [], "count": 0}


def _summary_payload() -> dict[str, Any]:
    decision = _decision_payload()
    health = decision.get("provider_health_summary") or {}
    journal = _journal_payload(10)
    return {
        "config": {
            "symbol": decision.get("symbol") or os.getenv("GOLD_SYMBOL", "XAUUSD"),
            "execution_mode": (decision.get("cloud_status") or {}).get("execution_mode", "paper"),
        },
        "states": [
            {"key": key, **value}
            for key, value in health.items()
            if isinstance(value, dict)
        ],
        "journal": journal,
        "decision": decision,
        "secrets": {
            "twelve_data_api_key_set": bool(os.getenv("TWELVE_DATA_API_KEY")),
            "openai_api_key_set": bool(os.getenv("OPENAI_API_KEY")),
        },
    }


def _normalize_provider_health_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            out[key] = value
            continue
        row = dict(value)
        state = row.get("state")
        if isinstance(state, dict):
            row["state"] = str(state.get("state") or "unknown")
            if not row.get("source") and state.get("source"):
                row["source"] = state["source"]
        out[key] = row
    return out


def _provider_health_for_decision(decision: dict[str, Any]) -> dict[str, Any]:
    context = decision.get("live_market_context")
    if not isinstance(context, dict):
        context = read_json(LIVE_CONTEXT_PATH, {})
    if not isinstance(context, dict):
        context = {}
    return _normalize_provider_health_payload(provider_health(decision, context))


def _market_summary_from_health(health: dict[str, Any]) -> dict[str, Any]:
    def state(key: str) -> str:
        row = health.get(key)
        if isinstance(row, dict):
            return str(row.get("state") or "unknown")
        return "unknown"

    return {
        "macro": state("fmp_macro"),
        "sentiment": state("finnhub_sentiment"),
        "spread": state("spread"),
        "volatility": state("volatility"),
        "cme": state("cme"),
        "options": state("options"),
        "cot": state("cot"),
        "cross_market": state("cross_market"),
        "chart": state("chart_fallback"),
    }


def _market_levels_payload(decision: dict[str, Any] | None = None) -> dict[str, Any]:
    path = ROOT / "config" / "market_levels.json"
    payload: dict[str, Any] = {
        "state": "missing",
        "source": "config/market_levels.json",
        "configured": False,
        "levels": [],
    }
    if not path.exists():
        return payload

    raw = read_json(path, {})
    rows = raw.get("levels") if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    current_price = None
    if isinstance(decision, dict):
        try:
            current_price = float(decision.get("current_price"))
        except (TypeError, ValueError):
            current_price = None

    levels: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                price = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                strength = float(row.get("strength") or 1.0)
            except (TypeError, ValueError):
                strength = 1.0
            item: dict[str, Any] = {
                "price": price,
                "kind": str(row.get("kind") or "level"),
                "label": str(row.get("label") or ""),
                "strength": strength,
            }
            if current_price is not None:
                item["distance_points"] = round(price - current_price, 3)
            levels.append(item)

    if current_price is not None:
        levels.sort(key=lambda item: abs(float(item.get("distance_points") or 0)))
    else:
        levels.sort(key=lambda item: float(item["price"]))
    try:
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()
    except OSError:
        updated_at = None
    return {
        "state": "manual_proxy" if levels else "empty",
        "source": "config/market_levels.json",
        "configured": True,
        "updated_at": updated_at,
        "description": raw.get("description", "") if isinstance(raw, dict) else "",
        "levels": levels,
    }


def _smart_money_payload(decision: dict[str, Any]) -> dict[str, Any]:
    audit = decision.get("alignment_audit") or decision.get("tf_alignment_audit") or {}
    reads = decision.get("timeframe_reads") or []
    side = str(decision.get("side") or audit.get("side") or "none").lower()
    rows: list[dict[str, Any]] = []
    active_ifvg = 0
    candles_loaded = 0
    aligned_count = int(audit.get("aligned_count") or 0)
    htf_aligned = int(audit.get("htf_aligned") or 0)
    entry_confirmed = bool(audit.get("entry_confirmed") or audit.get("entry_ifvg_confirmed"))
    displacement = bool(audit.get("entry_displacement") or audit.get("entry_displacement_confirmed"))
    liquidity = bool(audit.get("liquidity_confirmed"))

    for row in reads if isinstance(reads, list) else []:
        if not isinstance(row, dict):
            continue
        tf = str(row.get("timeframe") or "").upper()
        candles = int(float(row.get("candles") or 0))
        if candles > 0:
            candles_loaded += candles
        ifvg = str(row.get("ifvg_side") or "none").lower()
        has_ifvg = ifvg in {"buy", "sell", "long", "short"}
        if has_ifvg:
            active_ifvg += 1
        rows.append({
            "timeframe": tf,
            "candles": candles,
            "bias": row.get("bias") or "unknown",
            "ifvg_side": row.get("ifvg_side") or "none",
            "has_ifvg": has_ifvg,
            "displacement": bool(row.get("displacement")),
            "liquidity_sweep": bool(row.get("liquidity_sweep")),
            "score": int(float(row.get("score") or 0)),
            "warnings": row.get("warnings") or [],
        })

    blockers: list[str] = []
    if aligned_count < 5:
        blockers.append(f"{max(0, 5 - aligned_count)} more timeframe alignment votes needed")
    if htf_aligned < 2:
        blockers.append("higher-timeframe agreement missing")
    if not entry_confirmed:
        blockers.append("entry-timeframe IFVG retest missing")
    if not displacement:
        blockers.append("entry displacement missing")
    if not liquidity:
        blockers.append("liquidity sweep missing")

    state = "ready" if side in {"buy", "sell"} and not blockers else "waiting"
    if not rows or candles_loaded <= 0:
        state = "no_candles"
    elif active_ifvg <= 0:
        state = "no_active_ifvg"

    return {
        "state": state,
        "label": "Smart Money Engine",
        "side": side,
        "active_ifvg_reads": active_ifvg,
        "aligned_count": aligned_count,
        "required_aligned": 5,
        "htf_aligned": htf_aligned,
        "required_htf": 2,
        "entry_confirmed": entry_confirmed,
        "entry_displacement": displacement,
        "liquidity_confirmed": liquidity,
        "blockers": blockers,
        "timeframes": rows,
    }


def _data_readiness_payload(decision: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    labels = {
        "decision_state": "Decision state",
        "twelvedata": "Analysis candles",
        "chart_fallback": "Chart candles",
        "fmp_macro": "Macro calendar",
        "finnhub_sentiment": "Sentiment",
        "spread": "Spread",
        "volatility": "Volatility",
        "cot": "COT positioning",
        "cross_market": "DXY / yields / VIX",
        "ctrader": "cTrader",
        "cme": "CME futures/OI",
        "options": "Options / IV / skew",
        "orders": "Live orders",
    }
    for key, row in health.items():
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "unknown")
        severity = str(row.get("severity") or "warning")
        configured = row.get("configured")
        needs_action = severity in {"warning", "danger", "critical"} or configured is False
        if key in {"orders", "chart_fallback"}:
            needs_action = severity in {"danger", "critical"}
        message = row.get("message") or row.get("error") or row.get("warning") or ""
        action = ""
        required = row.get("required_env") or []
        if configured is False and required:
            action = "Configure " + ", ".join(str(x) for x in required)
        elif "429" in str(message):
            action = "Reduce polling or add a higher-limit market data provider"
        elif "403" in str(message):
            action = "Replace or upgrade the provider credential"
        elif needs_action:
            action = str(message or "Review feed state")
        items.append({
            "key": key,
            "label": row.get("label") or labels.get(key, key),
            "state": state,
            "severity": severity,
            "configured": configured,
            "needs_action": needs_action,
            "action": action,
            "source": row.get("source"),
            "message": message,
        })

    for missing in decision.get("missing_inputs") or []:
        if isinstance(missing, dict):
            items.append({
                "key": "missing_" + str(missing.get("key") or "input"),
                "label": missing.get("label") or "Missing input",
                "state": "missing",
                "severity": "warning",
                "configured": None,
                "needs_action": True,
                "action": missing.get("impact") or "",
                "source": "decision",
                "message": missing.get("impact") or "",
            })

    smart = _smart_money_payload(decision)
    if smart["state"] != "ready":
        items.append({
            "key": "smart_money_engine",
            "label": smart["label"],
            "state": smart["state"],
            "severity": "warning",
            "configured": True,
            "needs_action": True,
            "action": "; ".join(smart["blockers"][:3]),
            "source": "decision_timeframe_reads",
            "message": "; ".join(smart["blockers"]),
        })

    actionable = [item for item in items if item.get("needs_action")]
    critical = [
        item for item in actionable
        if item.get("severity") in {"danger", "critical"} or item.get("key") in {"fmp_macro", "spread", "smart_money_engine"}
    ]
    return {
        "state": "blocked" if critical else "degraded" if actionable else "ready",
        "ready": not critical,
        "actionable_count": len(actionable),
        "critical_count": len(critical),
        "items": items,
        "actions": actionable,
    }


def _parse_snapshot_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _cloud_bundle() -> dict[str, Any]:
    data = read_json(LATEST_CLOUD_STATE, {})
    return data if isinstance(data, dict) else {}


def _cloud_bundle_path() -> Path | None:
    return LATEST_CLOUD_STATE if LATEST_CLOUD_STATE.exists() else None


def _bundle_published_at(bundle: dict[str, Any]) -> datetime | None:
    for value in (
        bundle.get("published_at"),
        bundle.get("generated_at"),
        bundle.get("updated_at"),
        (bundle.get("decision") or {}).get("timestamp_utc") if isinstance(bundle.get("decision"), dict) else None,
    ):
        parsed = _parse_snapshot_time(value)
        if parsed:
            return parsed
    return None


def _cloud_sync_meta(bundle: dict[str, Any] | None = None, *, fallback_path: Path | None = None) -> dict[str, Any]:
    bundle = bundle if isinstance(bundle, dict) else {}
    path = _cloud_bundle_path() or fallback_path
    published = _bundle_published_at(bundle)
    if published is None and path and path.exists():
        try:
            published = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            published = None
    if published is None:
        return {
            "state": "missing",
            "fresh": False,
            "published_at": None,
            "age_seconds": None,
            "source": "cloud_state_missing",
            "message": "Cloud state missing - local engine has not synced yet",
            "max_age_seconds": CLOUD_STATE_MAX_AGE_SECONDS,
        }
    age = max(0, int((datetime.now(timezone.utc) - published).total_seconds()))
    stale = age > CLOUD_STATE_MAX_AGE_SECONDS
    return {
        "state": "stale" if stale else "fresh",
        "fresh": not stale,
        "published_at": published.replace(microsecond=0).isoformat(),
        "age_seconds": age,
        "source": bundle.get("source") or "cloud_state",
        "message": "Cloud state stale - local engine not syncing" if stale else "Cloud state fresh",
        "max_age_seconds": CLOUD_STATE_MAX_AGE_SECONDS,
    }


def _cloud_decision() -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    bundle = _cloud_bundle()
    decision = bundle.get("decision") if isinstance(bundle.get("decision"), dict) else None
    if decision:
        return dict(decision), _cloud_sync_meta(bundle), "latest_cloud_state.json"

    split = CLOUD_STATE_DIR / "ifvg_mtf_decision_state.json"
    if split.exists():
        raw = read_json(split, {})
        if isinstance(raw, dict) and raw:
            return dict(raw), _cloud_sync_meta({}, fallback_path=split), _rel_source(split)
    return None, _cloud_sync_meta({}), "missing"


def _paper_allowed_from_decision(decision: dict[str, Any]) -> bool:
    if decision.get("paper_allowed") is not None:
        return bool(decision.get("paper_allowed"))
    action = str(decision.get("action") or "").upper()
    blockers = decision.get("hard_blocks") or decision.get("blockers") or decision.get("readable_blockers") or []
    return "TRADE_READY" in action and not bool(blockers)


def _normalize_render_decision(decision: dict[str, Any], meta: dict[str, Any], *, synced: bool) -> dict[str, Any]:
    out = dict(decision)
    source = "local_authoritative_engine" if synced else "render_cloud_fallback"
    out["source"] = source
    out["render_dashboard_mode"] = True
    out["live_allowed"] = False
    out["live_orders_enabled"] = False
    out["paper_allowed"] = _paper_allowed_from_decision(out)
    out["cloud_sync"] = meta

    market_context = out.get("market_context") if isinstance(out.get("market_context"), dict) else {}
    market_context = dict(market_context)
    data_health = out.get("data_health") if isinstance(out.get("data_health"), dict) else {}
    data_health = dict(data_health)
    if synced:
        if market_context.get("spread_source") == "live_tick":
            data_health["spread"] = "live_tick"
        elif data_health.get("spread") == "live_tick":
            market_context["spread_source"] = "live_tick"
    else:
        if market_context.get("spread_source") == "live_tick":
            market_context["spread_source"] = "render_cloud_fallback"
        if data_health.get("spread") == "live_tick":
            data_health["spread"] = "render_cloud_fallback"
    out["market_context"] = market_context
    out["data_health"] = data_health

    reads = out.get("timeframe_reads") if isinstance(out.get("timeframe_reads"), list) else []
    candle_counts = []
    for row in reads:
        if not isinstance(row, dict):
            continue
        try:
            candle_counts.append(int(float(row.get("candles") or 0)))
        except (TypeError, ValueError):
            pass
    current_cloud_status = out.get("cloud_status") if isinstance(out.get("cloud_status"), dict) else {}
    cloud_status = dict(current_cloud_status)
    cloud_status.update({
        "analysis": "online" if any(c > 0 for c in candle_counts) else cloud_status.get("analysis", "waiting_for_data"),
        "source": source,
        "data_provider": "local_authoritative_engine" if synced else cloud_status.get("data_provider", "render_cloud_fallback"),
        "broker": "MT5 bridge local" if synced else cloud_status.get("broker", "not_synced"),
        "orders": "locked",
        "execution_mode": "paper",
        "live_orders": "locked",
        "cloud_sync": meta.get("state"),
        "cloud_sync_age_seconds": meta.get("age_seconds"),
        "candles_loaded": cloud_status.get("candles_loaded", sum(candle_counts)),
        "spread": market_context.get("spread_points") or cloud_status.get("spread"),
        "spread_source": market_context.get("spread_source") or data_health.get("spread"),
    })
    out["cloud_status"] = cloud_status
    return out


def _cloud_provider_health() -> tuple[dict[str, Any] | None, str]:
    bundle = _cloud_bundle()
    raw = bundle.get("provider_health") if isinstance(bundle.get("provider_health"), dict) else None
    if raw:
        return _normalize_provider_health_payload(raw), "latest_cloud_state.json"
    split = CLOUD_STATE_DIR / "provider_health.json"
    if split.exists():
        raw = read_json(split, {})
        if isinstance(raw, dict) and raw:
            return _normalize_provider_health_payload(raw), _rel_source(split)
    return None, "missing"


def _provider_health_payload(decision: dict[str, Any] | None = None) -> dict[str, Any]:
    cloud, _source = _cloud_provider_health()
    if cloud is not None:
        return cloud
    try:
        data = _provider_health_for_decision(decision or get_decision_for_api())
    except Exception:
        data = read_json(PROVIDER_HEALTH_PATH, {})
        if not data:
            try:
                data = get_decision_for_api().get("provider_health_summary", {})
            except Exception:
                data = {}
    return _normalize_provider_health_payload(data)


def _performance_payload() -> dict[str, Any]:
    bundle = _cloud_bundle()
    raw = bundle.get("performance") if isinstance(bundle.get("performance"), dict) else None
    source = "latest_cloud_state.json"
    if raw is None:
        split = CLOUD_STATE_DIR / "paper_performance_report.json"
        if split.exists():
            raw = read_json(split, {})
            source = _rel_source(split)
    if raw is None:
        local = ROOT / "logs" / "paper_performance_report.json"
        if local.exists():
            raw = read_json(local, {})
            source = _rel_source(local)
    if not isinstance(raw, dict) or not raw:
        raw = dict(EMPTY_PERFORMANCE)
        source = "empty"
    out = dict(raw)
    out.setdefault("source", source)
    return out


def _rows_payload(
    bundle_key: str,
    split_filename: str,
    local_filename: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    bundle = _cloud_bundle()
    rows = bundle.get(bundle_key) if isinstance(bundle.get(bundle_key), list) else None
    source = "latest_cloud_state.json"
    if rows is None:
        split = CLOUD_STATE_DIR / split_filename
        if split.exists():
            rows = _read_jsonl(split, limit=limit)
            source = _rel_source(split)
    if rows is None:
        local = ROOT / "logs" / local_filename
        if local.exists():
            rows = _read_jsonl(local, limit=limit)
            source = _rel_source(local)
    if rows is None:
        rows = []
        source = "empty"
    clean_rows = [row for row in rows if isinstance(row, dict)][:max(1, min(int(limit), 500))]
    return {"count": len(clean_rows), "rows": clean_rows, "items": clean_rows, "source": source}


def _normalize_journal_row(payload: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "timestamp_utc": payload.get("timestamp_utc") or payload.get("ts") or payload.get("time"),
        "action": payload.get("action") or payload.get("verdict") or "unknown",
        "score": payload.get("final_score") if payload.get("final_score") is not None else payload.get("score"),
        "grade": payload.get("final_grade") or payload.get("grade"),
        "side": payload.get("side") or "none",
        "price": payload.get("current_price"),
        "source": source,
        "blockers": (payload.get("readable_blockers") or payload.get("hard_blocks") or payload.get("blockers") or [])[:5],
    }


def _legacy_journal_payload(limit: int = 20) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(payload: dict[str, Any], source: str) -> None:
        stamp = payload.get("timestamp_utc") or payload.get("ts") or payload.get("time")
        key = str(stamp or "") + "|" + str(payload.get("action") or "") + "|" + str(payload.get("final_score") or "")
        if key in seen:
            return
        seen.add(key)
        rows.append(_normalize_journal_row(payload, source))

    jsonl = ROOT / "data" / "journal" / "decision_snapshots.jsonl"
    if jsonl.exists():
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines()[-max(limit * 3, 30):]:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    add(payload, "data/journal/decision_snapshots.jsonl")
        except OSError:
            pass

    snap_dir = ROOT / "logs" / "decision_snapshots"
    if snap_dir.exists():
        try:
            files = sorted(snap_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(limit * 3, 30)]
        except OSError:
            files = []
        for path in files:
            payload = read_json(path, {})
            if isinstance(payload, dict) and payload:
                try:
                    source = str(path.relative_to(ROOT))
                except ValueError:
                    source = str(path)
                add(payload, source)

    rows.sort(key=lambda row: _parse_snapshot_time(row.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    rows = rows[:max(1, min(int(limit), 100))]
    return {"count": len(rows), "rows": rows, "items": rows, "source": "legacy_snapshots" if rows else "empty"}


def _journal_payload(limit: int = 20) -> dict[str, Any]:
    payload = _rows_payload(
        "decision_journal",
        "decision_journal.jsonl",
        "decision_journal.jsonl",
        limit=limit,
    )
    if payload["rows"] or payload["source"] != "empty":
        source = str(payload.get("source") or "cloud_state")
        rows = [_normalize_journal_row(row, source) for row in payload["rows"]]
        return {"count": len(rows), "rows": rows, "items": rows, "source": source}
    return _legacy_journal_payload(limit)


def _paper_signals_payload(limit: int = 20) -> dict[str, Any]:
    return _rows_payload(
        "paper_signals",
        "paper_signal_outcomes.jsonl",
        "paper_signal_outcomes.jsonl",
        limit=limit,
    )


def _enrich_decision(decision: dict[str, Any], health: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        decision = dict(decision)
        health = health if isinstance(health, dict) else _provider_health_payload(decision)
        decision["provider_health_summary"] = health
        decision["market_intelligence_summary"] = _market_summary_from_health(health)
        decision["market_levels_summary"] = _market_levels_payload(decision)
        decision["smart_money_summary"] = _smart_money_payload(decision)
        decision["data_readiness_summary"] = _data_readiness_payload(decision, health)
    except Exception:
        pass
    return decision


def _decision_payload(*, refresh: bool = False) -> dict[str, Any]:
    cloud_decision, meta, _source = _cloud_decision()
    if cloud_decision:
        decision = _normalize_render_decision(cloud_decision, meta, synced=True)
        return _enrich_decision(decision, _provider_health_payload(decision))

    decision = get_decision_for_api(refresh=refresh)
    if not isinstance(decision, dict):
        return {}
    decision = _normalize_render_decision(decision, meta, synced=False)
    return _enrich_decision(decision)


class Handler(SimpleHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes: int = 5_000_000) -> tuple[dict[str, Any] | None, str | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "invalid content length"
        if length <= 0:
            return None, "empty request body"
        if length > max_bytes:
            return None, "request body too large"
        try:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            payload = json.loads(body)
        except Exception as exc:
            return None, f"invalid JSON: {exc}"
        if not isinstance(payload, dict):
            return None, "payload must be a JSON object"
        return payload, None

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path != "/api/ingest-state":
            return self.json({"error": "not found"}, status=404)

        expected = os.getenv("GOLD_CLOUD_SYNC_TOKEN", "").strip()
        supplied = (self.headers.get(SYNC_TOKEN_HEADER) or "").strip()
        if not expected or supplied != expected:
            return self.json({"ok": False, "error": "forbidden"}, status=403)

        payload, error = self._read_json_body()
        if error or payload is None:
            return self.json({"ok": False, "error": error or "invalid payload"}, status=400)

        payload = _json_safe_value(payload)
        payload.setdefault("published_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        payload.setdefault("source", "local_authoritative_engine")
        payload.setdefault("mode", "paper")
        payload.setdefault("live_orders", "locked")

        try:
            _write_json(LATEST_CLOUD_STATE, payload)
            if isinstance(payload.get("decision"), dict):
                _write_json(CLOUD_STATE_DIR / "ifvg_mtf_decision_state.json", payload["decision"])
            if isinstance(payload.get("performance"), dict):
                _write_json(CLOUD_STATE_DIR / "paper_performance_report.json", payload["performance"])
            if isinstance(payload.get("provider_health"), dict):
                _write_json(CLOUD_STATE_DIR / "provider_health.json", payload["provider_health"])
            if isinstance(payload.get("paper_signals"), list):
                _write_jsonl(CLOUD_STATE_DIR / "paper_signal_outcomes.jsonl", payload["paper_signals"])
            if isinstance(payload.get("decision_journal"), list):
                _write_jsonl(CLOUD_STATE_DIR / "decision_journal.jsonl", payload["decision_journal"])
        except Exception as exc:
            return self.json({"ok": False, "error": f"failed to persist cloud state: {exc}"}, status=500)

        return self.json({
            "ok": True,
            "published_at": payload.get("published_at"),
            "source": payload.get("source"),
            "items": {
                "decision": isinstance(payload.get("decision"), dict),
                "performance": isinstance(payload.get("performance"), dict),
                "provider_health": isinstance(payload.get("provider_health"), dict),
                "paper_signals": len(payload.get("paper_signals") or []) if isinstance(payload.get("paper_signals"), list) else 0,
                "decision_journal": len(payload.get("decision_journal") or []) if isinstance(payload.get("decision_journal"), list) else 0,
            },
        })

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/decision":
            refresh = (query.get("refresh") or ["0"])[0].lower() in {"1", "true", "yes"}
            return self.json(_decision_payload(refresh=refresh))
        if path == "/api/provider-health" or path == "/api/health":
            data = _provider_health_payload(_decision_payload())
            return self.json({"ok": True, **data} if path == "/api/health" else data)
        if path == "/api/performance":
            return self.json(_performance_payload())
        if path == "/api/paper-signals" or path == "/api/paper_signals":
            limit = int((query.get("limit") or ["20"])[0])
            return self.json(_paper_signals_payload(limit))
        if path == "/api/decision-journal" or path == "/api/decision_journal":
            limit = int((query.get("limit") or ["20"])[0])
            return self.json(_journal_payload(limit))
        if path == "/api/market-intelligence":
            return self.json(_decision_payload().get("market_intelligence_summary", {}))
        if path == "/api/market-levels":
            return self.json(_market_levels_payload(_decision_payload()))
        if path == "/api/smart-money":
            return self.json(_decision_payload().get("smart_money_summary", {}))
        if path == "/api/data-readiness":
            return self.json(_decision_payload().get("data_readiness_summary", {}))
        if path == "/api/journal":
            limit = int((query.get("limit") or ["20"])[0])
            return self.json(_journal_payload(limit))
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

    def json(self, payload: Any, *, status: int = 200) -> None:
        self._send_bytes(_safe_json(payload), "application/json; charset=utf-8", status=status)


def serve(host: str = "0.0.0.0", port: int = 8770) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"gold-trader market intelligence UI: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8770")))

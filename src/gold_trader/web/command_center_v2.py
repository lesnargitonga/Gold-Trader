from __future__ import annotations

import json
import math
import mimetypes
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))
).resolve()
FRONTEND_DIR = ROOT / "frontend" / "command_center_v2"

TFS = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]

_SPA_PATHS = {
    "/",
    "/index.html",
    "/trade",
    "/market",
    "/markets",
    "/signals",
    "/risk",
    "/journal",
    "/settings",
}


def _base_candidates() -> list[Path]:
    bases: list[Path] = []
    for raw in (
        os.getenv("GOLD_TRADER_ROOT"),
        os.getenv("GOLD_RUNTIME_ROOT"),
        str(_PKG_ROOT),
        str(Path.cwd()),
        os.getenv("RENDER_PROJECT_DIR"),
        "/opt/render/project/src",
    ):
        if not raw:
            continue
        try:
            base = Path(raw).resolve()
        except Exception:
            continue
        if base not in bases:
            bases.append(base)
    return bases


def _candidate_paths(*parts: str) -> list[Path]:
    paths: list[Path] = []
    for base in _base_candidates():
        candidate = base.joinpath(*parts)
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _decision_paths() -> list[Path]:
    paths: list[Path] = []
    for base in _base_candidates():
        for rel in (
            ("logs", "ifvg_mtf_decision_state.json"),
            ("logs", "decision_state.json"),
            ("data", "ifvg_mtf_decision_state.json"),
            ("data", "state.json"),
            ("ifvg_mtf_decision_state.json"),
        ):
            candidate = base.joinpath(*rel)
            if candidate not in paths:
                paths.append(candidate)
    return paths


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _json_safe(data)  # type: ignore[return-value]
        return {}
    except Exception:
        return {}


def _latest_decision_path() -> Path | None:
    existing = [p for p in _decision_paths() if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def _load_live_context() -> dict:
    merged: dict = {}
    for rel in (
        ("logs", "live_market_context.json"),
        ("logs", "live_context.json"),
        ("data", "live_market_context.json"),
    ):
        for path in _candidate_paths(*rel):
            if path.exists():
                data = _read_json(path)
                if data:
                    merged.update(data)
    return merged


def _default_decision() -> dict:
    return {
        "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "symbol": os.getenv("GOLD_SYMBOL", "XAU/USD"),
        "action": "WAIT",
        "side": "none",
        "final_grade": "—",
        "final_score": 0,
        "current_price": None,
        "reasons": ["Waiting for the first full-system scan."],
        "blockers": [],
        "timeframe_reads": [],
        "daily_guard": {},
        "market_context": {},
        "next_update": "Waiting for a fresh full-system scan.",
        "cloud_status": {
            "analysis": "starting",
            "data_provider": os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata"),
            "orders": "locked",
        },
        "_source_path": None,
        "_source_age_seconds": None,
    }


def _tf_reads(decision: dict[str, Any]) -> list[dict[str, Any]]:
    reads = decision.get("timeframe_reads") or []
    by_tf = {str(r.get("timeframe", "")).upper(): r for r in reads if isinstance(r, dict)}
    return [
        dict(by_tf.get(tf, {"timeframe": tf, "candles": 0, "bias": "unknown", "ifvg_side": "none", "score": 0}))
        for tf in TFS
    ]


_HARDENED_SCORE_LABELS = {
    "timeframe_alignment": "Timeframe Alignment",
    "ifvg_geometry": "IFVG Geometry",
    "macro_regime": "Macro Regime",
    "sentiment_gate": "Sentiment Gate",
    "session_spread": "Session / Spread",
    "volatility": "Volatility",
}
_HARDENED_SCORE_MAX = {
    "timeframe_alignment": 25,
    "ifvg_geometry": 20,
    "macro_regime": 20,
    "sentiment_gate": 15,
    "session_spread": 10,
    "volatility": 10,
}


def _score_decomposition_from_hardened(decision: dict[str, Any]) -> dict[str, Any]:
    flat = decision.get("score_decomposition")
    if not isinstance(flat, dict) or "components" in flat:
        return flat if isinstance(flat, dict) else {}
    missing = set(decision.get("missing_inputs") or [])
    maxes = decision.get("score_component_max") if isinstance(decision.get("score_component_max"), dict) else _HARDENED_SCORE_MAX
    components = []
    for key, label in _HARDENED_SCORE_LABELS.items():
        max_v = int(maxes.get(key, _HARDENED_SCORE_MAX[key]))
        score = int(flat.get(key, 0))
        components.append(
            {
                "key": key,
                "label": label,
                "score": score,
                "max": max_v,
                "missing": any(key in str(m).lower() or key.replace("_", " ") in str(m).lower() for m in missing),
            }
        )
    return {
        "components": components,
        "computed_score": int(decision.get("raw_score_before_penalty") or sum(c["score"] for c in components)),
        "final_score": int(decision.get("final_score") or 0),
        "data_quality_penalty": int(decision.get("data_quality_penalty") or 0),
    }


def _score_decomposition(decision: dict[str, Any]) -> dict[str, Any]:
    reads = _tf_reads(decision)
    side = str(decision.get("side") or "none").lower()
    mc = decision.get("market_context") or {}
    ctx = decision.get("live_market_context") or {}
    final = max(0, min(100, int(float(decision.get("final_score") or 0))))

    aligned = 0
    active_ifvg = 0
    entry_ok = False
    for r in reads:
        rside = str(r.get("ifvg_side") or "none").lower()
        if rside != "none":
            active_ifvg += 1
        if side != "none" and rside == side:
            aligned += 1
    alignment = min(25, round((aligned / 7) * 25)) if side != "none" else 0
    geometry = 0
    if active_ifvg:
        geometry += min(12, active_ifvg * 2)
    for r in reads:
        if str(r.get("timeframe", "")).upper() in {"M15", "M5", "M1"} and str(r.get("ifvg_side") or "none").lower() == side and side != "none":
            entry_ok = True
    if entry_ok:
        geometry += 8
    geometry = min(20, geometry)

    macro_state = str(mc.get("macro_state") or ctx.get("macro") or "unknown").lower()
    sentiment_state = str(mc.get("sentiment_state") or ctx.get("sentiment") or "unknown").lower()
    vol_state = str(mc.get("volatility_state") or ctx.get("volatility") or "unknown").lower()
    spread = mc.get("spread_points") or ctx.get("spread")
    session = str(mc.get("session") or "unknown").lower()

    macro = 0 if macro_state in {"unknown", "unavailable", "blocked"} else (20 if macro_state in {"clear", "normal", "ok"} else 10)
    sentiment = (
        0
        if sentiment_state in {"unknown", "unavailable"}
        else (15 if "bull" in sentiment_state or "bear" in sentiment_state or sentiment_state in {"neutral", "mixed"} else 8)
    )
    session_spread = 0
    if session in {"london", "new_york", "overlap"}:
        session_spread += 5
    if spread not in {None, "", "unknown", "unavailable"}:
        session_spread += 5
    volatility = 10 if vol_state == "normal" else (5 if vol_state in {"low", "high", "tradable"} else 0)

    comps = [
        {"key": "alignment", "label": "Timeframe Alignment", "score": alignment, "max": 25, "missing": False},
        {"key": "geometry", "label": "IFVG Geometry", "score": geometry, "max": 20, "missing": active_ifvg == 0},
        {"key": "macro", "label": "Macro Regime", "score": macro, "max": 20, "missing": macro_state in {"unknown", "unavailable"}},
        {"key": "sentiment", "label": "Sentiment Gate", "score": sentiment, "max": 15, "missing": sentiment_state in {"unknown", "unavailable"}},
        {"key": "session", "label": "Session / Spread", "score": session_spread, "max": 10, "missing": spread in {None, "", "unknown", "unavailable"}},
        {"key": "volatility", "label": "Volatility", "score": volatility, "max": 10, "missing": vol_state in {"unknown", "unavailable"}},
    ]
    computed = sum(int(c["score"]) for c in comps)
    penalty = sum(int(c["max"]) for c in comps if c.get("missing"))
    return {"components": comps, "computed_score": computed, "final_score": final, "data_quality_penalty": penalty}


def _watching_for(decision: dict[str, Any]) -> list[str]:
    explicit = decision.get("watching_for")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit]
    side = str(decision.get("side") or "none").lower()
    reads = _tf_reads(decision)
    aligned = sum(1 for r in reads if side != "none" and str(r.get("ifvg_side") or "none").lower() == side)
    items: list[str] = []
    if aligned < 5:
        items.append(f"{5 - aligned} more timeframe(s) to align with the trade side")
    if not any(
        str(r.get("timeframe", "")).upper() in {"M15", "M5", "M1"}
        and str(r.get("ifvg_side") or "none").lower() == side
        for r in reads
    ):
        items.append("Entry timeframe IFVG retest + confirmation candle")
    mc = decision.get("market_context") or {}
    if str(mc.get("macro_state") or "unknown").lower() == "unknown":
        items.append("Macro calendar clear/known before Grade-A execution")
    if str(mc.get("sentiment_state") or "unknown").lower() == "unknown":
        items.append("Non-conflicting sentiment state")
    if mc.get("spread_points") is None:
        items.append("Known spread or broker quote before live execution")
    return items or ["Maintain Grade-A alignment and wait for alert confirmation"]


def _normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    d = dict(decision)
    live_ctx = _load_live_context()
    if live_ctx:
        d["live_market_context"] = live_ctx

    action = str(d.get("action") or "WAIT").upper()
    if "TRADE_READY" in action:
        d["verdict_label"] = "PAPER TRADE READY"
    elif action in {"BUY", "SELL"}:
        d["verdict_label"] = action
    else:
        d["verdict_label"] = "WAIT"

    d["orders_locked"] = os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() != "true"
    d["execution_mode"] = os.getenv("GOLD_EXECUTION_MODE", "paper")
    d["data_provider"] = os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    reads = _tf_reads(d)
    d["timeframe_reads_ordered"] = reads
    d["timeframe_reads"] = reads
    d["candles_loaded"] = sum(int(r.get("candles") or 0) for r in reads)

    flat_score = d.get("score_decomposition")
    if isinstance(flat_score, dict) and any(k in flat_score for k in _HARDENED_SCORE_LABELS):
        d["score_decomposition"] = _score_decomposition_from_hardened(d)
    else:
        d["score_decomposition"] = _score_decomposition(d)

    if not d.get("watching_for"):
        d["watching_for"] = _watching_for(d)

    blockers = list(d.get("blockers") or [])
    for block in d.get("hard_blocks") or []:
        if block not in blockers:
            blockers.append(str(block))
    d["blockers"] = blockers

    if d.get("source_age_seconds") is not None and d.get("_source_age_seconds") is None:
        d["_source_age_seconds"] = d.get("source_age_seconds")

    mc = d.get("market_context") or {}
    data_issues: list[str] = list(d.get("data_issues") or [])
    for msg in mc.get("warnings") or []:
        data_issues.append(str(msg))
    for msg in mc.get("notes") or []:
        s = str(msg)
        if "unavailable" in s.lower() or "unknown" in s.lower():
            data_issues.append(s)
    for block in d.get("hard_blocks") or []:
        data_issues.append(str(block))
    for missing in d.get("missing_inputs") or []:
        data_issues.append(f"missing input: {missing}")
    penalty = int(d.get("data_quality_penalty") or d["score_decomposition"].get("data_quality_penalty") or 0)
    if penalty > 0 and not any("data quality penalty" in x for x in data_issues):
        data_issues.append(f"−{penalty} pts data quality penalty")
    d["data_issues"] = list(dict.fromkeys(data_issues))

    cloud = dict(d.get("cloud_status") or {})
    cloud.setdefault("data_provider", d["data_provider"])
    cloud.setdefault("execution_mode", d["execution_mode"])
    cloud.setdefault("orders", "locked" if d["orders_locked"] else "enabled")
    cloud.setdefault("candles_loaded", d["candles_loaded"])
    cloud.setdefault("analysis", "online" if d["candles_loaded"] > 0 else "waiting")
    d["cloud_status"] = cloud

    d["_meta"] = {
        "source": d.get("_source_path"),
        "source_age_seconds": d.get("_source_age_seconds"),
        "render": bool(os.getenv("RENDER")),
    }
    return _json_safe(d)  # type: ignore[return-value]


def load_decision() -> dict:
    base = _default_decision()
    path = _latest_decision_path()
    if path:
        base.update(_read_json(path))
        st = path.stat()
        try:
            base["_source_path"] = str(path.relative_to(ROOT))
        except ValueError:
            base["_source_path"] = str(path)
        base["_source_mtime"] = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        base["_source_age_seconds"] = max(0, int(time.time() - st.st_mtime))
    return _normalize_decision(base)


def candles_from_twelvedata(tf: str, count: int) -> dict[str, Any]:
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    tf = tf.upper()
    try:
        from gold_trader.data.twelvedata import fetch_candles

        rows = fetch_candles(tf, symbol=symbol, count=count)
        out: list[dict] = []
        for c in rows:
            if isinstance(c, dict):
                out.append(
                    {
                        "time": c.get("time") or c.get("datetime") or c.get("date") or "",
                        "open": float(c.get("open")),
                        "high": float(c.get("high")),
                        "low": float(c.get("low")),
                        "close": float(c.get("close")),
                        "volume": float(c.get("volume") or 0),
                    }
                )
            else:
                out.append(
                    {
                        "time": getattr(c, "time", ""),
                        "open": float(getattr(c, "open")),
                        "high": float(getattr(c, "high")),
                        "low": float(getattr(c, "low")),
                        "close": float(getattr(c, "close")),
                        "volume": float(getattr(c, "volume", 0) or 0),
                    }
                )
        return {"ok": True, "provider": "twelvedata", "timeframe": tf, "candles": out[-count:], "count": len(out[-count:])}
    except Exception as exc:
        return {"ok": False, "provider": "twelvedata", "timeframe": tf, "candles": [], "count": 0, "error": str(exc)}


def load_alerts(limit: int = 50) -> list[dict]:
    for path in _candidate_paths("logs", "operator_alerts.jsonl"):
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
            out: list[dict] = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({"message": line})
            return list(reversed(out))
        except Exception:
            return []
    return []


class Handler(BaseHTTPRequestHandler):
    server_version = "GoldTraderCommandCenter/2.0"

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, status: int = 200) -> None:
        payload = _json_safe(obj)
        self._send(status, json.dumps(payload, allow_nan=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _send_static(self, rel: str) -> None:
        path = (FRONTEND_DIR / rel).resolve()
        try:
            path.relative_to(FRONTEND_DIR.resolve())
        except Exception:
            self.send_error(403)
            return
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, content, ctype)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in _SPA_PATHS:
            self._send_static("index.html")
            return
        if path.startswith("/assets/"):
            self._send_static(path.removeprefix("/assets/"))
            return
        if path == "/health":
            d = load_decision()
            self._json(
                {
                    "ok": True,
                    "service": "command-center-v2",
                    "candles_loaded": d.get("candles_loaded"),
                    "source": d.get("_source_path"),
                    "age_seconds": d.get("_source_age_seconds"),
                }
            )
            return
        if path == "/api/decision":
            self._json(load_decision())
            return
        if path == "/api/alerts":
            self._json({"ok": True, "alerts": load_alerts()})
            return
        if path == "/api/candles":
            q = parse_qs(parsed.query)
            tf = (q.get("tf") or ["M15"])[0].upper()
            try:
                count = max(60, min(600, int((q.get("count") or ["280"])[0])))
            except Exception:
                count = 280
            if tf not in TFS:
                self._json({"ok": False, "error": f"unsupported timeframe {tf}", "timeframe": tf, "candles": []}, 400)
                return
            self._json(candles_from_twelvedata(tf, count))
            return

        self._send_static("index.html")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[cc-v2] {self.address_string()} {fmt % args}", flush=True)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("HOST", "0.0.0.0")
    port = port or int(os.getenv("PORT", "8770"))
    if not (FRONTEND_DIR / "app.js").exists():
        raise FileNotFoundError(f"Command center v2 frontend missing at {FRONTEND_DIR}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[cc-v2] serving on {host}:{port} from {FRONTEND_DIR}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

from __future__ import annotations

import json
import math
import mimetypes
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))
).resolve()
FRONTEND_DIR = ROOT / "frontend" / "react_command_center_v3"

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
            ("data", "ifvg_mtf_decision_state.json"),
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


def _default_decision() -> dict:
    return {
        "timestamp_utc": None,
        "symbol": os.getenv("GOLD_SYMBOL", "XAU/USD"),
        "action": "WAIT",
        "side": "none",
        "final_grade": "—",
        "final_score": 0,
        "current_price": None,
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "tp3": None,
        "rr_tp1": None,
        "rr_tp2": None,
        "timeframe_reads": [],
        "daily_guard": {"trades_taken": 0, "losses_taken": 0, "open_positions": 0, "blocked": False, "reasons": []},
        "market_context": {"volatility_state": "unknown", "macro_state": "unknown", "sentiment_state": "unknown", "spread_points": None, "warnings": [], "notes": []},
        "reasons": [],
        "blockers": ["Waiting for the next full-system scan."],
        "next_update": "Waiting for a fresh full-system scan.",
        "cloud_status": {},
    }


def _normalize_action(action: str, side: str) -> tuple[str, str]:
    raw = (action or "WAIT").upper()
    side = (side or "none").lower()
    if "TRADE_READY" in raw:
        verdict = "PAPER TRADE READY"
        tone = "danger" if side == "sell" else "success"
    elif raw in {"BUY", "LONG"} or ("BUY" in raw and side == "buy"):
        verdict = "BUY READY"
        tone = "success"
    elif raw in {"SELL", "SHORT"} or ("SELL" in raw and side == "sell"):
        verdict = "SELL READY"
        tone = "danger"
    elif "BLOCK" in raw:
        verdict = "BLOCKED"
        tone = "blocked"
    else:
        verdict = "WAIT"
        tone = "wait"
    return verdict, tone


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
    else:
        base["_source_path"] = None
        base["_source_mtime"] = None
        base["_source_age_seconds"] = None

    for ctx_path in _candidate_paths("logs", "live_market_context.json"):
        if ctx_path.exists():
            live_ctx = _read_json(ctx_path)
            if live_ctx:
                base["live_market_context"] = live_ctx
            break

    reads = base.get("timeframe_reads") or []
    by_tf = {str(r.get("timeframe", "")).upper(): r for r in reads if isinstance(r, dict)}
    normalized_reads = []
    for tf in TFS:
        r = dict(by_tf.get(tf, {}))
        r.setdefault("timeframe", tf)
        r.setdefault("candles", 0)
        r.setdefault("bias", "unknown")
        r.setdefault("ifvg_side", "none")
        r.setdefault("score", 0)
        r.setdefault("warnings", [])
        r.setdefault("reasons", [])
        normalized_reads.append(r)
    base["timeframe_reads"] = normalized_reads

    verdict, tone = _normalize_action(str(base.get("action", "WAIT")), str(base.get("side", "none")))
    base["verdict"] = verdict
    base["tone"] = tone
    base["active_ifvg_reads"] = sum(1 for r in normalized_reads if (r.get("ifvg_side") or "none") != "none")
    base["candles_loaded"] = sum(int(r.get("candles") or 0) for r in normalized_reads)

    cloud = dict(base.get("cloud_status") or {})
    cloud.setdefault("data_provider", os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata"))
    cloud.setdefault("execution_mode", os.getenv("GOLD_EXECUTION_MODE", "paper"))
    cloud.setdefault("orders", "locked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() != "true" else "enabled")
    cloud.setdefault("candles_loaded", base["candles_loaded"])
    cloud.setdefault("analysis", "online" if base["candles_loaded"] > 0 else "waiting")
    base["cloud_status"] = cloud

    base["_meta"] = {
        "source": base.get("_source_path"),
        "source_age_seconds": base.get("_source_age_seconds"),
        "render": bool(os.getenv("RENDER")),
    }
    return _json_safe(base)  # type: ignore[return-value]


def candles_from_twelvedata(tf: str, count: int) -> tuple[list[dict], str]:
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
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
        return out, "twelvedata"
    except Exception as exc:
        return [], f"twelvedata error: {exc!r}"


def load_alerts(limit: int = 40) -> list[dict]:
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
    server_version = "GoldTraderReactCommandCenter/3.0"

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
                    "service": "react-command-center-v3",
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
                count = max(60, min(500, int((q.get("count") or ["260"])[0])))
            except Exception:
                count = 260
            if tf not in TFS:
                self._json({"ok": False, "error": f"unsupported timeframe {tf}", "tf": tf, "candles": []}, 400)
                return
            candles, provider = candles_from_twelvedata(tf, count)
            payload: dict = {
                "ok": bool(candles),
                "tf": tf,
                "provider": provider,
                "count": len(candles),
                "candles": candles[-count:],
            }
            if not candles and "error" in provider:
                payload["error"] = provider
            self._json(payload)
            return

        self._send_static("index.html")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[react-cc-v3] {self.address_string()} {fmt % args}", flush=True)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("HOST", "0.0.0.0")
    port = port or int(os.getenv("PORT", "8770"))
    if not FRONTEND_DIR.exists():
        raise FileNotFoundError(f"React v3 frontend not found at {FRONTEND_DIR}")
    if not (FRONTEND_DIR / "app.js").exists():
        raise FileNotFoundError(
            f"Missing compiled app.js at {FRONTEND_DIR / 'app.js'} — run scripts/compile_react_command_center.sh"
        )
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[react-cc-v3] serving on {host}:{port} from {FRONTEND_DIR}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

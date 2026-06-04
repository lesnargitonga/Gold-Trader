from __future__ import annotations

import json
import mimetypes
import os
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))
).resolve()
FRONTEND_DIR = ROOT / "frontend" / "react_command_center"

TIMEFRAMES = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]


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


def _read_json_file(paths: list[Path]) -> tuple[dict, str | None, float | None]:
    for path in paths:
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {"value": data}, str(path), path.stat().st_mtime
        except Exception as exc:
            return {"error": f"failed to read {path}: {exc}"}, str(path), None
    return {}, None, None


def _decision_default() -> dict:
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
        "next_update": "Waiting for the next scan.",
        "cloud_status": {
            "analysis": "warming_up",
            "data_provider": os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata"),
            "orders": "locked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() != "true" else "enabled",
            "execution_mode": os.getenv("GOLD_EXECUTION_MODE", "paper"),
        },
    }


def _normalise_decision(data: dict, source: str | None, mtime: float | None) -> dict:
    out = _decision_default()
    out.update(data or {})

    live_ctx, live_source, live_mtime = _read_json_file(_candidate_paths("logs", "live_market_context.json"))
    if live_ctx:
        out["live_market_context"] = live_ctx

    now = time.time()
    out["_meta"] = {
        "source": source,
        "source_mtime": mtime,
        "source_age_seconds": round(now - mtime, 1) if mtime else None,
        "live_context_source": live_source,
        "live_context_age_seconds": round(now - live_mtime, 1) if live_mtime else None,
        "server_time": now,
        "render": bool(os.getenv("RENDER")),
    }

    candles_loaded = sum(int(tf.get("candles") or 0) for tf in out.get("timeframe_reads", []) if isinstance(tf, dict))
    cloud = out.setdefault("cloud_status", {})
    cloud.setdefault("analysis", "online" if candles_loaded else "warming_up")
    cloud.setdefault("data_provider", os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata"))
    cloud.setdefault("candles_loaded", candles_loaded)
    cloud.setdefault("orders", "locked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() != "true" else "enabled")
    cloud.setdefault("execution_mode", os.getenv("GOLD_EXECUTION_MODE", "paper"))

    return out


def load_decision() -> dict:
    paths = _candidate_paths("logs", "ifvg_mtf_decision_state.json")
    data, source, mtime = _read_json_file(paths)
    return _normalise_decision(data, source, mtime)


def load_alerts(limit: int = 40) -> list[dict]:
    alerts: list[dict] = []
    for path in _candidate_paths("logs", "operator_alerts.jsonl"):
        try:
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
            for line in lines:
                try:
                    alerts.append(json.loads(line))
                except Exception:
                    alerts.append({"message": line})
            break
        except Exception as exc:
            alerts.append({"level": "warning", "message": f"Could not read alerts: {exc}"})
            break
    return alerts[-limit:]


def _fetch_twelve_candles(tf: str, count: int) -> dict:
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    try:
        from gold_trader.data.twelvedata import fetch_candles

        candles = fetch_candles(tf, symbol=symbol, count=count)
        return {
            "ok": True,
            "source": "twelvedata",
            "timeframe": tf,
            "candles": candles,
            "count": len(candles),
        }
    except Exception as exc:
        return {"ok": False, "source": "twelvedata", "timeframe": tf, "candles": [], "count": 0, "error": str(exc)}


def load_candles(tf: str, count: int = 280) -> dict:
    tf = tf.upper()
    if tf not in TIMEFRAMES:
        return {"ok": False, "error": f"Unsupported timeframe {tf}", "timeframe": tf, "candles": []}

    provider = os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata").lower()
    if provider == "twelvedata" or os.getenv("TWELVE_DATA_API_KEY"):
        payload = _fetch_twelve_candles(tf, count)
        if payload.get("candles"):
            return payload
        if payload.get("error"):
            return payload

    return {
        "ok": False,
        "source": provider or "unknown",
        "timeframe": tf,
        "candles": [],
        "count": 0,
        "error": "No candle provider returned chart data. Check TWELVE_DATA_API_KEY and provider logs.",
    }


class CommandCenterHandler(SimpleHTTPRequestHandler):
    server_version = "GoldTraderReactCommandCenter/2.0"

    def _json(self, payload: object, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self._json({"ok": True, "service": "react-command-center", "time": time.time()})
            return
        if path == "/api/decision":
            self._json(load_decision())
            return
        if path == "/api/alerts":
            self._json({"ok": True, "alerts": load_alerts()})
            return
        if path == "/api/candles":
            tf = (qs.get("tf") or ["M15"])[0]
            try:
                count = max(60, min(500, int((qs.get("count") or ["280"])[0])))
            except Exception:
                count = 280
            self._json(load_candles(tf, count))
            return
        if path in {"/", "/trade", "/market", "/markets", "/signals", "/risk", "/journal", "/settings"}:
            self._send_static("index.html")
            return
        if path.startswith("/assets/"):
            self._send_static(path.removeprefix("/assets/"))
            return
        self._send_static("index.html")

    def log_message(self, fmt: str, *args: object) -> None:
        print("[react-command-center] " + fmt % args, flush=True)


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("HOST", "0.0.0.0")
    port = port or int(os.getenv("PORT", "8770"))
    if not FRONTEND_DIR.exists():
        raise FileNotFoundError(f"React frontend not found at {FRONTEND_DIR}")
    httpd = ThreadingHTTPServer((host, port), CommandCenterHandler)
    print(f"[react-command-center] serving on {host}:{port} from {FRONTEND_DIR}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(Path(__file__).resolve().parents[3])))
).resolve()
FRONTEND_DIR = ROOT / "frontend"
LOGS_DIR = ROOT / "logs"
LIVE_CONTEXT_PATH = LOGS_DIR / "live_market_context.json"
DECISION_PATH = LOGS_DIR / "ifvg_mtf_decision_state.json"
ALERTS_PATH = LOGS_DIR / "operator_alerts.jsonl"

TIMEFRAMES = {"D1", "H4", "H1", "M30", "M15", "M5", "M1"}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(type(obj).__name__)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_decision() -> dict[str, Any]:
    data = read_json(DECISION_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("timestamp_utc", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    data.setdefault("symbol", os.getenv("GOLD_SYMBOL", "XAU/USD"))
    data.setdefault("action", "WAIT")
    data.setdefault("side", "none")
    data.setdefault("final_grade", "—")
    data.setdefault("final_score", 0)
    data.setdefault("timeframe_reads", [])
    data.setdefault("reasons", [])
    data.setdefault("blockers", [])
    data.setdefault("daily_guard", {})
    data.setdefault("market_context", {})
    data.setdefault("cloud_status", {})
    data.setdefault("next_update", "Waiting for the next full-system scan.")
    live_ctx = read_json(LIVE_CONTEXT_PATH, {})
    if live_ctx:
        data["live_market_context"] = live_ctx
    reads = data.get("timeframe_reads") or []
    candle_total = sum(int((r or {}).get("candles") or 0) for r in reads if isinstance(r, dict))
    data.setdefault("cloud_status", {
        "analysis": "online" if candle_total > 0 else "waiting_for_data",
        "data_provider": os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata"),
        "candles_loaded": candle_total,
        "orders": "unlocked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true" else "locked",
        "execution_mode": os.getenv("GOLD_EXECUTION_MODE", "paper"),
    })
    return data


def read_alerts(limit: int = 40) -> list[dict[str, Any]]:
    if not ALERTS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in ALERTS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                item = {"message": line}
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return []
    return rows[-limit:]


def fetch_candles(tf: str, count: int) -> dict[str, Any]:
    tf = tf.upper()
    if tf not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {tf}")
    count = max(30, min(count, 700))
    provider = os.getenv("GOLD_MARKET_DATA_PROVIDER", "twelvedata").strip() or "twelvedata"
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    source = "none"
    warnings: list[str] = []
    candles: list[dict[str, Any]] = []

    if provider.lower() in {"twelvedata", "twelve_data", "twelve"}:
        try:
            from gold_trader.data.twelvedata import fetch_candles as td_fetch

            raw = td_fetch(tf, symbol=symbol, count=count)
            for row in raw:
                try:
                    candles.append(
                        {
                            "time": str(row.get("time") or row.get("datetime") or ""),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("volume") or 0.0),
                        }
                    )
                except Exception:
                    continue
            source = "twelvedata"
        except Exception as exc:
            warnings.append(f"twelvedata unavailable: {exc}")

    if not candles:
        # Fallback to the latest decision price so the UI never looks broken.
        decision = read_decision()
        price = decision.get("current_price") or 0
        try:
            price = float(price)
        except Exception:
            price = 0.0
        if price:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            candles = [{"time": now, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}]
            source = "decision_fallback"
        else:
            source = "empty"

    return {
        "symbol": symbol,
        "timeframe": tf,
        "source": source,
        "count": len(candles),
        "warnings": warnings,
        "candles": candles,
        "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


class ReactCommandCenterHandler(BaseHTTPRequestHandler):
    server_version = "GoldTraderReact/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[react-ui] {self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store" if ctype.startswith("text/") else "public, max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self.send_json({"ok": True, "service": "gold-trader-react-command-center", "timestamp_utc": datetime.now(timezone.utc).isoformat()})
            return
        if path == "/api/decision":
            self.send_json(read_decision())
            return
        if path == "/api/alerts":
            limit = int((qs.get("limit") or ["40"])[0])
            self.send_json({"alerts": read_alerts(limit), "count": len(read_alerts(limit))})
            return
        if path == "/api/candles":
            tf = (qs.get("tf") or ["M15"])[0].upper()
            count = int((qs.get("count") or ["260"])[0])
            try:
                self.send_json(fetch_candles(tf, count))
            except Exception as exc:
                self.send_json({"error": str(exc), "timeframe": tf, "candles": []}, status=400)
            return

        # App shell and static files.
        if path in {"/", "/trade", "/markets", "/signals", "/risk", "/journal", "/settings"}:
            self.send_file(FRONTEND_DIR / "index.html")
            return
        rel = path.lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self.send_error(404)
            return
        candidate = FRONTEND_DIR / rel
        if candidate.exists():
            self.send_file(candidate)
            return
        # Client-side route fallback.
        if re.match(r"^/(trade|markets|signals|risk|journal|settings)(/.*)?$", path):
            self.send_file(FRONTEND_DIR / "index.html")
            return
        self.send_error(404)


def build_server(host: str = "0.0.0.0", port: int = 8770) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), ReactCommandCenterHandler)


def serve(host: str = "0.0.0.0", port: int = 8770) -> None:
    httpd = build_server(host, port)
    print(f"gold-trader React Command Center: http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    serve(host, port)


if __name__ == "__main__":
    main()

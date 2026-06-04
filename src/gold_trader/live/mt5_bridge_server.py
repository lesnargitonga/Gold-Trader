"""MT5 bridge HTTP server — runs *under Wine* on Linux (or on a Windows host).

This process:
1. Imports MetaTrader5 (Windows-only Python pkg).
2. Wraps an MT5LocalBroker.
3. Exposes its operations over a small HTTP API.

The Linux-side agent (running natively) talks to this via MT5RemoteBroker.

Architecture
------------
    [ Linux native Python ]
       agent-cycle
       MT5RemoteBroker  --HTTP-->  [ Wine: Windows Python ]
                                      mt5_bridge_server
                                      MT5LocalBroker
                                      MetaTrader5 ---PIPE---> [ Wine: MT5 terminal ]

The bridge is intentionally tiny (single-file, stdlib only — runs under any
Python ≥ 3.10 even bare Wine-side).  Auth is a shared-secret header so the
HTTP port can sit on localhost without further hardening.

Endpoints
---------
GET  /healthz                       -> {"ok": true, "broker": "mt5_local"}
GET  /account                       -> AccountInfo dict
GET  /position?magic=N              -> OpenPosition dict | null
POST /order   (json: OrderRequest)  -> OrderResult dict
POST /close   (json: {id, reason})  -> ClosedTrade dict | null

All non-2xx responses include {"error": "..."} JSON.

Run
---
    set GOLD_BRIDGE_SECRET=...   (or export on bash-style)
    set GOLD_SYMBOL=GOLD
    python -m gold_trader.live.mt5_bridge_server --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque

# We import the broker module lazily inside main(); this lets the file be
# imported on Linux for tests without a MetaTrader5 dep.
SHARED_SECRET_HEADER = "X-Gold-Bridge-Secret"


# ---------------------------------------------------------------------------
# Tick feed (Phase B1).  A background thread polls ``mt5.symbol_info_tick``
# at ~1 Hz and pushes into an in-memory deque.  Bridge clients pull recent
# ticks via /ticks and inspect freshness via /last-tick (used by the
# Linux-side connectivity watchdog).
# ---------------------------------------------------------------------------


class TickFeed:
    """Thread-safe circular buffer of recent ticks with a poller thread.

    The poller is intentionally simple: a 1 Hz loop that fetches the latest
    tick and skips duplicates.  Backpressure is handled by ``maxlen`` —
    older ticks fall off when the deque fills.
    """

    def __init__(
        self,
        symbol: str,
        *,
        poll_fn,
        maxlen: int = 7200,  # ~2h at 1 Hz
        interval_sec: float = 1.0,
    ) -> None:
        self._symbol = symbol
        self._poll_fn = poll_fn
        self._interval = float(interval_sec)
        self._buf: Deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seen_ts: float | None = None
        self._last_error: str | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="tick-feed", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                t = self._poll_fn(self._symbol)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                t = None
            if t is not None:
                # MetaTrader5 returns a namedtuple-ish; convert.
                ts = float(getattr(t, "time", 0.0)) or time.time()
                if self._last_seen_ts is None or ts != self._last_seen_ts:
                    self._last_seen_ts = ts
                    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    bid = float(getattr(t, "bid", 0.0))
                    ask = float(getattr(t, "ask", 0.0))
                    last = getattr(t, "last", None)
                    volume = getattr(t, "volume", None)
                    flags = getattr(t, "flags", None)
                    with self._lock:
                        self._buf.append({
                            "symbol": self._symbol,
                            "ts": iso,
                            "bid": bid,
                            "ask": ask,
                            "last": float(last) if last is not None else None,
                            "volume": float(volume) if volume is not None else None,
                            "flags": int(flags) if flags is not None else None,
                            "received_at": datetime.now(timezone.utc).isoformat(),
                        })
                    self._last_error = None
            self._stop.wait(self._interval)

    def snapshot(self, *, since_iso: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            ticks = list(self._buf)
        if since_iso is None:
            return ticks
        return [t for t in ticks if t["ts"] >= since_iso]

    def last_tick(self) -> dict[str, Any] | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def health(self) -> dict[str, Any]:
        last = self.last_tick()
        now = datetime.now(timezone.utc)
        age = None
        if last is not None:
            try:
                age = (now - datetime.fromisoformat(last["ts"])).total_seconds()
            except Exception:  # noqa: BLE001
                age = None
        return {
            "symbol": self._symbol,
            "buffer_size": len(self._buf),
            "last_tick_ts": last["ts"] if last else None,
            "last_tick_age_sec": age,
            "last_error": self._last_error,
        }



def _serialize(obj: Any) -> Any:
    """Make dataclasses + datetimes + enums JSON-safe."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value") and not isinstance(obj, type):
        # Enum
        try:
            return obj.value
        except Exception:
            pass
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return str(obj)


class BridgeHandler(BaseHTTPRequestHandler):
    # Set by main() before serve_forever.
    broker = None  # type: ignore[assignment]
    secret: str = ""
    tick_feed: "TickFeed | None" = None

    # Quieter logging — default BaseHTTPRequestHandler logs every request.
    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        if '"GET ' in message and '" 200 ' in message:
            return
        sys.stderr.write("[bridge] " + message + "\n")

    # --- helpers ---
    def _check_auth(self) -> bool:
        if not self.secret:
            return True
        provided = self.headers.get(SHARED_SECRET_HEADER, "")
        return provided == self.secret

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(_serialize(payload)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _copy_rates(self, symbol: str | None, tf_minutes: int, count: int) -> list[dict[str, Any]]:
        """Pull the most recent ``count`` bars for ``symbol`` at the requested timeframe.

        Returns a list of ``{time, open, high, low, close, tick_volume, spread}``
        dicts, oldest -> newest.  ``time`` is unix seconds (UTC).
        """
        mt5_mod = getattr(self.broker, "_mt5", None)
        if mt5_mod is None:
            return []
        sym = symbol or getattr(self.broker, "_symbol", None) or getattr(self.broker, "symbol", "")
        if not sym:
            return []
        # Map minute count -> MT5 timeframe constant.
        tf_map = {
            1: "TIMEFRAME_M1", 2: "TIMEFRAME_M2", 3: "TIMEFRAME_M3",
            5: "TIMEFRAME_M5", 10: "TIMEFRAME_M10", 15: "TIMEFRAME_M15",
            30: "TIMEFRAME_M30",
            60: "TIMEFRAME_H1", 120: "TIMEFRAME_H2", 240: "TIMEFRAME_H4",
            1440: "TIMEFRAME_D1", 10080: "TIMEFRAME_W1",
        }
        tf_attr = tf_map.get(tf_minutes, "TIMEFRAME_M15")
        tf_const = getattr(mt5_mod, tf_attr, None)
        if tf_const is None:
            return []
        rates = mt5_mod.copy_rates_from_pos(sym, tf_const, 0, count)
        if rates is None:
            return []
        out: list[dict[str, Any]] = []
        for r in rates:
            try:
                names = getattr(r, "dtype", None)
                fields = set(names.names or ()) if names is not None else set()
                tick_volume = r["tick_volume"] if "tick_volume" in fields else 0.0
                spread = r["spread"] if "spread" in fields else 0.0
                out.append({
                    "time": int(r["time"]),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "tick_volume": float(tick_volume),
                    "spread": float(spread),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # --- routes ---
    def do_GET(self) -> None:  # noqa: N802 - http.server signature
        if not self._check_auth():
            self._send(401, {"error": "bad shared secret"})
            return
        try:
            if self.path.startswith("/healthz"):
                payload: dict[str, Any] = {
                    "ok": True,
                    "broker": getattr(self.broker, "name", "?"),
                }
                if self.tick_feed is not None:
                    payload["tick_feed"] = self.tick_feed.health()
                self._send(200, payload)
                return
            if self.path.startswith("/last-tick"):
                if self.tick_feed is None:
                    self._send(200, None)
                    return
                self._send(200, self.tick_feed.last_tick())
                return
            if self.path.startswith("/ticks"):
                if self.tick_feed is None:
                    self._send(200, [])
                    return
                from urllib.parse import unquote
                since_iso: str | None = None
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for kv in qs.split("&"):
                        if kv.startswith("since="):
                            since_iso = unquote(kv.split("=", 1)[1])
                self._send(200, self.tick_feed.snapshot(since_iso=since_iso))
                return
            if self.path.startswith("/account"):
                info = self.broker.get_account_info()
                self._send(200, info)
                return
            if self.path.startswith("/candles"):
                # /candles?symbol=...&timeframe=15&count=500
                from urllib.parse import unquote
                symbol = None
                tf_minutes = 15
                count = 500
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for kv in qs.split("&"):
                        if kv.startswith("symbol="):
                            symbol = unquote(kv.split("=", 1)[1])
                        elif kv.startswith("timeframe="):
                            tf_minutes = int(kv.split("=", 1)[1])
                        elif kv.startswith("count="):
                            count = max(1, min(int(kv.split("=", 1)[1]), 5000))
                bars = self._copy_rates(symbol, tf_minutes, count)
                self._send(200, bars)
                return
            if self.path.startswith("/symbols"):
                # /symbols — return current symbol + tradeable info
                payload = {"symbol": getattr(self.broker, "_symbol", None) or getattr(self.broker, "symbol", None)}
                self._send(200, payload)
                return
            if self.path.startswith("/position"):
                # Optional ?magic=N
                magic = None
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for kv in qs.split("&"):
                        if kv.startswith("magic="):
                            magic = int(kv.split("=", 1)[1])
                pos = self.broker.get_open_position() if magic is None else self.broker.get_open_position(magic)
                self._send(200, pos)
                return
            if self.path.startswith("/pending"):
                magic = None
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for kv in qs.split("&"):
                        if kv.startswith("magic="):
                            magic = int(kv.split("=", 1)[1])
                pending = (
                    self.broker.get_pending_order()
                    if magic is None
                    else self.broker.get_pending_order(magic)
                )
                self._send(200, pending)
                return
            if self.path.startswith("/deals"):
                # /deals?since=<iso8601>&magic=<int>
                from datetime import datetime as _dt
                from urllib.parse import unquote
                since: _dt | None = None
                magic = None
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for kv in qs.split("&"):
                        if kv.startswith("since="):
                            since = _dt.fromisoformat(unquote(kv.split("=", 1)[1]))
                        elif kv.startswith("magic="):
                            magic = int(kv.split("=", 1)[1])
                if since is None:
                    self._send(400, {"error": "missing ?since=<iso8601>"})
                    return
                deals = (
                    self.broker.get_deals_since(since)
                    if magic is None
                    else self.broker.get_deals_since(since, magic)
                )
                self._send(200, list(deals))
                return
            self._send(404, {"error": f"unknown path {self.path}"})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._send(401, {"error": "bad shared secret"})
            return
        try:
            payload = self._read_json()
            if self.path.startswith("/order"):
                from .broker import OrderRequest, OrderSide

                side = OrderSide(payload["side"]) if isinstance(payload["side"], str) else payload["side"]
                req = OrderRequest(
                    symbol=payload["symbol"],
                    side=side,
                    risk_dollars=float(payload["risk_dollars"]),
                    stop_price=float(payload["stop_price"]),
                    target_price=float(payload["target_price"]),
                    entry_price=(
                        float(payload["entry_price"])
                        if payload.get("entry_price") is not None
                        else None
                    ),
                    magic=int(payload.get("magic", 20260507)),
                    comment=str(payload.get("comment", "")),
                )
                result = self.broker.place_market_order(req)
                self._send(200, result)
                return
            if self.path.startswith("/close"):
                trade = self.broker.close_position(
                    str(payload["broker_order_id"]),
                    reason=str(payload.get("reason", "manual")),
                )
                self._send(200, trade)
                return
            if self.path.startswith("/cancel"):
                ok = self.broker.cancel_pending_order(
                    str(payload["broker_order_id"])
                )
                self._send(200, {"cancelled": bool(ok)})
                return
            self._send(404, {"error": f"unknown path {self.path}"})
        except KeyError as exc:
            self._send(400, {"error": f"missing field {exc}"})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 HTTP bridge (Wine/Windows side)")
    parser.add_argument("--host", default=os.environ.get("GOLD_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GOLD_BRIDGE_PORT", "8765")))
    parser.add_argument("--symbol", default=os.environ.get("GOLD_SYMBOL", "GOLD"))
    parser.add_argument("--magic", type=int, default=int(os.environ.get("GOLD_MAGIC", "20260507")))
    args = parser.parse_args(argv)

    secret = os.environ.get("GOLD_BRIDGE_SECRET", "")
    if not secret:
        print(
            "[bridge] WARNING: GOLD_BRIDGE_SECRET not set — running without auth. "
            "ONLY safe on 127.0.0.1 with no other users.",
            file=sys.stderr,
        )

    # Lazy import — only fails on Wine without MetaTrader5 installed.
    from .mt5_broker import MT5LocalBroker

    login_str = os.environ.get("MT5_LOGIN")
    broker = MT5LocalBroker(
        symbol=args.symbol,
        magic=args.magic,
        deviation_points=int(os.environ.get("MT5_DEVIATION", "20")),
        account_type=os.environ.get("MT5_ACCOUNT_TYPE", "demo"),
        login=int(login_str) if login_str else None,
        password=os.environ.get("MT5_PASSWORD"),
        server=os.environ.get("MT5_SERVER"),
        terminal_path=os.environ.get("MT5_TERMINAL_PATH"),
    )
    broker.connect()  # fail fast if MT5 not reachable

    BridgeHandler.broker = broker
    BridgeHandler.secret = secret

    # Tick feed (Phase B1).  Polls broker._mt5.symbol_info_tick at 1 Hz.
    tick_feed: TickFeed | None = None
    try:
        mt5_mod = getattr(broker, "_mt5", None)
        if mt5_mod is not None and hasattr(mt5_mod, "symbol_info_tick"):
            tick_feed = TickFeed(
                symbol=args.symbol,
                poll_fn=mt5_mod.symbol_info_tick,
                interval_sec=float(os.environ.get("GOLD_TICK_INTERVAL", "1.0")),
                maxlen=int(os.environ.get("GOLD_TICK_BUFFER", "7200")),
            )
            tick_feed.start()
            BridgeHandler.tick_feed = tick_feed
            print(
                f"[bridge] tick feed started symbol={args.symbol} "
                f"interval={tick_feed._interval}s",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge] tick feed disabled: {exc}", file=sys.stderr)
        tick_feed = None

    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"[bridge] listening on http://{args.host}:{args.port} symbol={args.symbol}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[bridge] shutting down", file=sys.stderr)
    finally:
        if tick_feed is not None:
            tick_feed.stop()
        broker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

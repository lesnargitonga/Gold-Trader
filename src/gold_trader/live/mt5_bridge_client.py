"""MT5 remote broker — Linux-side HTTP client to the Wine bridge.

Same Broker interface as MT5LocalBroker; the only difference is that every
operation goes over HTTP to ``mt5_bridge_server`` instead of calling the
MetaTrader5 Python pkg directly.

This is what your Linux-native agent-cycle uses.  It works as long as the
bridge process is running under Wine (or on a Windows VPS) and reachable
on the configured host:port.

Configuration (env vars)
------------------------
GOLD_BROKER=mt5_remote
GOLD_BRIDGE_URL=http://127.0.0.1:8765      (default)
GOLD_BRIDGE_SECRET=<shared secret>          (must match the server)
GOLD_MAGIC=20260507                         (default)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .broker import (
    AccountInfo,
    BrokerError,
    ClosedTrade,
    Deal,
    OpenPosition,
    OrderRequest,
    OrderResult,
    OrderSide,
    PendingOrder,
)


SHARED_SECRET_HEADER = "X-Gold-Bridge-Secret"


class MT5RemoteBroker:
    """HTTP client that satisfies the ``Broker`` Protocol."""

    name: str = "mt5_remote"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8765",
        shared_secret: str = "",
        timeout: float = 15.0,
        magic: int = 20260507,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._secret = shared_secret
        self._timeout = timeout
        self._magic = magic

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._secret:
            h[SHARED_SECRET_HEADER] = self._secret
        return h

    def _get(self, path: str) -> Any:
        url = f"{self._base}{path}"
        req = Request(url, headers=self._headers(), method="GET")
        return self._call(req)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method="POST")
        return self._call(req)

    def _call(self, req: Request) -> Any:
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                msg = payload.get("error", str(exc))
            except Exception:
                msg = str(exc)
            raise BrokerError(f"bridge HTTP {exc.code}: {msg}") from exc
        except URLError as exc:
            raise BrokerError(f"bridge unreachable at {self._base}: {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrokerError(f"bridge returned non-JSON: {raw[:200]}") from exc

    # ------------------------------------------------------------------
    def healthz(self) -> dict[str, Any]:
        return self._get("/healthz") or {}

    def get_candles(
        self,
        *,
        symbol: str = "",
        timeframe_minutes: int = 15,
        count: int = 500,
    ) -> list[dict[str, Any]]:
        from urllib.parse import urlencode
        qs = urlencode({"symbol": symbol, "timeframe": timeframe_minutes, "count": count})
        data = self._get(f"/candles?{qs}")
        return data if isinstance(data, list) else []

    def get_last_tick(self) -> dict[str, Any] | None:
        data = self._get("/last-tick")
        return data if isinstance(data, dict) else None

    def get_ticks_since(self, since: datetime) -> list[dict[str, Any]]:
        from urllib.parse import quote

        data = self._get(f"/ticks?since={quote(since.isoformat(), safe='')}")
        return data if isinstance(data, list) else []

    def get_account_info(self) -> AccountInfo:
        data = self._get("/account")
        if not isinstance(data, dict):
            raise BrokerError(f"bridge /account returned {data!r}")
        return AccountInfo(
            equity=float(data["equity"]),
            balance=float(data["balance"]),
            currency=str(data["currency"]),
            margin_used=float(data["margin_used"]),
            margin_free=float(data["margin_free"]),
            leverage=float(data["leverage"]),
        )

    def get_open_position(self, magic: int | None = None) -> OpenPosition | None:
        path = "/position" if magic is None else f"/position?magic={magic}"
        data = self._get(path)
        if data is None:
            return None
        return OpenPosition(
            broker_order_id=str(data["broker_order_id"]),
            symbol=str(data["symbol"]),
            side=OrderSide(data["side"]),
            units=float(data["units"]),
            entry_price=float(data["entry_price"]),
            stop_price=float(data["stop_price"]),
            target_price=float(data["target_price"]),
            opened_at=datetime.fromisoformat(data["opened_at"]),
            unrealised_pnl=float(data["unrealised_pnl"]),
            magic=int(data["magic"]),
        )

    def place_market_order(self, request: OrderRequest) -> OrderResult:
        body = {
            "symbol": request.symbol,
            "side": request.side.value,
            "risk_dollars": request.risk_dollars,
            "stop_price": request.stop_price,
            "target_price": request.target_price,
            "entry_price": request.entry_price,
            "magic": request.magic,
            "comment": request.comment,
        }
        data = self._post("/order", body)
        if not isinstance(data, dict):
            raise BrokerError(f"bridge /order returned {data!r}")
        return OrderResult(
            accepted=bool(data["accepted"]),
            broker_order_id=data.get("broker_order_id"),
            fill_price=data.get("fill_price"),
            units=data.get("units"),
            error=data.get("error"),
        )

    def close_position(self, broker_order_id: str, reason: str = "manual") -> ClosedTrade | None:
        body = {"broker_order_id": broker_order_id, "reason": reason}
        data = self._post("/close", body)
        if data is None:
            return None
        return ClosedTrade(
            broker_order_id=str(data["broker_order_id"]),
            symbol=str(data["symbol"]),
            side=OrderSide(data["side"]),
            units=float(data["units"]),
            entry_price=float(data["entry_price"]),
            exit_price=float(data["exit_price"]),
            opened_at=datetime.fromisoformat(data["opened_at"]),
            closed_at=datetime.fromisoformat(data["closed_at"]),
            pnl_dollars=float(data["pnl_dollars"]),
            exit_reason=str(data["exit_reason"]),
        )

    def get_pending_order(self, magic: int | None = None) -> PendingOrder | None:
        path = "/pending" if magic is None else f"/pending?magic={magic}"
        data = self._get(path)
        if data is None:
            return None
        return PendingOrder(
            broker_order_id=str(data["broker_order_id"]),
            symbol=str(data["symbol"]),
            side=OrderSide(data["side"]),
            units=float(data["units"]),
            entry_price=float(data["entry_price"]),
            stop_price=float(data["stop_price"]),
            target_price=float(data["target_price"]),
            placed_at=datetime.fromisoformat(data["placed_at"]),
            magic=int(data["magic"]),
        )

    def cancel_pending_order(self, broker_order_id: str) -> bool:
        body = {"broker_order_id": broker_order_id}
        data = self._post("/cancel", body)
        if not isinstance(data, dict):
            return False
        return bool(data.get("cancelled", False))

    def get_deals_since(
        self,
        since: datetime,
        magic: int | None = None,
    ) -> list[Deal]:
        # Encode the timestamp as ISO-8601 — bridge parses with fromisoformat.
        from urllib.parse import quote

        since_iso = since.isoformat()
        path = f"/deals?since={quote(since_iso, safe='')}"
        if magic is not None:
            path += f"&magic={magic}"
        data = self._get(path)
        if not isinstance(data, list):
            return []
        out: list[Deal] = []
        for d in data:
            try:
                out.append(
                    Deal(
                        deal_id=str(d["deal_id"]),
                        order_ticket=(
                            str(d["order_ticket"])
                            if d.get("order_ticket") is not None
                            else None
                        ),
                        position_ticket=(
                            str(d["position_ticket"])
                            if d.get("position_ticket") is not None
                            else None
                        ),
                        symbol=str(d["symbol"]),
                        side=OrderSide(d["side"]),
                        volume=float(d["volume"]),
                        price=float(d["price"]),
                        profit=float(d.get("profit", 0.0)),
                        swap=float(d.get("swap", 0.0)),
                        commission=float(d.get("commission", 0.0)),
                        fee=float(d.get("fee", 0.0)),
                        deal_type=int(d.get("deal_type", 0)),
                        entry_type=int(d.get("entry_type", 0)),
                        reason=int(d.get("reason", 0)),
                        time=datetime.fromisoformat(d["time"]),
                        magic=int(d.get("magic", 0)),
                        comment=str(d.get("comment", "") or ""),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return out

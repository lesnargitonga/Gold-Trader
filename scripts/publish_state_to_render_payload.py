#!/usr/bin/env python3
"""Build the local-authoritative cloud state payload for the Render dashboard."""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LOGS = REPO / "logs"
CLOUD = REPO / "data" / "cloud_state"
LATEST = CLOUD / "latest_cloud_state.json"


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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_json(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_json(v) for v in value]
    if isinstance(value, tuple):
        return [safe_json(v) for v in value]
    return value


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return safe_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    return default


def read_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(safe_json(obj))
    return list(reversed(rows))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(safe_json(payload), indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(safe_json(row), ensure_ascii=False, allow_nan=False) + "\n")
    tmp.replace(path)


def paper_allowed(decision: dict[str, Any]) -> bool:
    if decision.get("paper_allowed") is not None:
        return bool(decision.get("paper_allowed"))
    action = str(decision.get("action") or "").upper()
    blockers = decision.get("hard_blocks") or decision.get("blockers") or decision.get("readable_blockers") or []
    return "TRADE_READY" in action and not bool(blockers)


def normalize_decision(decision: dict[str, Any], market_health: dict[str, Any]) -> dict[str, Any]:
    out = dict(decision)
    out["source"] = "local_authoritative_engine"
    out["live_allowed"] = False
    out["live_orders_enabled"] = False
    out["paper_allowed"] = paper_allowed(out)
    out["render_dashboard_mode"] = True

    market_context = out.get("market_context") if isinstance(out.get("market_context"), dict) else {}
    market_context = dict(market_context)
    data_health = out.get("data_health") if isinstance(out.get("data_health"), dict) else {}
    data_health = dict(data_health)

    spread_source = market_health.get("spread_source") or (market_health.get("details") or {}).get("spread_source")
    spread_points = market_health.get("spread_points") or (market_health.get("details") or {}).get("spread_points")
    if spread_source:
        market_context["spread_source"] = spread_source
    if spread_points is not None:
        market_context["spread_points"] = spread_points
    if market_context.get("spread_source") == "live_tick":
        data_health["spread"] = "live_tick"

    out["market_context"] = market_context
    out["data_health"] = data_health

    reads = out.get("timeframe_reads") if isinstance(out.get("timeframe_reads"), list) else []
    candles_loaded = 0
    for row in reads:
        if not isinstance(row, dict):
            continue
        try:
            candles_loaded += int(float(row.get("candles") or 0))
        except (TypeError, ValueError):
            pass

    cloud_status = out.get("cloud_status") if isinstance(out.get("cloud_status"), dict) else {}
    cloud_status = dict(cloud_status)
    cloud_status.update({
        "analysis": "online" if candles_loaded else cloud_status.get("analysis", "waiting_for_data"),
        "source": "local_authoritative_engine",
        "data_provider": "local_authoritative_engine",
        "broker": "MT5 bridge local",
        "orders": "locked",
        "live_orders": "locked",
        "execution_mode": "paper",
        "candles_loaded": cloud_status.get("candles_loaded", candles_loaded),
        "spread": market_context.get("spread_points") or cloud_status.get("spread"),
        "spread_source": market_context.get("spread_source") or data_health.get("spread"),
    })
    out["cloud_status"] = cloud_status
    return out


def main() -> int:
    limit = max(1, int(os.getenv("GOLD_CLOUD_SYNC_LIMIT", "50")))
    decision = read_json(LOGS / "ifvg_mtf_decision_state.json", {})
    if not isinstance(decision, dict) or not decision:
        print("missing local decision state: logs/ifvg_mtf_decision_state.json", file=sys.stderr)
        return 2

    market_health = read_json(LOGS / "market_context_health.json", {})
    if not isinstance(market_health, dict) or not market_health:
        market_health = read_json(LOGS / "market_health.json", {})
    if not isinstance(market_health, dict):
        market_health = {}

    performance = read_json(LOGS / "paper_performance_report.json", dict(EMPTY_PERFORMANCE))
    if not isinstance(performance, dict):
        performance = dict(EMPTY_PERFORMANCE)
    provider_health = read_json(LOGS / "provider_health.json", {})
    if not isinstance(provider_health, dict):
        provider_health = {}

    paper_signals = read_jsonl(LOGS / "paper_signal_outcomes.jsonl", limit)
    decision_journal = read_jsonl(LOGS / "decision_journal.jsonl", limit)
    published_at = now_iso()
    decision = normalize_decision(decision, market_health)

    payload: dict[str, Any] = {
        "published_at": published_at,
        "source": "local_authoritative_engine",
        "mode": "paper",
        "live_orders": "locked",
        "decision": decision,
        "performance": performance,
        "paper_signals": paper_signals,
        "decision_journal": decision_journal,
        "provider_health": provider_health,
        "market_health": market_health,
    }

    write_json(LATEST, payload)
    write_json(CLOUD / "ifvg_mtf_decision_state.json", decision)
    write_json(CLOUD / "paper_performance_report.json", performance)
    write_json(CLOUD / "provider_health.json", provider_health)
    write_jsonl(CLOUD / "paper_signal_outcomes.jsonl", paper_signals)
    write_jsonl(CLOUD / "decision_journal.jsonl", decision_journal)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(LATEST),
                "published_at": published_at,
                "decision_action": decision.get("action"),
                "decision_score": decision.get("final_score"),
                "paper_signals": len(paper_signals),
                "decision_journal": len(decision_journal),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

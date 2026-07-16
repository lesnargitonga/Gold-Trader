"""Per-slice TP routing & paper eligibility (config-driven).

The (grade x timeframe) -> {eligible, tp_model, live_track} table lives in
``config/tp_routing.json`` so slices can be tuned without code changes. Backed by
``logs/ifvg_tp_structure_audit.json`` + ``logs/ifvg_htf_edge_audit.json``.

Policy encoded by the default config (2026-06):
  * Grade A: eligible on every timeframe, 5R runner target, counts toward the
    20-trade live go/no-go (``live_track=true``).
  * Grade B: eligible PAPER-ONLY on 4H with a 5R runner; blocked on 1H/15m;
    never counts toward the live track. Nearest-zone targets disallowed for B.
  * Grade C/D: never eligible.

Everything is paper while live orders are locked — ``eligible`` means
"show an actionable paper go-ahead", not "send a live order".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROUTING_PATH = REPO_ROOT / "config" / "tp_routing.json"

_INELIGIBLE = {"eligible": False, "tp_model": "off", "live_track": False}
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None


def _load(path: Path | None = None) -> dict[str, Any]:
    """Load routing config, reloading when the file mtime changes."""
    global _cache, _cache_mtime
    p = path or DEFAULT_ROUTING_PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"routes": {}, "default_risk_usd": 30.0, "runner_r_multiple": 5.0}
    if _cache is None or _cache_mtime != mtime or path is not None:
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {"routes": {}, "default_risk_usd": 30.0, "runner_r_multiple": 5.0}
        if path is None:
            _cache, _cache_mtime = data, mtime
        else:
            return data
    return _cache  # type: ignore[return-value]


def runner_r_multiple(path: Path | None = None) -> float:
    return float(_load(path).get("runner_r_multiple", 5.0))


def default_risk_usd(path: Path | None = None) -> float:
    return float(_load(path).get("default_risk_usd", 30.0))


def resolve_tp_route(grade: str, timeframe: int | None, *, path: Path | None = None) -> dict[str, Any]:
    """Return {eligible, tp_model, live_track, note, reason} for a grade x TF slice."""
    cfg = _load(path)
    routes = cfg.get("routes") or {}
    tf_key = str(int(timeframe)) if timeframe else ""
    g = (grade or "D").upper()
    entry = ((routes.get(g) or {}).get(tf_key)) if tf_key else None
    if not isinstance(entry, dict):
        reason = (
            f"Grade {g} on M{tf_key or '?'} not eligible for entry — no route configured "
            f"(see config/tp_routing.json; audit-backed)"
        )
        return {**_INELIGIBLE, "note": "", "reason": reason}
    eligible = bool(entry.get("eligible"))
    tp_model = str(entry.get("tp_model") or "off")
    live_track = bool(entry.get("live_track"))
    note = str(entry.get("note") or "")
    if eligible:
        reason = note or f"Grade {g} on M{tf_key} eligible — {tp_model}"
    else:
        reason = note or f"Grade {g} on M{tf_key} not eligible for entry (audit)"
    return {"eligible": eligible, "tp_model": tp_model, "live_track": live_track,
            "note": note, "reason": reason}


def compute_target(
    side: str,
    entry: float | None,
    stop: float | None,
    tp_model: str,
    *,
    structural_fallback: float | None = None,
    r_multiple: float | None = None,
) -> float | None:
    """Resolve the routed target price. runner_5r => entry +/- N*risk."""
    if tp_model == "off" or entry is None or stop is None:
        return None
    if tp_model == "structural":
        return structural_fallback
    if tp_model.startswith("runner"):
        risk = abs(float(entry) - float(stop))
        if risk <= 0:
            return None
        rm = r_multiple if r_multiple is not None else runner_r_multiple()
        return float(entry) + rm * risk if str(side).lower() == "long" else float(entry) - rm * risk
    return structural_fallback

"""Runtime config (persisted JSON) for operator-toggleable settings.

The cron wrapper reads this file (via ``read_runtime_config``) at the
top of every cycle so changes made from the web UI take effect on the
next iteration without editing the shell script.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2].parent / "config" / "runtime_config.json"


@dataclass
class RuntimeConfig:
    macro_filter_mode: str = "soft"  # off | soft | hard
    auto_trade_enabled: bool = True
    news_blackout_min: float = 0.0  # 0 = disabled, e.g. 15.0 = ±15min around high-impact USD events
    bridge_url: str = "http://127.0.0.1:8765"
    bridge_secret: str = ""
    symbol: str = "XAUUSD"
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # Don't expose the secret to the UI in plaintext — surface a presence flag instead.
        d["bridge_secret_set"] = bool(self.bridge_secret)
        d.pop("bridge_secret", None)
        return d


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        return RuntimeConfig()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return RuntimeConfig()
    cfg = RuntimeConfig()
    if isinstance(data, dict):
        if data.get("macro_filter_mode") in ("off", "soft", "hard"):
            cfg.macro_filter_mode = data["macro_filter_mode"]
        if isinstance(data.get("auto_trade_enabled"), bool):
            cfg.auto_trade_enabled = data["auto_trade_enabled"]
        try:
            v = float(data.get("news_blackout_min", 0.0))
            if 0.0 <= v <= 240.0:
                cfg.news_blackout_min = v
        except (TypeError, ValueError):
            pass
        if isinstance(data.get("notes"), str):
            cfg.notes = data["notes"]
        if isinstance(data.get("bridge_url"), str) and data["bridge_url"].startswith(("http://", "https://")):
            cfg.bridge_url = data["bridge_url"][:200]
        if isinstance(data.get("bridge_secret"), str):
            cfg.bridge_secret = data["bridge_secret"][:200]
        if isinstance(data.get("symbol"), str) and data["symbol"]:
            cfg.symbol = data["symbol"][:20]
    return cfg


def save_runtime_config(cfg: RuntimeConfig, path: Path | None = None) -> None:
    p = path or DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    raw = asdict(cfg)  # write the FULL dict including bridge_secret
    tmp.write_text(json.dumps(raw, indent=2))
    tmp.replace(p)

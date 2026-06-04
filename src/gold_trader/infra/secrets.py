"""Persisted operator secrets (API keys) with env-var override.

Secrets live in ``config/secrets.json`` (gitignored). Environment variables
always win when set, so you can rotate keys in Settings and clear them when
testing is over without re-exporting shell vars every session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SECRETS_PATH = REPO_ROOT / "config" / "secrets.json"


def secrets_path(path: Path | None = None) -> Path:
    return path or DEFAULT_SECRETS_PATH


def load_secrets(path: Path | None = None) -> dict[str, str]:
    p = secrets_path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("openai_api_key", "bridge_secret", "twelve_data_api_key", "fmp_api_key", "finnhub_api_key"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def save_secrets(
    updates: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, str]:
    """Merge *updates* into secrets file. Empty string or clear_* removes a key."""
    p = secrets_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_secrets(p)
    if updates.get("clear_openai_api_key"):
        current.pop("openai_api_key", None)
    elif isinstance(updates.get("openai_api_key"), str) and updates["openai_api_key"].strip():
        current["openai_api_key"] = updates["openai_api_key"].strip()[:500]
    if updates.get("clear_bridge_secret"):
        current.pop("bridge_secret", None)
    elif isinstance(updates.get("bridge_secret"), str) and updates["bridge_secret"].strip():
        current["bridge_secret"] = updates["bridge_secret"].strip()[:200]
    if updates.get("clear_twelve_data_api_key"):
        current.pop("twelve_data_api_key", None)
    elif isinstance(updates.get("twelve_data_api_key"), str) and updates["twelve_data_api_key"].strip():
        current["twelve_data_api_key"] = updates["twelve_data_api_key"].strip()[:200]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True))
    tmp.replace(p)
    return current


def resolve_openai_api_key(*, path: Path | None = None) -> str:
    env = os.environ.get("OPENAI_API_KEY", "").strip()
    if env:
        return env
    return load_secrets(path).get("openai_api_key", "")


def resolve_twelve_data_api_key(*, path: Path | None = None) -> str:
    env = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if env:
        return env
    return load_secrets(path).get("twelve_data_api_key", "")


def resolve_bridge_secret(*, path: Path | None = None, runtime_fallback: str = "") -> str:
    env = os.environ.get("GOLD_BRIDGE_SECRET", "").strip()
    if env:
        return env
    stored = load_secrets(path).get("bridge_secret", "")
    if stored:
        return stored
    return (runtime_fallback or "").strip()


def secret_hint(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"…{value[-4:]}"


def secrets_status(*, path: Path | None = None, runtime_bridge_secret: str = "") -> dict[str, Any]:
    openai = resolve_openai_api_key(path=path)
    bridge = resolve_bridge_secret(path=path, runtime_fallback=runtime_bridge_secret)
    env_openai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    env_bridge = bool(os.environ.get("GOLD_BRIDGE_SECRET", "").strip())
    secrets_file = secrets_path(path)
    try:
        rel_path = str(secrets_file.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(secrets_file)
    return {
        "openai_api_key_set": bool(openai),
        "openai_api_key_hint": secret_hint(openai),
        "openai_api_key_from_env": env_openai,
        "bridge_secret_set": bool(bridge),
        "bridge_secret_hint": secret_hint(bridge),
        "bridge_secret_from_env": env_bridge,
        "secrets_path": rel_path,
    }

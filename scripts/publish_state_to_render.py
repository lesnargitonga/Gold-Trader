#!/usr/bin/env python3
"""Publish the prepared local cloud-state payload to the Render dashboard."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAYLOAD = REPO / "data" / "cloud_state" / "latest_cloud_state.json"
DEFAULT_URL = "https://gold-trader-kmaw.onrender.com/api/ingest-state"


def main() -> int:
    ingest_url = os.getenv("GOLD_RENDER_INGEST_URL", DEFAULT_URL).strip()
    token = os.getenv("GOLD_CLOUD_SYNC_TOKEN", "").strip()
    if not ingest_url:
        print("GOLD_RENDER_INGEST_URL is empty", file=sys.stderr)
        return 2
    if not token:
        print("GOLD_CLOUD_SYNC_TOKEN is required", file=sys.stderr)
        return 2
    if not PAYLOAD.exists():
        print(f"missing payload: {PAYLOAD}; run scripts/publish_state_to_render_payload.py first", file=sys.stderr)
        return 2

    body = PAYLOAD.read_bytes()
    req = urllib.request.Request(
        ingest_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gold-Sync-Token": token,
            "User-Agent": "GoldTraderLocalPublisher/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            print(json.dumps({"ok": True, "status": resp.getcode(), "response": text[:1000]}, indent=2))
            return 0 if 200 <= resp.getcode() < 300 else 1
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        print(json.dumps({"ok": False, "status": exc.code, "response": text[:1000]}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

STATE_PATH = Path(
    os.getenv("GOLD_RUNTIME_ROOT", str(Path(__file__).resolve().parents[3]))
).resolve() / "logs" / "telegram_alert_state.json"


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    enabled = os.getenv("GOLD_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if not enabled or not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _post(url, {"chat_id": chat_id, "text": text[:3900], "parse_mode": "Markdown"})
    return True


def send_decision_alert_if_needed(decision: dict) -> bool:
    action = decision.get("action", "WAIT")
    grade = decision.get("final_grade", "?")
    score = int(decision.get("final_score") or 0)
    blockers = decision.get("blockers") or []
    key = f"{action}:{grade}:{score}:{len(blockers)}"
    try:
        old = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    old_key = old.get("last_key")
    should = action in {"BUY", "SELL", "TRADE_READY"} or score >= int(os.getenv("GOLD_ALERT_SCORE_THRESHOLD", "70")) or key != old_key
    if not should:
        return False
    msg = decision.get("operator_message") or f"Gold Trader: {action} grade {grade} score {score}"
    ok = send_telegram(msg)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"last_key": key, "sent": ok}, indent=2), encoding="utf-8")
    return ok

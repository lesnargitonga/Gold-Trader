#!/usr/bin/env python3
"""Audit script for Gold-Trader local command center.

Writes two files under `logs/`: `system_audit.json` and `system_audit.md`.
Exits non-zero if critical checks fail.

Checks performed:
1. Which UI is referenced in `scripts/start.sh` (heuristic)
2. /api/decision returns valid JSON
3. /api/decision matches `logs/ifvg_mtf_decision_state.json` (if present)
4. Bridge endpoint responsiveness (tries common endpoints)
5. Spread source reported by decision (`market_context.spread_source` or `data_health.spread`)
6. Macro file existence and freshness (`data/macro/economic_calendar.json`)
7. Sentiment file existence, freshness and score (`logs/sentiment_state.json`)
8. Decision staleness (timestamp age)
9. Decision live/paper indicators
10. Journal files exist and are writable
11. Deprecated frontend files are not served by the UI
12. Render-only scripts separation (search for 'render' in `scripts/`)

Use: PYTHONPATH=src .venv/bin/python scripts/audit_gold_trader_system.py
"""
from __future__ import annotations

import json
import os
import sys
import http.client
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def iso_to_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        # Accept a variety of ISO-like formats
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 5) -> tuple[int, str, dict[str, Any]]:
    req = Request(url)
    # Add headers using add_header to preserve exact header names/casing
    if headers:
        for k, v in (headers or {}).items():
            try:
                req.add_header(k, v)
            except Exception:
                pass
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            code = getattr(resp, "getcode", lambda: 200)()
            body = resp.read().decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return code, body, hdrs
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body, {}
    except URLError as e:
        return 0, str(e), {}
    except socket.timeout:
        return 0, "timeout", {}


def bridge_http_get(url: str, bridge_secret: str | None = None, timeout: int = 5) -> tuple[int, str, dict[str, Any]]:
    """HTTP GET for bridge probes, preserving both bridge-secret header spellings."""
    parsed = urlsplit(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.putrequest("GET", path, skip_accept_encoding=True)
        conn.putheader("User-Agent", "GoldTraderAudit/1.0")
        if bridge_secret:
            conn.putheader("X-Gold-Bridge-Secret", bridge_secret)
            conn.putheader("X-GOLD-BRIDGE-SECRET", bridge_secret)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, hdrs
    except (OSError, http.client.HTTPException, socket.timeout) as exc:
        return 0, str(exc), {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def mark(name: str, ok: bool, details: str = "", critical: bool = False) -> dict[str, Any]:
    return {"ok": bool(ok), "details": str(details), "critical": bool(critical)}


def main() -> int:
    results: dict[str, Any] = {}
    critical_failures = []

    # 1) Which UI is active from scripts/start.sh (heuristic)
    try:
        start_sh = (ROOT / "scripts" / "start.sh").read_text()
        found_static = "src/gold_trader/web/static" in start_sh or "web ui" in start_sh.lower() or "gold_trader/web/server.py" in start_sh
        found_frontend = "frontend/" in start_sh or "command_center" in start_sh
        details = []
        if found_static:
            details.append("start.sh references official static UI/server")
        if found_frontend:
            details.append("start.sh references legacy frontend prototypes")
        if not details:
            details.append("no clear UI hint found in scripts/start.sh")
        results["start_script_ui"] = mark("start_script_ui", True, "; ".join(details), critical=False)
    except Exception as exc:
        results["start_script_ui"] = mark("start_script_ui", False, f"error reading scripts/start.sh: {exc}", critical=False)

    # 2) /api/decision returns valid JSON
    api_decision = None
    code, body, hdrs = http_get("http://127.0.0.1:8770/api/decision", timeout=6)
    if code != 200:
        results["api_decision"] = mark("api_decision", False, f"HTTP {code} response; body: {body[:200]!r}", critical=True)
        critical_failures.append("api_decision")
    else:
        try:
            api_decision = json.loads(body)
            results["api_decision"] = mark("api_decision", True, f"OK; keys: {list(api_decision.keys())}", critical=False)
        except Exception as exc:
            results["api_decision"] = mark("api_decision", False, f"invalid JSON: {exc}; raw: {body[:200]!r}", critical=True)
            critical_failures.append("api_decision")

    # 3) /api/decision matches logs/ifvg_mtf_decision_state.json
    decision_file = LOGS / "ifvg_mtf_decision_state.json"
    if decision_file.exists():
        try:
            file_dec = json.loads(decision_file.read_text())
            mismatch = []
            if api_decision is not None:
                for k in ("action", "final_score", "symbol", "timestamp_utc"):
                    a = api_decision.get(k) if isinstance(api_decision, dict) else None
                    b = file_dec.get(k)
                    if a != b:
                        mismatch.append(f"{k}: api={a!r} file={b!r}")
            details = f"file ok; mismatches: {mismatch}" if mismatch else "file ok; matches API (for inspected keys)"
            results["decision_file"] = mark("decision_file", True, details, critical=False)
        except Exception as exc:
            results["decision_file"] = mark("decision_file", False, f"failed to parse {decision_file}: {exc}", critical=True)
            critical_failures.append("decision_file_parse")
    else:
        results["decision_file"] = mark("decision_file", False, f"missing {decision_file}", critical=True)
        critical_failures.append("decision_file_missing")

    # 4) Bridge /last-tick works (try common endpoints)
    bridge_base = os.environ.get("GOLD_BRIDGE_URL", "http://127.0.0.1:8765")
    bridge_secret_candidates: list[tuple[str, str]] = []

    def add_bridge_secret(source: str, value: str | None) -> None:
        secret = (value or "").strip()
        if not secret:
            return
        if any(existing == secret for _src, existing in bridge_secret_candidates):
            return
        bridge_secret_candidates.append((source, secret))

    add_bridge_secret("env:GOLD_BRIDGE_SECRET", os.environ.get("GOLD_BRIDGE_SECRET"))
    # config/secrets.json -> bridge_secret (or fallback keys)
    try:
        sec_path = ROOT / "config" / "secrets.json"
        if sec_path.exists():
            sec = json.loads(sec_path.read_text())
            file_secret = sec.get("bridge_secret") or sec.get("GOLD_BRIDGE_SECRET") or sec.get("GOLD_BRIDGE_TOKEN")
            add_bridge_secret("config/secrets.json", file_secret)
    except Exception:
        pass
    # ~/.gold-mt5-wine/credentials.env -> GOLD_BRIDGE_SECRET
    try:
        cred_path = Path.home() / ".gold-mt5-wine" / "credentials.env"
        if cred_path.exists():
            for line in cred_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k == "GOLD_BRIDGE_SECRET":
                    add_bridge_secret("~/.gold-mt5-wine/credentials.env", v)
                    break
    except Exception:
        pass
    if not bridge_secret_candidates:
        bridge_secret_candidates.append(("missing", ""))

    bridge_ok = False
    bridge_details = []
    bridge_secret_source = "missing"
    endpoints = ["/last_tick", "/last-tick", "/tick", "/tick?symbol=GOLD", "/last_tick?symbol=GOLD", "/open_positions?symbol=GOLD", "/"]
    for ep in endpoints:
        url = bridge_base.rstrip("/") + ep
        for source, bridge_secret in bridge_secret_candidates:
            code, body, _ = bridge_http_get(url, bridge_secret=bridge_secret, timeout=3)
            bridge_details.append((url, source, code))
            if code == 200:
                bridge_ok = True
                bridge_secret_source = source
                break
            if code not in {401, 403}:
                break
        if bridge_ok:
            break
    bridge_statuses = {code for _url, _source, code in bridge_details}
    bridge_failure = "bridge_unreachable"
    if not bridge_ok and bridge_statuses.intersection({401, 403}):
        bridge_failure = "bridge_auth_failed"
    elif not bridge_ok and any(code > 0 for code in bridge_statuses):
        bridge_failure = "bridge_endpoint_unhealthy"
    results["bridge_last_tick"] = mark(
        "bridge_last_tick",
        bridge_ok,
        f"secret_source={bridge_secret_source}; checked: {bridge_details}",
        critical=not bridge_ok,
    )
    if not bridge_ok:
        critical_failures.append(bridge_failure)

    # 5) Spread source is live_tick?
    spread_ok = False
    spread_details = "unknown"
    try:
        mc = api_decision.get("market_context") if isinstance(api_decision, dict) else None
        dh = api_decision.get("data_health") if isinstance(api_decision, dict) else None
        dh_spread = dh.get("spread") if isinstance(dh, dict) else None
        if mc and isinstance(mc, dict):
            direct_source = mc.get("spread_source")
            spread = mc.get("spread") or mc.get("spread_feed") or mc.get("spread_points")
            if direct_source:
                spread_ok = direct_source == "live_tick"
                spread_details = f"market_context.spread_source={direct_source!r}; data_health.spread={dh_spread!r}"
            elif dh_spread:
                spread_ok = dh_spread == "live_tick"
                spread_details = f"data_health.spread={dh_spread!r}"
            elif isinstance(spread, dict):
                source = spread.get("source")
                spread_ok = source == "live_tick"
                spread_details = f"market_context.spread.source={source!r}; data_health.spread={dh_spread!r}"
            else:
                spread_details = f"market_context spread entry: {spread!r}; data_health.spread={dh_spread!r}"
        elif dh_spread:
            spread_ok = dh_spread == "live_tick"
            spread_details = f"data_health.spread={dh_spread!r}"
        else:
            spread_details = "no market_context in API decision"
    except Exception as exc:
        spread_details = str(exc)
    results["spread_source_live_tick"] = mark("spread_source_live_tick", spread_ok, spread_details, critical=False)

    # 6) Macro file exists & fresh
    macro_candidates = [ROOT / "data" / "macro" / "economic_calendar.json", ROOT / "config" / "manual_macro_calendar.json"]
    macro_found = None
    macro_details = []
    for p in macro_candidates:
        if p.exists():
            macro_found = p
            break
    if macro_found:
        age = now_ts() - macro_found.stat().st_mtime
        fresh = age < 3600
        try:
            data = json.loads(macro_found.read_text())
            has_state = bool(data)
        except Exception:
            has_state = False
        macro_details = f"path={macro_found} age_s={int(age)} fresh={fresh} has_state={has_state}"
        results["macro_file"] = mark("macro_file", fresh and has_state, macro_details, critical=not has_state)
        if not has_state:
            critical_failures.append("macro_no_state")
    else:
        results["macro_file"] = mark("macro_file", False, "missing macro file (checked common paths)", critical=True)
        critical_failures.append("macro_missing")

    # 7) Sentiment file exists, fresh and has score
    sent_candidates = list((ROOT / "logs").glob("*sentiment*.json*"))
    sent_found = sent_candidates[0] if sent_candidates else None
    if sent_found:
        try:
            sent_obj = json.loads(sent_found.read_text())
            age = now_ts() - sent_found.stat().st_mtime
            fresh = age < 3600
            has_score = any(k in sent_obj for k in ("sentiment_score", "score"))
            details = f"path={sent_found} age_s={int(age)} fresh={fresh} has_score={has_score}"
            results["sentiment_file"] = mark("sentiment_file", fresh and has_score, details, critical=not has_score)
            if not has_score:
                critical_failures.append("sentiment_no_score")
        except Exception as exc:
            results["sentiment_file"] = mark("sentiment_file", False, f"failed to parse {sent_found}: {exc}", critical=True)
            critical_failures.append("sentiment_parse")
    else:
        results["sentiment_file"] = mark("sentiment_file", False, "no sentiment file found under logs/", critical=True)
        critical_failures.append("sentiment_missing")

    # 8) Decision is stale?
    stale_threshold = 300  # seconds
    decision_age = None
    try:
        if api_decision and isinstance(api_decision, dict):
            ts = api_decision.get("timestamp_utc") or api_decision.get("timestamp") or api_decision.get("updated_at")
            if ts:
                t = iso_to_ts(ts)
                if t:
                    decision_age = int(now_ts() - t)
        if decision_age is None and decision_file.exists():
            decision_age = int(now_ts() - decision_file.stat().st_mtime)
    except Exception:
        decision_age = None
    if decision_age is None:
        results["decision_stale"] = mark("decision_stale", False, "could not determine decision age", critical=True)
        critical_failures.append("decision_age_unknown")
    else:
        ok = decision_age <= stale_threshold
        details = f"age_s={decision_age} threshold_s={stale_threshold}"
        results["decision_stale"] = mark("decision_stale", ok, details, critical=(decision_age > 3600))
        if decision_age > 3600:
            critical_failures.append("decision_too_old")

    # 9) Decision: paper_allowed / live_allowed (best-effort)
    live_allowed = False
    paper_allowed = True
    try:
        if api_decision and isinstance(api_decision, dict):
            ui = api_decision.get("cloud_status") or api_decision.get("ui_status") or api_decision.get("provider_health") or {}
            if isinstance(ui, dict):
                mode = ui.get("execution_mode") or ui.get("mode") or ui.get("orders")
                if mode and str(mode).lower() == "live":
                    live_allowed = True
                    paper_allowed = False
                if ui.get("orders") and str(ui.get("orders")).lower() == "locked":
                    live_allowed = False
                    paper_allowed = True
    except Exception:
        pass
    results["decision_allowed"] = mark("decision_allowed", True, f"live_allowed={live_allowed} paper_allowed={paper_allowed}", critical=False)

    # 10) Journal files exist and writable
    journals = [LOGS / "decision_journal.jsonl", LOGS / "paper_signal_outcomes.jsonl", LOGS / "trade_journal.csv"]
    jw = {}
    for j in journals:
        try:
            with open(j, "a"):
                pass
            jw[str(j)] = {"exists": True, "writable": True}
        except Exception as exc:
            jw[str(j)] = {"exists": j.exists(), "writable": False, "error": str(exc)}
    jw_ok = all(x.get("writable") for x in jw.values())
    if not jw_ok:
        critical_failures.append("journals_not_writable")
    results["journals"] = mark("journals", jw_ok, json.dumps(jw), critical=not jw_ok)

    # 11) Deprecated frontend files are not served
    prototype_paths = ["/frontend/index.html", "/command_center_v2/index.html", "/pro_command_center/index.html", "/react_command_center/index.html"]
    served = []
    for p in prototype_paths:
        code, body, _ = http_get("http://127.0.0.1:8770" + p, timeout=3)
        if code == 200:
            served.append((p, code))
    results["deprecated_frontends_served"] = mark("deprecated_frontends_served", len(served) == 0, f"served={served}", critical=len(served) > 0)
    if served:
        critical_failures.append("deprecated_frontend_served")

    # 12) Render-only scripts separation: look for 'render' in scripts/
    render_hits = []
    for p in (ROOT / "scripts").rglob("*.py"):
        try:
            if "render" in p.name.lower():
                render_hits.append(str(p))
            else:
                txt = p.read_text(errors="ignore")
                if "render" in txt.lower():
                    render_hits.append(str(p))
        except Exception:
            continue
    results["render_scripts_in_scripts"] = mark("render_scripts_in_scripts", len(render_hits) == 0, f"hits={render_hits}", critical=False)

    # Persist outputs
    json_out = LOGS / "system_audit.json"
    md_out = LOGS / "system_audit.md"
    summary = {k: v for k, v in results.items()}
    meta = {"generated_at": datetime.now(timezone.utc).isoformat()}
    out = {"meta": meta, "results": summary}
    json_out.write_text(json.dumps(out, indent=2, default=str))

    # Write markdown summary
    lines = ["# Gold-Trader System Audit", "", f"Generated: {meta['generated_at']}", "", "## Summary of checks", ""]
    for k, v in results.items():
        status = "PASS" if v.get("ok") else "FAIL"
        critical = "YES" if v.get("critical") else "NO"
        details = str(v.get("details", "")).replace("\n", " ")
        lines.append(f"- **{k}**: {status} (critical={critical}) — {details}")

    lines.append("")
    if critical_failures:
        lines.append("## CRITICAL ISSUES FOUND")
        for c in critical_failures:
            lines.append(f"- {c}")
    else:
        lines.append("## No critical issues detected")

    md_out.write_text("\n".join(lines))

    # final exit code
    if critical_failures:
        print("CRITICAL failures:", critical_failures)
        return 2
    print("Audit completed; no critical failures.")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)

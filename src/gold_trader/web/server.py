"""HTTP server (stdlib) + JSON API for the gold-trader control panel."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import select
import shlex
import statistics
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .runtime_config import RuntimeConfig, load_runtime_config, save_runtime_config
from ..infra.secrets import resolve_bridge_secret, save_secrets, secrets_status
from ..core.market_intelligence_ux import get_decision_for_api
# Prefer the Render-normalization utilities when available so the web server
# can return the exact local engine decision shape when the cloud bundle is
# authored by the local authoritative engine.
try:
    from .market_intelligence_api import _cloud_bundle, _cloud_sync_meta, _normalize_render_decision  # type: ignore
except Exception:
    _cloud_bundle = None
    _cloud_sync_meta = None
    _normalize_render_decision = None

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGS_DIR = REPO_ROOT / "logs"
DATA_DIR = REPO_ROOT / "data"
JOURNAL_PATH = LOGS_DIR / "trade_journal.csv"
CONFIG_PATH = REPO_ROOT / "config" / "runtime_config.json"
SECRETS_PATH = REPO_ROOT / "config" / "secrets.json"
LIVE_TRACKER_DIR = DATA_DIR / "live_tracker"
LIVE_TRACKER_MIN_REFRESH_SECONDS = 60.0

# Track background jobs (manual agent-cycle / sync-macro triggers) so the UI
# can poll status without re-launching duplicates.
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_live_tracker_lock = threading.Lock()
_live_tracker_last_refresh = 0.0
_live_tracker_last_error: str | None = None


# ---------- Helpers --------------------------------------------------------


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _read_journal() -> list[dict[str, str]]:
    if not JOURNAL_PATH.exists():
        return []
    with JOURNAL_PATH.open("r", newline="") as f:
        return list(csv.DictReader(f))


def _stats(rs: list[float]) -> dict[str, float]:
    if not rs:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gw = sum(wins)
    gl = -sum(losses)
    pf = (gw / gl) if gl > 0 else float("inf")
    return {
        "n": len(rs),
        "wr": len(wins) / len(rs),
        "pf": pf if pf != float("inf") else -1.0,  # JSON-safe sentinel
        "avg_r": statistics.fmean(rs),
        "total_r": sum(rs),
    }


def _paper_state_paths() -> list[tuple[str, Path]]:
    candidates = [
        ("paper", DATA_DIR / "agent_live_xauusd" / "paper_state.json"),
        ("mt5_remote", DATA_DIR / "live_xauusd" / "paper_state.json"),
    ]
    return [(name, p) for name, p in candidates if p.exists()]


def _load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
            lines = data.decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except Exception:
        return []


_LOG_TS_PATTERNS = (
    re.compile(r'"ts"\s*:\s*"([^"]+)"'),
    re.compile(r"\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]"),
    re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
)


def _extract_log_timestamp(line: str, fallback: datetime) -> str:
    for pattern in _LOG_TS_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        raw = match.group(1).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return fallback.astimezone(timezone.utc).isoformat()


def _log_entries(path: Path, n: int, source: str | None = None) -> list[dict[str, str]]:
    fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if path.exists() else datetime.now(timezone.utc)
    entries = []
    for line in _tail(path, n):
        entries.append({
            "timestamp": _extract_log_timestamp(line, fallback),
            "source": source or path.name,
            "line": line,
        })
    return entries


# ---------- Background job runner -----------------------------------------


def _agent_cycle_findings(output: str) -> list[str]:
    wanted_prefixes = (
        "active_families=",
        "starting broker=",
        "broker:",
        "live_account:",
        "equity_guard_trip:",
        "divergence_alert:",
        "paper_equity:",
        "Snapshot ",
        "decision:",
        "entry_candidates:",
        "warnings:",
        "mtf_paper_signal:",
        "macro_paper_signal",
    )
    findings: list[str] = []
    capture_block = 0
    for raw in output.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(prefix) or prefix in stripped for prefix in wanted_prefixes):
            findings.append(line)
            capture_block = 6 if stripped in {"entry_candidates:", "warnings:"} or stripped.startswith(("entry_candidates:", "warnings:")) else 0
            continue
        if capture_block and (line.startswith("    - ") or line.startswith("    ")):
            findings.append(line)
            capture_block -= 1
    return findings[-40:]


def _run_job(job_id: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
        _jobs[job_id]["output"] = ""
        _jobs[job_id]["findings"] = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        started = time.monotonic()
        chunks: list[str] = []
        assert proc.stdout is not None
        while True:
            if time.monotonic() - started > 900:
                proc.kill()
                with _jobs_lock:
                    _jobs[job_id]["status"] = "timeout"
                    _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
                return
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if ready:
                line = proc.stdout.readline()
                if line:
                    chunks.append(line)
                    output = "".join(chunks)[-20_000:]
                    with _jobs_lock:
                        _jobs[job_id]["output"] = output
                        _jobs[job_id]["findings"] = _agent_cycle_findings(output) if _jobs[job_id].get("label") == "agent-cycle" else []
                    continue
            if proc.poll() is not None:
                remainder = proc.stdout.read()
                if remainder:
                    chunks.append(remainder)
                break
        output = "".join(chunks)[-20_000:]
        with _jobs_lock:
            _jobs[job_id]["status"] = "done" if (proc.returncode or 0) == 0 else "failed"
            _jobs[job_id]["exit_code"] = proc.returncode
            _jobs[job_id]["output"] = output
            _jobs[job_id]["findings"] = _agent_cycle_findings(output) if _jobs[job_id].get("label") == "agent-cycle" else []
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


def _start_job(label: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    job_id = f"{label}-{int(time.time() * 1000)}"
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "label": label,
            "cmd": " ".join(shlex.quote(c) for c in cmd),
            "status": "queued",
        }
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    full_env.setdefault("PYTHONPATH", str(REPO_ROOT / "src"))
    threading.Thread(
        target=_run_job, args=(job_id, cmd, cwd, full_env), daemon=True,
    ).start()
    return job_id


# ---------- API endpoints --------------------------------------------------


def _api_summary() -> dict[str, Any]:
    cfg = load_runtime_config(CONFIG_PATH)
    states = []
    for name, path in _paper_state_paths():
        st = _load_state(path)
        if st is None:
            continue
        op = st.get("open_position")
        states.append({
            "broker": name,
            "path": str(path.relative_to(REPO_ROOT)),
            "paper_equity": st.get("paper_equity"),
            "daily_peak_equity": st.get("daily_peak_equity"),
            "total_trades": st.get("total_trades", 0),
            "winning_trades": st.get("winning_trades", 0),
            "win_rate": (
                st.get("winning_trades", 0) / st.get("total_trades", 1)
                if st.get("total_trades", 0) > 0 else 0.0
            ),
            "last_updated": st.get("last_updated"),
            "open_position": op,
            "closed_count": len(st.get("closed_positions", [])),
        })
    rows = _read_journal()
    last_n = rows[-50:] if rows else []
    realised = [_to_float(r.get("realised_r")) for r in last_n]
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "config": cfg.to_dict(),
        "secrets": secrets_status(path=SECRETS_PATH, runtime_bridge_secret=cfg.bridge_secret),
        "states": states,
        "journal": {
            "total": len(rows),
            "last50_stats": _stats(realised),
            "first_opened_at": rows[0].get("opened_at") if rows else None,
            "last_closed_at": rows[-1].get("closed_at") if rows else None,
        },
    }


def _api_journal(query: dict[str, list[str]]) -> dict[str, Any]:
    rows = _read_journal()
    verdict = (query.get("verdict") or [""])[0]
    family = (query.get("family") or [""])[0]
    side = (query.get("side") or [""])[0]
    limit = int((query.get("limit") or ["500"])[0])
    if verdict:
        rows = [r for r in rows if r.get("filter_verdict") == verdict]
    if family:
        rows = [r for r in rows if r.get("family") == family]
    if side:
        rows = [r for r in rows if r.get("side", "").lower().endswith(side.lower())]
    rows = rows[-limit:]
    return {"rows": rows, "count": len(rows)}


def _api_stats() -> dict[str, Any]:
    rows = _read_journal()
    if not rows:
        return {"n": 0}
    realised = [_to_float(r["realised_r"]) for r in rows]
    drift = [_to_float(r["drift_r"]) for r in rows]

    by_verdict: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_verdict[r.get("filter_verdict", "n/a")].append(_to_float(r["realised_r"]))

    verdict_stats = {k: _stats(v) for k, v in by_verdict.items()}

    lift = None
    promote = False
    promote_reason = None
    allow_rs = by_verdict.get("allow", []) + by_verdict.get("allow_with_warning", [])
    block_rs = by_verdict.get("block", [])
    if allow_rs and block_rs:
        s_a = _stats(allow_rs)
        s_b = _stats(block_rs)
        lift = {
            "allow_n": s_a["n"], "allow_avg_r": s_a["avg_r"],
            "block_n": s_b["n"], "block_avg_r": s_b["avg_r"],
            "delta_avg_r": s_a["avg_r"] - s_b["avg_r"],
        }
        if len(rows) >= 30 and (s_a["avg_r"] - s_b["avg_r"]) >= 0.10 \
                and s_a["n"] >= 10 and s_b["n"] >= 10:
            promote = True
            promote_reason = "n>=30, allow-block delta>=0.10R, both buckets >=10"

    by_regime: dict[str, dict[str, dict]] = {}
    for col in (
        "regime_vol_pct", "regime_trend", "regime_compression",
        "regime_macro_real10y", "regime_macro_dxy", "regime_macro_vix",
    ):
        bucket: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            v = r.get(col, "")
            if v:
                bucket[v].append(_to_float(r["realised_r"]))
        by_regime[col] = {k: _stats(v) for k, v in bucket.items() if len(v) >= 3}

    drift_avg = statistics.fmean(drift) if drift else 0.0
    drift_sd = statistics.stdev(drift) if len(drift) >= 2 else 0.0

    return {
        "n": len(rows),
        "overall_realised": _stats(realised),
        "drift_avg_r": drift_avg,
        "drift_stdev": drift_sd,
        "drift_warning": abs(drift_avg) >= 0.05,
        "by_verdict": verdict_stats,
        "filter_lift": lift,
        "promote_filter_to_hard": promote,
        "promote_reason": promote_reason,
        "by_regime": by_regime,
        "exit_reason_mix": dict(Counter(r.get("exit_reason", "unknown") for r in rows)),
    }


def _api_logs(query: dict[str, list[str]]) -> dict[str, Any]:
    n = int((query.get("n") or ["200"])[0])
    name = (query.get("file") or ["agent.log"])[0]
    if "/" in name or ".." in name:  # path-traversal guard
        return {"error": "invalid log name"}
    log_path = LOGS_DIR / name
    entries = _log_entries(log_path, n, name)
    return {
        "file": str(log_path.relative_to(REPO_ROOT)),
        "lines": [entry["line"] for entry in entries],
        "entries": entries,
    }


def _log_category(name: str) -> tuple[str, str, str]:
    lowered = name.lower()
    if lowered in {"live_trade_watch.log", "live_monitor.log"}:
        return ("Live trading", "Watch signals, live tracker notes, manual-entry alerts.", "live")
    if lowered in {"agent.log", "events.jsonl", "gold_trader.jsonl"}:
        return ("Agent decisions", "System decisions, macro filter, risk checks, order state.", "agent")
    if "bridge" in lowered or lowered == "web.log":
        return ("Connection", "MT5 bridge, web server, chart/account connectivity.", "connection")
    if "journal" in lowered or lowered.endswith(".csv"):
        return ("Journal", "Trade ledger, paper/live journal, realised outcomes.", "journal")
    if "audit" in lowered or "holdout" in lowered or "mtf" in lowered or "premium" in lowered or "sync" in lowered or "champion" in lowered:
        return ("Research", "Backtests, validation, sync jobs, champion selection.", "research")
    return ("Other", "Supporting log file.", "other")


def _api_logs_list() -> dict[str, Any]:
    if not LOGS_DIR.exists():
        return {"files": []}
    files = []
    for p in sorted(LOGS_DIR.glob("*")):
        if p.is_file():
            label, description, key = _log_category(p.name)
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
                "category": label,
                "category_key": key,
                "description": description,
            })
    return {"files": files}


def _interesting_log_line(line: str, source: str) -> bool:
    s = line.lower()
    if "brokenpipeerror" in s or "self._send_json" in s or "already journaled" in s or "no-op" in s:
        return False
    if source == "live_trade_watch.log" and (" info 15m " in s or " levels:" in s):
        return True
    if "kill_switch=false" in s or '"kill_switch":false' in s or "kill: ok" in s:
        return "ledger_synced" in s or "entry_candidates" in s
    return any(token in s for token in (
        "alert", "setup", "entry", "entry_candidates", "decision:", "signal",
        "position_open", "pending", "order", "bridge_error", "divergence_alert",
        "ledger_synced", "kill_switch", "trip", "blocked", "failed", "error",
        "allow_with_warning", "macro_", "no open position",
    ))


def _api_live_notes(query: dict[str, list[str]]) -> dict[str, Any]:
    n = int((query.get("n") or ["80"])[0])
    sources = [
        "live_trade_watch.log",
        "agent.log",
        "events.jsonl",
        "gold_trader.jsonl",
    ]
    notes: list[dict[str, str]] = []
    for source in sources:
        path = LOGS_DIR / source
        if not path.exists():
            continue
        entries = _log_entries(path, n, source)
        picked = [entry for entry in entries if _interesting_log_line(entry["line"], source)]
        if source == "live_trade_watch.log" and not picked and entries:
            picked = entries[-1:]
        notes.extend(picked[-10:])
    return {"notes": notes[:40]}


def _api_set_config(body: dict[str, Any]) -> dict[str, Any]:
    cfg = load_runtime_config(CONFIG_PATH)
    if "macro_filter_mode" in body and body["macro_filter_mode"] in ("off", "soft", "hard"):
        cfg.macro_filter_mode = body["macro_filter_mode"]
    if "auto_trade_enabled" in body and isinstance(body["auto_trade_enabled"], bool):
        cfg.auto_trade_enabled = body["auto_trade_enabled"]
    if "news_blackout_min" in body:
        try:
            v = float(body["news_blackout_min"])
            if 0.0 <= v <= 240.0:
                cfg.news_blackout_min = v
        except (TypeError, ValueError):
            pass
    if "bridge_url" in body and isinstance(body["bridge_url"], str):
        v = body["bridge_url"].strip()
        if v.startswith(("http://", "https://")) and len(v) <= 200:
            cfg.bridge_url = v
    if "bridge_secret" in body and isinstance(body["bridge_secret"], str) and body["bridge_secret"].strip():
        cfg.bridge_secret = body["bridge_secret"][:200]
        save_secrets({"bridge_secret": cfg.bridge_secret}, path=SECRETS_PATH)
    if "symbol" in body and isinstance(body["symbol"], str) and body["symbol"].strip():
        cfg.symbol = body["symbol"].strip()[:20]
    if "notes" in body and isinstance(body["notes"], str):
        cfg.notes = body["notes"][:500]
    save_runtime_config(cfg, CONFIG_PATH)
    out = {"ok": True, "config": cfg.to_dict()}
    out["secrets"] = secrets_status(path=SECRETS_PATH, runtime_bridge_secret=cfg.bridge_secret)
    return out


def _api_secrets_get() -> dict[str, Any]:
    cfg = load_runtime_config(CONFIG_PATH)
    return secrets_status(path=SECRETS_PATH, runtime_bridge_secret=cfg.bridge_secret)


def _api_secrets_set(body: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if body.get("clear_openai_api_key"):
        updates["clear_openai_api_key"] = True
    elif isinstance(body.get("openai_api_key"), str) and body["openai_api_key"].strip():
        updates["openai_api_key"] = body["openai_api_key"].strip()
    if body.get("clear_bridge_secret"):
        updates["clear_bridge_secret"] = True
        cfg = load_runtime_config(CONFIG_PATH)
        cfg.bridge_secret = ""
        save_runtime_config(cfg, CONFIG_PATH)
    elif isinstance(body.get("bridge_secret"), str) and body["bridge_secret"].strip():
        updates["bridge_secret"] = body["bridge_secret"].strip()
        cfg = load_runtime_config(CONFIG_PATH)
        cfg.bridge_secret = body["bridge_secret"].strip()[:200]
        save_runtime_config(cfg, CONFIG_PATH)
    if updates:
        save_secrets(updates, path=SECRETS_PATH)
    cfg = load_runtime_config(CONFIG_PATH)
    return {"ok": True, "secrets": secrets_status(path=SECRETS_PATH, runtime_bridge_secret=cfg.bridge_secret)}


def _api_run_cycle() -> dict[str, Any]:
    cmd = ["bash", str(REPO_ROOT / "scripts" / "run_agent_cycle.sh")]
    job_id = _start_job("agent-cycle", cmd, REPO_ROOT)
    return {"ok": True, "job_id": job_id}


def _api_sync_macro(body: dict[str, Any]) -> dict[str, Any]:
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return {"ok": False, "error": ".venv/bin/python not found"}
    today = datetime.now(timezone.utc).date().isoformat()
    start = body.get("start_date") or "2024-12-01"
    end = body.get("end_date") or today
    names = body.get("names") or "bei10,real5y,usdcny,fedfunds,wti,brent,tedspr,gold_lbma"
    cmd = [
        str(venv_py), "-m", "gold_trader.cli", "sync-macro",
        "--start-date", str(start),
        "--end-date", str(end),
        "--names", str(names),
    ]
    job_id = _start_job("sync-macro", cmd, REPO_ROOT)
    return {"ok": True, "job_id": job_id}


def _api_jobs(query: dict[str, list[str]]) -> dict[str, Any]:
    job_id = (query.get("id") or [""])[0]
    with _jobs_lock:
        if job_id:
            return _jobs.get(job_id, {"error": "not found"})
        return {"jobs": list(_jobs.values())[-20:]}


def _calendar_path() -> Path:
    return REPO_ROOT / "data" / "macro" / "news_calendar.csv"


def _api_calendar() -> dict[str, Any]:
    from ..calendar import NewsCalendar
    cal = NewsCalendar.load(_calendar_path())
    now = datetime.now(timezone.utc)
    events = [
        {
            "timestamp": e.timestamp.isoformat(),
            "event": e.event,
            "impact": e.impact,
            "minutes_until": (e.timestamp - now).total_seconds() / 60.0,
        }
        for e in cal.events
    ]
    upcoming = [e for e in events if e["minutes_until"] >= -60]
    return {
        "total": len(events),
        "upcoming": upcoming[:25],
        "path": str(_calendar_path().relative_to(REPO_ROOT)),
    }


def _api_calendar_add(body: dict[str, Any]) -> dict[str, Any]:
    from ..calendar import NewsCalendar, NewsEvent, _parse_iso
    ts_raw = (body.get("timestamp") or "").strip()
    event = (body.get("event") or "").strip()[:80]
    impact = (body.get("impact") or "high").strip().lower()
    if impact not in ("low", "medium", "high"):
        impact = "high"
    if not ts_raw or not event:
        return {"ok": False, "error": "timestamp and event required"}
    try:
        ts = _parse_iso(ts_raw)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid timestamp: {exc}"}
    cal = NewsCalendar.load(_calendar_path())
    cal.add(NewsEvent(timestamp=ts, event=event, impact=impact))
    cal.save(_calendar_path())
    return {"ok": True, "total": len(cal.events)}


def _api_calendar_delete(body: dict[str, Any]) -> dict[str, Any]:
    from ..calendar import NewsCalendar, _parse_iso
    ts_raw = (body.get("timestamp") or "").strip()
    if not ts_raw:
        return {"ok": False, "error": "timestamp required"}
    try:
        ts = _parse_iso(ts_raw)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid timestamp: {exc}"}
    cal = NewsCalendar.load(_calendar_path())
    before = len(cal.events)
    cal.events = [e for e in cal.events if e.timestamp != ts]
    cal._ts_index = [e.timestamp for e in cal.events]
    cal.save(_calendar_path())
    return {"ok": True, "removed": before - len(cal.events)}


# ---------- Datasets / Candles --------------------------------------------


_FAMILIES = [
    "liquidity_sweep", "compression_breakout", "asian_range_breakout",
    "london_breakout", "trend_pullback", "ny_session_breakout", "momentum_burst",
    "previous_day_breakout", "opening_range_breakout", "asian_range_fade",
    "fair_value_gap", "inversion_fair_value_gap", "rsi_divergence",
    "ny_close_compression", "session_continuation",
    "dxy_lead_lag", "real_yield_reversal",
]


def _safe_repo_path(rel: str) -> Path | None:
    """Return absolute path under REPO_ROOT or None if traversal."""
    try:
        target = (REPO_ROOT / rel).resolve()
    except Exception:
        return None
    if not str(target).startswith(str(REPO_ROOT.resolve())):
        return None
    return target


def _api_datasets() -> dict[str, Any]:
    out = []
    for csv_path in sorted(DATA_DIR.rglob("xauusd_*.csv")):
        try:
            rel = csv_path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        st = csv_path.stat()
        out.append({
            "path": str(rel),
            "name": csv_path.name,
            "folder": str(csv_path.parent.relative_to(REPO_ROOT)),
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    return {"datasets": out}


def _read_ohlc(path: Path, limit: int = 1500, since_iso: str = "") -> list[dict[str, Any]]:
    """Return rows in lightweight-charts format: time(unix-sec), open/high/low/close + volume."""
    if not path.exists() or path.suffix != ".csv":
        return []
    rows: list[dict[str, Any]] = []
    cutoff = None
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except ValueError:
            cutoff = None
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts_raw = r.get("timestamp") or r.get("date") or ""
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if cutoff and ts < cutoff:
                continue
            try:
                rows.append({
                    "time": int(ts.timestamp()),
                    "iso": ts.isoformat(),
                    "open": float(r.get("open", 0) or 0),
                    "high": float(r.get("high", 0) or 0),
                    "low": float(r.get("low", 0) or 0),
                    "close": float(r.get("close", 0) or 0),
                    "volume": float(r.get("volume", 0) or 0),
                    "spread": float(r.get("spread", 0) or 0) if r.get("spread") else 0.0,
                    "session": r.get("session", ""),
                })
            except (ValueError, TypeError):
                continue
    if limit > 0 and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def _refresh_live_tracker_cache(*, force: bool = False) -> dict[str, Any]:
    """Refresh a dedicated live CSV cache for chart fallback/tracking.

    The bridge is the preferred live source.  When bridge candles are missing
    or broken, this cache gives the UI a deterministic, freshly-downloaded
    chart instead of whichever old CSV happens to have the newest mtime.
    """
    global _live_tracker_last_refresh, _live_tracker_last_error
    now = time.time()
    with _live_tracker_lock:
        age = now - _live_tracker_last_refresh
        if not force and _live_tracker_last_refresh > 0 and age < LIVE_TRACKER_MIN_REFRESH_SECONDS:
            return {
                "refreshed": False,
                "age_sec": age,
                "error": _live_tracker_last_error,
                "dir": str(LIVE_TRACKER_DIR.relative_to(REPO_ROOT)),
            }
        LIVE_TRACKER_DIR.mkdir(parents=True, exist_ok=True)
        venv_py = REPO_ROOT / ".venv" / "bin" / "python"
        cmd = [
            str(venv_py), "-m", "gold_trader.cli", "sync-dukascopy",
            "--symbol", "XAUUSD",
            "--days", "4",
            "--base-interval-minutes", "1",
            "--timeframes", "1,5,15,60,240",
            "--max-workers", "4",
            "--output-dir", str(LIVE_TRACKER_DIR),
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(REPO_ROOT / "src"))
        try:
            proc = subprocess.run(
                cmd, cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=180,
            )
            _live_tracker_last_refresh = time.time()
            _live_tracker_last_error = None if proc.returncode == 0 else proc.stdout[-1000:]
            return {
                "refreshed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output_tail": proc.stdout[-1000:],
                "error": _live_tracker_last_error,
                "dir": str(LIVE_TRACKER_DIR.relative_to(REPO_ROOT)),
            }
        except Exception as exc:  # noqa: BLE001
            _live_tracker_last_refresh = time.time()
            _live_tracker_last_error = str(exc)
            return {
                "refreshed": False,
                "error": _live_tracker_last_error,
                "dir": str(LIVE_TRACKER_DIR.relative_to(REPO_ROOT)),
            }


def _api_candles(query: dict[str, list[str]]) -> dict[str, Any]:
    rel = (query.get("path") or [""])[0]
    if not rel:
        return {"error": "path required"}
    p = _safe_repo_path(rel)
    if p is None or not p.exists():
        return {"error": f"not found: {rel}"}
    limit = max(0, int((query.get("limit") or ["1500"])[0]))
    since = (query.get("since") or [""])[0]
    bars = _read_ohlc(p, limit=limit, since_iso=since)
    return {
        "path": rel,
        "count": len(bars),
        "first": bars[0]["iso"] if bars else None,
        "last": bars[-1]["iso"] if bars else None,
        "bars": bars,
    }


# ---------- Indicators (server-side, optional overlays) -------------------


def _ema(values: list[float], period: int) -> list[float | None]:
    if not values or period <= 1:
        return [v for v in values]
    out: list[float | None] = []
    k = 2.0 / (period + 1)
    ema_val: float | None = None
    for i, v in enumerate(values):
        if i + 1 < period:
            out.append(None)
            continue
        if ema_val is None:
            ema_val = sum(values[i + 1 - period:i + 1]) / period
        else:
            ema_val = v * k + ema_val * (1 - k)
        out.append(ema_val)
    return out


def _api_indicators(query: dict[str, list[str]]) -> dict[str, Any]:
    rel = (query.get("path") or [""])[0]
    p = _safe_repo_path(rel) if rel else None
    if p is None or not p.exists():
        return {"error": "path required"}
    bars = _read_ohlc(p, limit=int((query.get("limit") or ["1500"])[0]))
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    times = [b["time"] for b in bars]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)

    # Session VWAP (UTC-day anchored, typical price * volume)
    vwap: list[float | None] = []
    cur_day = None
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        d = datetime.fromtimestamp(b["time"], timezone.utc).date()
        if d != cur_day:
            cur_day = d
            cum_pv = 0.0
            cum_v = 0.0
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b["volume"] if b["volume"] > 0 else 1.0
        cum_pv += tp * v
        cum_v += v
        vwap.append(cum_pv / cum_v if cum_v > 0 else None)

    def _line(name: str, vals: list[float | None]) -> list[dict[str, Any]]:
        return [
            {"time": times[i], "value": v}
            for i, v in enumerate(vals) if v is not None
        ]

    return {
        "ema20": _line("ema20", ema20),
        "ema50": _line("ema50", ema50),
        "ema200": _line("ema200", ema200),
        "vwap": _line("vwap", vwap),
    }


# ---------- Macro series API ----------------------------------------------


def _api_macro_list() -> dict[str, Any]:
    macro_dir = DATA_DIR / "macro"
    if not macro_dir.exists():
        return {"series": []}
    out = []
    for p in sorted(macro_dir.glob("*.csv")):
        if p.name == "news_calendar.csv":
            continue
        st = p.stat()
        # peek first/last
        first, last, count = None, None, 0
        try:
            with p.open("r") as f:
                rows = list(csv.DictReader(f))
                count = len(rows)
                if rows:
                    first = rows[0].get("date")
                    last = rows[-1].get("date")
        except Exception:
            pass
        out.append({
            "name": p.stem,
            "path": str(p.relative_to(REPO_ROOT)),
            "size": st.st_size,
            "rows": count,
            "first": first,
            "last": last,
        })
    return {"series": out}


def _api_macro_series(query: dict[str, list[str]]) -> dict[str, Any]:
    name = (query.get("name") or [""])[0]
    if not name or "/" in name or ".." in name:
        return {"error": "invalid name"}
    p = DATA_DIR / "macro" / f"{name}.csv"
    if not p.exists():
        return {"error": f"not found: {name}"}
    rows: list[dict[str, Any]] = []
    with p.open("r") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "date": r["date"],
                    "value": float(r["value"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
    return {"name": name, "count": len(rows), "rows": rows}


# ---------- Bridge proxy --------------------------------------------------


def _bridge_client():
    from ..live.mt5_bridge_client import MT5RemoteBroker
    cfg = load_runtime_config(CONFIG_PATH)
    base_url = os.environ.get("GOLD_BRIDGE_URL") or cfg.bridge_url or "http://127.0.0.1:8765"
    secret = resolve_bridge_secret(path=SECRETS_PATH, runtime_fallback=cfg.bridge_secret or "")
    return MT5RemoteBroker(base_url=base_url, shared_secret=secret, timeout=4.0)


def _bridge_symbol() -> str:
    cfg = load_runtime_config(CONFIG_PATH)
    return os.environ.get("GOLD_SYMBOL") or cfg.symbol or "XAUUSD"


def _api_bridge_status() -> dict[str, Any]:
    cfg = load_runtime_config(CONFIG_PATH)
    base_url = os.environ.get("GOLD_BRIDGE_URL") or cfg.bridge_url
    out: dict[str, Any] = {
        "url": base_url,
        "symbol": _bridge_symbol(),
        "online": False,
        "healthz": None,
        "account": None,
        "open_position": None,
        "error": None,
    }
    try:
        client = _bridge_client()
        out["healthz"] = client.healthz()
        out["online"] = True
        try:
            acc = client.get_account_info()
            out["account"] = {
                "equity": acc.equity, "balance": acc.balance,
                "currency": acc.currency, "margin_used": acc.margin_used,
                "margin_free": acc.margin_free, "leverage": acc.leverage,
            }
        except Exception as exc:  # noqa: BLE001
            out["account_error"] = str(exc)
        try:
            pos = client.get_open_position()
            if pos is not None:
                out["open_position"] = {
                    "broker_order_id": pos.broker_order_id,
                    "symbol": pos.symbol,
                    "side": pos.side.value,
                    "units": pos.units,
                    "entry_price": pos.entry_price,
                    "stop_price": pos.stop_price,
                    "target_price": pos.target_price,
                    "opened_at": pos.opened_at.isoformat(),
                    "unrealised_pnl": pos.unrealised_pnl,
                    "magic": pos.magic,
                }
        except Exception as exc:  # noqa: BLE001
            out["position_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    out["connection_state"] = "online" if out.get("online") else "offline"
    out["next_action"] = (
        "Ready — you can approve live trades."
        if out.get("online")
        else "Run ./start from the project folder (starts MT5 + bridge + UI)."
    )
    return out


def _ensure_mt5_terminal_running() -> str | None:
    """Start MT5 under Wine if not already running. Returns error message or None."""
    try:
        check = subprocess.run(
            ["pgrep", "-f", "terminal64.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check.returncode == 0:
            return None
    except Exception:  # noqa: BLE001
        pass

    prefix = Path(os.environ.get("GOLD_MT5_PREFIX", Path.home() / ".gold-mt5-wine"))
    mt5_exe = prefix / "drive_c/Program Files/MetaTrader 5/terminal64.exe"
    if not mt5_exe.exists():
        return f"MT5 not found at {mt5_exe} — run scripts/setup_wine_mt5.sh first."

    env = os.environ.copy()
    env.setdefault("WINEPREFIX", str(prefix))
    env.setdefault("WINEARCH", "win64")
    env.setdefault("WINEDEBUG", "-all")
    log_path = LOGS_DIR / "mt5_terminal.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash", "-lc",
        f"nohup wine {shlex.quote(str(mt5_exe))} >> {shlex.quote(str(log_path))} 2>&1 & echo $!",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return (proc.stdout or "MT5 launch failed")[-500:]
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def _api_bridge_start() -> dict[str, Any]:
    """Attempt to launch MT5 (Wine) and the bridge in the background."""
    status = _api_bridge_status()
    if status.get("online"):
        return {"ok": True, "already_online": True, "message": "Bridge is already online."}

    mt5_err = _ensure_mt5_terminal_running()
    if mt5_err:
        return {"ok": False, "error": mt5_err}

    script = Path.home() / ".gold-mt5-wine" / "start-bridge.sh"
    if not script.exists():
        return {
            "ok": False,
            "error": f"Bridge launcher not found at {script}. Run setup_wine_mt5.sh first.",
        }

    cred = Path.home() / ".gold-mt5-wine" / "credentials.env"
    cfg = load_runtime_config(CONFIG_PATH)
    secret = resolve_bridge_secret(path=SECRETS_PATH, runtime_fallback=cfg.bridge_secret or "")
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT / "src"))
    if secret:
        env["GOLD_BRIDGE_SECRET"] = secret
    env.setdefault("GOLD_BRIDGE_URL", cfg.bridge_url or "http://127.0.0.1:8765")
    env.setdefault("GOLD_SYMBOL", cfg.symbol or "GOLD")

    if cred.exists():
        for line in cred.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                env[key] = val

    log_path = LOGS_DIR / "bridge_launch.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # Wine Python needs a pseudo-TTY — nohup detached stdio causes WinError 6.
    inner = f"bash {shlex.quote(str(script))}"
    cmd = [
        "bash", "-lc",
        f"script -qefc {shlex.quote(inner)} {shlex.quote(str(log_path))} & echo $!",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        pid = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stdout or "bridge launch failed")[-500:]}
        return {
            "ok": True,
            "message": "MT5 + bridge starting — live in ~15s if MT5 is logged in.",
            "pid": pid,
            "log": str(log_path.relative_to(REPO_ROOT)),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _annotate_candle_freshness(out: dict[str, Any]) -> None:
    bars = out.get("bars") or []
    if not bars:
        return
    last = bars[-1]
    last_ts = datetime.fromtimestamp(int(last["time"]), timezone.utc)
    out["last_bar"] = last
    out["last_bar_iso"] = last_ts.isoformat()
    out["age_sec"] = (datetime.now(timezone.utc) - last_ts).total_seconds()


def _csv_candle_payload(path: Path, count: int) -> list[dict[str, Any]]:
    bars = _read_ohlc(path, limit=count)
    return [
        {
            "time": b["time"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b["volume"],
            "spread": b["spread"],
        }
        for b in bars
    ]


def _api_live_candles(query: dict[str, list[str]]) -> dict[str, Any]:
    """Pull live candles from MT5 bridge with CSV fallback when bridge is offline."""
    tf = int((query.get("timeframe") or ["15"])[0])
    count = max(1, min(int((query.get("count") or ["500"])[0]), 5000))
    prefer_cache = (query.get("prefer_cache") or [""])[0] in ("1", "true", "yes")
    sync = (query.get("sync") or [""])[0] in ("1", "true", "yes")
    symbol = (query.get("symbol") or [""])[0] or _bridge_symbol()
    fallback = _find_latest_csv_for_tf(tf)
    out: dict[str, Any] = {
        "source": "bridge",
        "symbol": symbol,
        "timeframe": tf,
        "online": False,
        "bars": [],
        "count": 0,
    }

    def _apply_csv_fallback() -> bool:
        if not fallback:
            return False
        out["source"] = "csv_fallback"
        out["fallback_path"] = str(fallback.relative_to(REPO_ROOT))
        out["bars"] = _csv_candle_payload(fallback, count)
        out["count"] = len(out["bars"])
        return True

    # Preview/offline only — skip bridge when the UI explicitly asks for cache.
    if prefer_cache and _apply_csv_fallback():
        _annotate_candle_freshness(out)
        return out

    try:
        client = _bridge_client()
        bars = client.get_candles(symbol=symbol, timeframe_minutes=tf, count=count)
        if bars:
            out["online"] = True
            out["source"] = "bridge"
            out["bars"] = [
                {
                    "time": b["time"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b.get("tick_volume", 0.0),
                    "spread": b.get("spread", 0.0),
                }
                for b in bars
            ]
            out["count"] = len(out["bars"])
            _annotate_candle_freshness(out)
            return out
        out["error"] = "bridge returned no bars"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)

    if sync:
        out["tracker_refresh"] = _refresh_live_tracker_cache(force=True)
        fallback = _find_latest_csv_for_tf(tf)

    if not _apply_csv_fallback():
        out["bars"] = []
        out["count"] = 0
    _annotate_candle_freshness(out)
    return out


def _tracker_level_state(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {}
    from datetime import datetime as _dt
    last = bars[-1]
    last_ts = _dt.fromtimestamp(int(last["time"]), timezone.utc)
    day = last_ts.date()
    day_bars = [b for b in bars if _dt.fromtimestamp(int(b["time"]), timezone.utc).date() == day]
    scope = day_bars or bars[-96:]
    session_high = max(b["high"] for b in scope)
    session_low = min(b["low"] for b in scope)
    midpoint = (session_high + session_low) / 2.0
    prior = [b for b in bars if _dt.fromtimestamp(int(b["time"]), timezone.utc).date() < day]
    prior_close = prior[-1]["close"] if prior else None
    return {
        "session_high": session_high,
        "session_low": session_low,
        "midpoint": midpoint,
        "prior_close": prior_close,
        "distance_from_high": last["close"] - session_high,
        "distance_from_low": last["close"] - session_low,
        "position_in_range": (
            (last["close"] - session_low) / (session_high - session_low)
            if session_high > session_low else 0.5
        ),
        "short_rejection_zone": [midpoint, session_high],
        "short_breakdown": session_low,
        "long_reclaim": session_high,
    }


def _api_live_tracker(query: dict[str, list[str]]) -> dict[str, Any]:
    """Single live-trading snapshot for the UI and operator.

    Includes candles, data-source/freshness, macro filter verdicts, current
    session levels, and the latest watcher alerts/log lines.
    """
    tf = int((query.get("timeframe") or ["15"])[0])
    count = max(50, min(int((query.get("count") or ["600"])[0]), 2000))
    candles = _api_live_candles({"timeframe": [str(tf)], "count": [str(count)]})
    bars = candles.get("bars") or []
    last = bars[-1] if bars else None
    now = datetime.now(timezone.utc)
    age_sec = None
    macro: dict[str, Any] = {}
    if last:
        last_ts = datetime.fromtimestamp(int(last["time"]), timezone.utc)
        age_sec = (now - last_ts).total_seconds()
        try:
            from ..data.macro import load_macro_frame
            from ..macro_filter import MacroDecisionFilter
            from ..models import Side
            mf = MacroDecisionFilter(macro=load_macro_frame(DATA_DIR / "macro"))
            for side in (Side.LONG, Side.SHORT):
                verdict = mf.evaluate(side, last_ts)
                macro[side.name.lower()] = {
                    "verdict": verdict.verdict,
                    "reason": verdict.reason,
                    "tags": verdict.regime_tags,
                }
        except Exception as exc:  # noqa: BLE001
            macro["error"] = str(exc)
    return {
        "now": now.isoformat(),
        "symbol": candles.get("symbol"),
        "timeframe": tf,
        "source": candles.get("source"),
        "online": candles.get("online", False),
        "error": candles.get("error"),
        "tracker_refresh": candles.get("tracker_refresh"),
        "count": len(bars),
        "last_bar": last,
        "age_sec": age_sec,
        "levels": _tracker_level_state(bars),
        "macro": macro,
        "watch_log": _tail(LOGS_DIR / "live_trade_watch.log", 20),
        "bars": bars,
    }


def _api_live_scout(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return automatic IFVG scout state for the Trade UI (always-on AI watch)."""
    from ..assistants.ifvg_scout import (
        DEFAULT_STATE_PATH,
        load_scout_state,
        read_scout_timeframe,
        run_scout_scan,
        scout_public_view,
        write_scout_timeframe,
    )

    tf = int((query.get("timeframe") or ["15"])[0])
    if tf != read_scout_timeframe():
        write_scout_timeframe(tf)

    state = load_scout_state(DEFAULT_STATE_PATH)
    stale = False
    tf_mismatch = int(state.get("primary_timeframe") or 0) not in (0, tf)
    last = state.get("last_scan_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - last_dt).total_seconds() > 120
        except Exception:
            stale = True
    else:
        stale = True
    if stale or tf_mismatch:
        try:
            state = run_scout_scan(primary_tf=tf, force_research=False)
        except Exception as exc:  # noqa: BLE001
            return {
                **scout_public_view(state, timeframe=tf),
                "error": str(exc),
                "stale": True,
            }
    view = scout_public_view(state, timeframe=tf)
    candles = _api_live_candles({"timeframe": [str(tf)], "count": ["50"]})
    view["online"] = candles.get("online", False)
    view["source"] = candles.get("source")
    view["age_sec"] = candles.get("age_sec")
    view["timeframe"] = tf
    return view


def _api_live_ifvg_checklist(query: dict[str, list[str]]) -> dict[str, Any]:
    tf = int((query.get("timeframe") or ["15"])[0])
    count = max(80, min(int((query.get("count") or ["800"])[0]), 3000))
    prefer_cache = (query.get("prefer_cache") or [""])[0] in ("1", "true", "yes")
    candles = _api_live_candles({
        "timeframe": [str(tf)],
        "count": [str(count)],
        "prefer_cache": ["1"] if prefer_cache else [""],
    })
    rows = candles.get("bars") or []
    bars = _bars_from_ohlc_rows(rows)
    if not bars:
        return {
            "source": candles.get("source", "unknown"),
            "online": candles.get("online", False),
            "timeframe": tf,
            "setups": [],
            "manual_approval_required": True,
            "message": "no bars available",
        }
    try:
        from ..assistants.ifvg_confluence import (
            find_ifvg_setups,
            load_market_levels,
            setup_to_dict,
        )
        from ..calendar import NewsCalendar
        from ..data.macro import load_macro_frame

        macro_frame = load_macro_frame(DATA_DIR / "macro")
        if not macro_frame.names():
            macro_frame = None
        levels = load_market_levels(REPO_ROOT / "config" / "market_levels.json")
        calendar = NewsCalendar.load(DATA_DIR / "macro" / "news_calendar.csv")
        setups = find_ifvg_setups(
            bars,
            macro_frame=macro_frame,
            market_levels=levels,
            news_calendar=calendar,
            openai_config_path=REPO_ROOT / "config" / "openai_research.json",
            openai_cache_path=DATA_DIR / "cache" / "openai_market_research.json",
            force_external_research=(query.get("refresh_research") or [""])[0] in ("1", "true", "yes"),
        )
        return {
            "source": candles.get("source", "unknown"),
            "online": candles.get("online", False),
            "timeframe": tf,
            "bar_count": len(bars),
            "age_sec": candles.get("age_sec"),
            "manual_approval_required": True,
            "setups": [setup_to_dict(setup, timeframe_minutes=tf) for setup in setups[:5]],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "source": candles.get("source", "unknown"),
            "online": candles.get("online", False),
            "timeframe": tf,
            "setups": [],
            "error": str(exc),
        }


def _find_latest_csv_for_tf(tf_minutes: int) -> Path | None:
    """Find the most recently modified CSV in data/ matching the timeframe suffix."""
    suffix = f"_{tf_minutes}m.csv"
    matches: list[tuple[float, Path]] = []
    for p in DATA_DIR.rglob(f"*{suffix}"):
        if p.is_file():
            matches.append((p.stat().st_mtime, p))
    if not matches:
        # Common fallback path
        for name in (f"data/xauusd_full_{tf_minutes}m.csv",):
            p = REPO_ROOT / name
            if p.exists():
                return p
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _bars_from_ohlc_rows(rows: list[dict[str, Any]]):
    """Convert _read_ohlc / live-candle rows into MarketBar objects for zones."""
    from ..models import MarketBar
    out = []
    for r in rows:
        ts = r.get("iso") or r.get("time")
        if isinstance(ts, str):
            from datetime import datetime as _dt
            try:
                t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
        else:
            from datetime import datetime as _dt, timezone as _tz
            try:
                t = _dt.fromtimestamp(int(ts), tz=_tz.utc)
            except Exception:
                continue
        try:
            out.append(MarketBar(
                timestamp=t,
                open=float(r.get("open", 0) or 0),
                high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0),
                close=float(r.get("close", 0) or 0),
                volume=float(r.get("volume", 0) or 0),
                spread=float(r.get("spread", 0) or 0),
                session=r.get("session", "") or "unknown",
            ))
        except (TypeError, ValueError):
            continue
    return out


def _api_live_zones(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return strategy zones (FVG / IFVG / swings / prev-day / asian) on the
    requested timeframe. Uses the same bridge-or-CSV-fallback as live candles
    so zones always render even when the bridge is offline.
    """
    from ..zones import all_zones
    tf = int((query.get("timeframe") or ["15"])[0])
    count = max(50, min(int((query.get("count") or ["500"])[0]), 5000))
    families_csv = (query.get("families") or [""])[0]
    families = [f.strip() for f in families_csv.split(",") if f.strip()] or None
    lookback = max(20, min(int((query.get("lookback") or ["200"])[0]), 2000))
    prefer_cache = (query.get("prefer_cache") or [""])[0] in ("1", "true", "yes")

    candles = _api_live_candles({
        "timeframe": [str(tf)],
        "count": [str(count)],
        "prefer_cache": ["1"] if prefer_cache else [""],
    })
    rows = candles.get("bars") or []
    bars = _bars_from_ohlc_rows(rows)
    zones = all_zones(bars, families=families, lookback=lookback)
    return {
        "source": candles.get("source", "unknown"),
        "online": candles.get("online", False),
        "timeframe": tf,
        "count": len(zones),
        "bar_count": len(bars),
        "zones": [z.to_dict() for z in zones],
    }


def _api_live_confluence(query: dict[str, list[str]]) -> dict[str, Any]:
    """Multi-timeframe confluence: clusters zones across requested TFs into
    confluence points. Used by the Live tab as a heatmap of "where multiple
    strategies / TFs agree on a price area"."""
    from ..confluence import score_confluence
    from ..zones import all_zones

    tfs = (query.get("timeframes") or ["15,60,240"])[0]
    tf_list = []
    for s in tfs.split(","):
        s = s.strip()
        if s.isdigit():
            tf_list.append(int(s))
    if not tf_list:
        tf_list = [15, 60, 240]
    count = max(50, min(int((query.get("count") or ["500"])[0]), 5000))
    tolerance = float((query.get("tolerance") or ["0.50"])[0])
    min_contrib = max(2, int((query.get("min_contributors") or ["2"])[0]))

    zones_by_tf: dict[int, list] = {}
    sources: dict[int, str] = {}
    for tf in tf_list:
        candles = _api_live_candles({"timeframe": [str(tf)], "count": [str(count)]})
        rows = candles.get("bars") or []
        bars = _bars_from_ohlc_rows(rows)
        zones_by_tf[tf] = all_zones(bars)
        sources[tf] = candles.get("source", "unknown")

    points = score_confluence(
        zones_by_tf,
        tolerance=tolerance,
        min_contributors=min_contrib,
    )
    return {
        "timeframes": tf_list,
        "tolerance": tolerance,
        "count": len(points),
        "sources": sources,
        "points": [p.to_dict() for p in points],
    }


def _api_bridge_close(body: dict[str, Any]) -> dict[str, Any]:
    bid = (body.get("broker_order_id") or "").strip()
    if not bid:
        return {"ok": False, "error": "broker_order_id required"}
    reason = (body.get("reason") or "manual_ui").strip()[:50]
    try:
        client = _bridge_client()
        closed = client.close_position(bid, reason=reason)
        if closed is None:
            return {"ok": False, "error": "no position closed"}
        return {
            "ok": True,
            "closed": {
                "broker_order_id": closed.broker_order_id,
                "exit_price": closed.exit_price,
                "pnl_dollars": closed.pnl_dollars,
                "exit_reason": closed.exit_reason,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _api_ifvg_approve(body: dict[str, Any]) -> dict[str, Any]:
    """Place a live IFVG trade after manual operator approval."""
    from ..live.broker import OrderRequest, OrderSide

    side_raw = str(body.get("side") or "").strip().lower()
    if side_raw in {"long", "buy", "bull", "bullish"}:
        side = OrderSide.BUY
    elif side_raw in {"short", "sell", "bear", "bearish"}:
        side = OrderSide.SELL
    else:
        return {"ok": False, "error": "side must be long or short"}

    try:
        stop = float(body.get("stop"))
        target = float(body.get("target") or body.get("tp1"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "stop and target (tp1) are required numbers"}

    entry = body.get("entry")
    try:
        entry_price = float(entry) if entry not in (None, "") else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "entry must be a number when provided"}

    try:
        risk_pct = float(body.get("risk_pct") or 0.01)
        risk_pct = max(0.001, min(risk_pct, 0.05))
    except (TypeError, ValueError):
        risk_pct = 0.01

    verdict = str(body.get("verdict") or "").strip().lower()
    if verdict and verdict not in {"valid_entry", "alert_wait"}:
        return {"ok": False, "error": f"verdict {verdict!r} is not approvable"}

    if body.get("externally_blocked"):
        from ..research.realtime_research import load_openai_research_config

        research_cfg = load_openai_research_config(REPO_ROOT / "config" / "openai_research.json")
        if research_cfg.mode == "hard":
            return {"ok": False, "error": "setup is externally blocked (hard mode)"}

    cfg = load_runtime_config(CONFIG_PATH)
    symbol = str(body.get("symbol") or cfg.symbol or _bridge_symbol()).strip()[:20]
    if not symbol:
        return {"ok": False, "error": "symbol required"}

    try:
        client = _bridge_client()
        client.healthz()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"MT5 bridge offline: {exc}"}

    try:
        existing = client.get_open_position()
        if existing is not None:
            return {
                "ok": False,
                "error": f"already in {existing.side.value} position (ticket {existing.broker_order_id})",
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not check open position: {exc}"}

    try:
        acct = client.get_account_info()
        risk_dollars = max(1.0, float(acct.equity) * risk_pct)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not read account equity: {exc}"}

    comment = str(body.get("comment") or "ifvg_manual_ui")[:31]
    req = OrderRequest(
        symbol=symbol,
        side=side,
        risk_dollars=risk_dollars,
        stop_price=stop,
        target_price=target,
        entry_price=entry_price,
        comment=comment,
    )
    try:
        result = client.place_market_order(req)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if not result.accepted:
        return {"ok": False, "error": result.error or "order rejected by broker"}

    return {
        "ok": True,
        "order": {
            "broker_order_id": result.broker_order_id,
            "fill_price": result.fill_price,
            "units": result.units,
            "side": side.value,
            "symbol": symbol,
            "stop": stop,
            "target": target,
            "entry": entry_price or result.fill_price,
            "risk_dollars": risk_dollars,
        },
    }




def _api_families() -> dict[str, Any]:
    return {"families": _FAMILIES}


def _api_lab_holdout(body: dict[str, Any]) -> dict[str, Any]:
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return {"ok": False, "error": ".venv/bin/python not found"}
    rel = (body.get("path") or "").strip()
    p = _safe_repo_path(rel) if rel else None
    if p is None or not p.exists():
        return {"ok": False, "error": f"invalid path: {rel}"}
    family = (body.get("family") or "asian_range_breakout").strip()
    if family not in _FAMILIES:
        return {"ok": False, "error": f"unknown family: {family}"}
    n_perm = int(body.get("n_permutations") or 3000)
    n_perm = max(100, min(n_perm, 20000))
    holdout_frac = float(body.get("holdout_fraction") or (1.0 / 3.0))
    cmd = [
        str(venv_py), "-m", "gold_trader.cli", "holdout-eval", str(p),
        "--family", family,
        "--n-permutations", str(n_perm),
        "--holdout-fraction", str(holdout_frac),
    ]
    job_id = _start_job(f"holdout-{family}", cmd, REPO_ROOT)
    return {"ok": True, "job_id": job_id}


def _api_lab_permutation(body: dict[str, Any]) -> dict[str, Any]:
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return {"ok": False, "error": ".venv/bin/python not found"}
    rel = (body.get("path") or "").strip()
    p = _safe_repo_path(rel) if rel else None
    if p is None or not p.exists():
        return {"ok": False, "error": f"invalid path: {rel}"}
    family = (body.get("family") or "asian_range_breakout").strip()
    if family not in _FAMILIES:
        return {"ok": False, "error": f"unknown family: {family}"}
    n_perm = int(body.get("n_permutations") or 3000)
    n_perm = max(100, min(n_perm, 20000))
    cmd = [
        str(venv_py), "-m", "gold_trader.cli", "permutation-test", str(p),
        "--family", family,
        "--n-permutations", str(n_perm),
    ]
    job_id = _start_job(f"perm-{family}", cmd, REPO_ROOT)
    return {"ok": True, "job_id": job_id}


def _api_miner_run(body: dict[str, Any]) -> dict[str, Any]:
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return {"ok": False, "error": ".venv/bin/python not found"}
    rel = (body.get("path") or "data/xauusd_full_15m.csv").strip()
    p = _safe_repo_path(rel) if rel else None
    if p is None or not p.exists():
        return {"ok": False, "error": f"invalid path: {rel}"}
    timeframes = (body.get("timeframes") or "60,240").strip()
    horizons = (body.get("horizons") or "8,16").strip()
    out_dir_rel = (body.get("output_dir") or f"reports/mined_patterns/ui_{int(time.time())}").strip()
    out_dir = _safe_repo_path(out_dir_rel)
    if out_dir is None:
        return {"ok": False, "error": "invalid output_dir"}
    cmd = [
        str(venv_py), "-m", "gold_trader.cli", "mine-all", str(p),
        "--timeframes", timeframes,
        "--horizons", horizons,
        "--max-combo-size", str(int(body.get("max_combo_size") or 2)),
        "--min-signals", str(int(body.get("min_signals") or 50)),
        "--min-effect-r", str(float(body.get("min_effect_r") or 0.10)),
        "--fdr-q", str(float(body.get("fdr_q") or 0.10)),
        "--bootstrap-blocks", str(int(body.get("bootstrap_blocks") or 300)),
        "--output-dir", str(out_dir),
    ]
    if bool(body.get("with_macro")):
        cmd.append("--with-macro")
    job_id = _start_job("mine-all", cmd, REPO_ROOT)
    return {"ok": True, "job_id": job_id, "output_dir": out_dir_rel}


def _api_miner_results() -> dict[str, Any]:
    base = REPO_ROOT / "reports" / "mined_patterns"
    out = []
    if not base.exists():
        return {"runs": []}
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        info = {
            "name": d.name,
            "path": str(d.relative_to(REPO_ROOT)),
            "mtime": datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat(),
        }
        for csv_name in ("all_survivors.csv", "cross_tf_replicators.csv"):
            f = d / csv_name
            if f.exists():
                try:
                    with f.open("r") as fh:
                        rows = list(csv.DictReader(fh))
                    info[csv_name] = {"count": len(rows)}
                except Exception:
                    info[csv_name] = {"count": 0}
        out.append(info)
    return {"runs": out[:30]}


def _api_miner_survivors(query: dict[str, list[str]]) -> dict[str, Any]:
    rel = (query.get("dir") or "").strip()
    p = _safe_repo_path(rel)
    if p is None or not p.is_dir():
        return {"error": "invalid dir"}
    file_name = (query.get("file") or "all_survivors.csv")[0] if isinstance(query.get("file"), list) else "all_survivors.csv"
    file_name = (query.get("file") or ["all_survivors.csv"])[0]
    if "/" in file_name or ".." in file_name:
        return {"error": "invalid file"}
    f = p / file_name
    if not f.exists():
        return {"error": f"missing {file_name}"}
    sort_key = (query.get("sort") or ["effect_r"])[0]
    limit = int((query.get("limit") or ["100"])[0])
    with f.open("r") as fh:
        rows = list(csv.DictReader(fh))

    def _safe_float(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=lambda r: abs(_safe_float(r.get(sort_key, 0))), reverse=True)
    return {"file": str(f.relative_to(REPO_ROOT)), "count": len(rows), "rows": rows[:limit]}


# ---------- Risk console --------------------------------------------------


def _api_risk() -> dict[str, Any]:
    rows = _read_journal()
    realised = [_to_float(r.get("realised_r")) for r in rows]
    equity_curve = []
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for i, r in enumerate(realised):
        cum += r
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
        equity_curve.append({
            "i": i,
            "closed_at": rows[i].get("closed_at"),
            "cum_r": cum,
            "drawdown_r": dd,
        })
    by_day: dict[str, int] = defaultdict(int)
    for r in rows:
        ts = (r.get("opened_at") or "")[:10]
        if ts:
            by_day[ts] += 1
    paper = []
    for name, path in _paper_state_paths():
        st = _load_state(path)
        if st:
            paper.append({
                "broker": name,
                "equity": st.get("paper_equity"),
                "daily_peak": st.get("daily_peak_equity"),
                "kill_switch": st.get("kill_switch", False),
                "kill_reason": st.get("kill_reason"),
                "open_position": st.get("open_position"),
                "path": str(path.relative_to(REPO_ROOT)),
            })
    return {
        "n_trades": len(rows),
        "max_drawdown_r": max_dd,
        "current_drawdown_r": (peak - cum) if rows else 0.0,
        "total_r": cum,
        "equity_curve": equity_curve,
        "trades_per_day": dict(sorted(by_day.items())[-30:]),
        "paper_states": paper,
    }


def _api_risk_kill(body: dict[str, Any]) -> dict[str, Any]:
    target = (body.get("broker") or "").strip()
    enabled = bool(body.get("enabled", True))
    reason = (body.get("reason") or "manual_ui").strip()[:200]
    paths = dict(_paper_state_paths())
    if target and target not in paths:
        return {"ok": False, "error": f"unknown broker: {target}", "available": list(paths.keys())}
    affected = []
    for name, path in _paper_state_paths():
        if target and name != target:
            continue
        st = _load_state(path) or {}
        st["kill_switch"] = enabled
        st["kill_reason"] = reason if enabled else None
        st["last_updated"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(st, indent=2, default=str))
        affected.append(name)
    return {"ok": True, "kill_switch": enabled, "affected": affected}


# ---------- Replay --------------------------------------------------------


def _api_replay(query: dict[str, list[str]]) -> dict[str, Any]:
    rel = (query.get("path") or [""])[0]
    p = _safe_repo_path(rel) if rel else None
    if p is None or not p.exists():
        return {"error": "invalid path"}
    date = (query.get("date") or [""])[0]
    if not date:
        return {"error": "date required (YYYY-MM-DD)"}
    bars_all = _read_ohlc(p, limit=0)
    day_bars = [b for b in bars_all if b["iso"][:10] == date]
    if not day_bars:
        return {"path": rel, "date": date, "bars": [], "trades": [], "error": "no bars on date"}
    rows = _read_journal()
    day_trades = []
    for r in rows:
        opened = (r.get("opened_at") or "")[:10]
        closed = (r.get("closed_at") or "")[:10]
        if opened == date or closed == date:
            try:
                day_trades.append({
                    "opened_at": r.get("opened_at"),
                    "closed_at": r.get("closed_at"),
                    "family": r.get("family"),
                    "side": r.get("side"),
                    "entry": float(r.get("entry") or 0),
                    "stop": float(r.get("stop") or 0),
                    "target": float(r.get("target") or 0),
                    "exit_price": float(r.get("exit_price") or 0),
                    "exit_reason": r.get("exit_reason"),
                    "realised_r": float(r.get("realised_r") or 0),
                    "filter_verdict": r.get("filter_verdict"),
                })
            except (TypeError, ValueError):
                pass
    return {
        "path": rel,
        "date": date,
        "bars": day_bars,
        "trades": day_trades,
        "count": len(day_bars),
    }


# ---------- Decision journal / paper signals / performance -----------------

def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = _tail(path, limit)
    out: list[dict[str, Any]] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            # skip malformed lines
            continue
    return out


def _api_performance() -> dict[str, Any]:
    p = LOGS_DIR / "paper_performance_report.json"
    if not p.exists():
        return {"error": "missing report"}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        return {"error": f"invalid report: {exc}"}


def _api_paper_signals(query: dict[str, list[str]]) -> dict[str, Any]:
    limit = int((query.get("limit") or ["50"])[0])
    p = LOGS_DIR / "paper_signal_outcomes.jsonl"
    rows = _read_jsonl_tail(p, limit)
    return {"rows": rows, "count": len(rows)}


def _api_decision(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return the current decision for the UI. Supports `?refresh=true`."""
    try:
        refresh_val = (query.get("refresh") or ["false"])[0]
        refresh = str(refresh_val).lower() in ("1", "true", "yes")
    except Exception:
        refresh = False
    try:
        # Prefer the cloud bundle normalized decision when the bundle is
        # authored by the local authoritative engine and is fresh. This
        # ensures Render / remote dashboards show the exact local engine
        # decision shape (flattened and normalized).
        try:
            if _cloud_bundle and _cloud_sync_meta and _normalize_render_decision:
                bundle = _cloud_bundle()
                decision = bundle.get("decision") if isinstance(bundle.get("decision"), dict) else None
                meta = _cloud_sync_meta(bundle) if bundle is not None else {}
                source = bundle.get("source") or (decision or {}).get("source") if isinstance(decision, dict) else bundle.get("source")
                if decision and source == "local_authoritative_engine" and meta.get("state") == "fresh":
                    try:
                        return _normalize_render_decision(decision, meta, synced=True)
                    except Exception:
                        pass
        except Exception:
            # fall back to normal path on any normalization error
            pass

        return get_decision_for_api(refresh=refresh)
    except Exception as exc:
        return {"error": str(exc)}


def _api_decision_journal(query: dict[str, list[str]]) -> dict[str, Any]:
    limit = int((query.get("limit") or ["50"])[0])
    p = LOGS_DIR / "decision_journal.jsonl"
    rows = _read_jsonl_tail(p, limit)
    return {"rows": rows, "count": len(rows)}


# ---------- HTTP handler ---------------------------------------------------


class GoldTraderHandler(BaseHTTPRequestHandler):
    server_version = "GoldTraderUI/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Quiet by default; logs go to stderr only for errors.
        return

    # ----- response helpers
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        # Restrict to STATIC_DIR.
        target = (STATIC_DIR / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ----- routes
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)
        try:
            if path == "/" or path == "/index.html":
                self._send_static("index.html")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                return
            if path == "/api/summary":
                self._send_json(_api_summary())
                return
            if path == "/api/secrets":
                self._send_json(_api_secrets_get())
                return
            if path == "/api/journal":
                self._send_json(_api_journal(query))
                return
            if path == "/api/stats":
                self._send_json(_api_stats())
                return
            if path == "/api/logs":
                self._send_json(_api_logs(query))
                return
            if path == "/api/logs/list":
                self._send_json(_api_logs_list())
                return
            if path == "/api/live/notes":
                self._send_json(_api_live_notes(query))
                return
            if path == "/api/jobs":
                self._send_json(_api_jobs(query))
                return
            if path == "/api/calendar":
                self._send_json(_api_calendar())
                return
            if path == "/api/datasets":
                self._send_json(_api_datasets())
                return
            if path == "/api/candles":
                self._send_json(_api_candles(query))
                return
            if path == "/api/indicators":
                self._send_json(_api_indicators(query))
                return
            if path == "/api/strategies/families":
                self._send_json(_api_families())
                return
            if path == "/api/macro/list":
                self._send_json(_api_macro_list())
                return
            if path == "/api/macro/series":
                self._send_json(_api_macro_series(query))
                return
            if path == "/api/bridge/status":
                self._send_json(_api_bridge_status())
                return
            if path == "/api/live/candles":
                self._send_json(_api_live_candles(query))
                return
            if path == "/api/live/tracker":
                self._send_json(_api_live_tracker(query))
                return
            if path == "/api/live/scout":
                self._send_json(_api_live_scout(query))
                return
            if path == "/api/live/ifvg/checklist":
                self._send_json(_api_live_ifvg_checklist(query))
                return
            if path == "/api/live/zones":
                self._send_json(_api_live_zones(query))
                return
            if path == "/api/live/confluence":
                self._send_json(_api_live_confluence(query))
                return
            if path == "/api/miner/results":
                self._send_json(_api_miner_results())
                return
            if path == "/api/miner/survivors":
                self._send_json(_api_miner_survivors(query))
                return
            if path == "/api/risk":
                self._send_json(_api_risk())
                return
            if path == "/api/replay":
                self._send_json(_api_replay(query))
                return
            if path == "/api/performance":
                self._send_json(_api_performance())
                return
            if path == "/api/paper-signals":
                self._send_json(_api_paper_signals(query))
                return
            if path == "/api/decision":
                self._send_json(_api_decision(query))
                return
            if path in ("/api/decision-journal", "/api/decision_journal"):
                self._send_json(_api_decision_journal(query))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            body = self._read_json_body()
            if path == "/api/config":
                self._send_json(_api_set_config(body))
                return
            if path == "/api/secrets":
                self._send_json(_api_secrets_set(body))
                return
            if path == "/api/run-cycle":
                self._send_json(_api_run_cycle())
                return
            if path == "/api/sync-macro":
                self._send_json(_api_sync_macro(body))
                return
            if path == "/api/calendar/add":
                self._send_json(_api_calendar_add(body))
                return
            if path == "/api/calendar/delete":
                self._send_json(_api_calendar_delete(body))
                return
            if path == "/api/bridge/close":
                self._send_json(_api_bridge_close(body))
                return
            if path == "/api/bridge/start":
                self._send_json(_api_bridge_start())
                return
            if path == "/api/ifvg/approve":
                self._send_json(_api_ifvg_approve(body))
                return
            if path == "/api/lab/holdout":
                self._send_json(_api_lab_holdout(body))
                return
            if path == "/api/lab/permutation":
                self._send_json(_api_lab_permutation(body))
                return
            if path == "/api/miner/run":
                self._send_json(_api_miner_run(body))
                return
            if path == "/api/risk/kill":
                self._send_json(_api_risk_kill(body))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=500)


# ---------- public entry --------------------------------------------------


def build_server(host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), GoldTraderHandler)


def serve(host: str = "127.0.0.1", port: int = 8770) -> None:
    httpd = build_server(host, port)
    print(f"gold-trader UI: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

# Gold Trader

Autonomous XAUUSD research + live trading agent. Pure-stdlib Python on
Linux. Live execution via MT5-under-Wine bridge. Web UI for live
monitoring, research, and operator controls. Cron drives the loop.

> **Single source of truth: [docs/HANDBOOK.md](docs/HANDBOOK.md)** —
> architecture, strategy library, MT5 runbook, research methodology,
> empirical results, roadmap, troubleshooting.
>
> **Latest audit (Apr 29 – May 29 2026):** [docs/AUDIT_RESULTS.md](docs/AUDIT_RESULTS.md) —
> full-stack pipeline, grade/family verdict, live scout rules.
>
> **IFVG operator guide:** [docs/IFVG_CONFLUENCE_ASSISTANT.md](docs/IFVG_CONFLUENCE_ASSISTANT.md) ·
> **Execution geometry:** [docs/IFVG_EXECUTION_GEOMETRY_AUDIT.md](docs/IFVG_EXECUTION_GEOMETRY_AUDIT.md) ·
> **HTF IFVG reset:** [docs/HTF_IFVG_RESET.md](docs/HTF_IFVG_RESET.md) ·
> **Full-system IFVG:** [docs/FULL_SYSTEM_IFVG.md](docs/FULL_SYSTEM_IFVG.md) ·
> **Command center (Render):** [docs/ABSOLUTE_GOLD_COMMAND_CENTER.md](docs/ABSOLUTE_GOLD_COMMAND_CENTER.md) ·
> **Multi-page app:** [docs/ABSOLUTE_GOLD_APP.md](docs/ABSOLUTE_GOLD_APP.md) ·
> **Full market awareness:** [docs/FULL_MARKET_AWARENESS.md](docs/FULL_MARKET_AWARENESS.md) ·
> **React command center:** [docs/REACT_COMMAND_CENTER.md](docs/REACT_COMMAND_CENTER.md) ·
> **React v2 (polished UI):** [docs/REACT_COMMAND_CENTER_V2.md](docs/REACT_COMMAND_CENTER_V2.md) ·
> **React v3 (live data binding):** [docs/REACT_COMMAND_CENTER_V3.md](docs/REACT_COMMAND_CENTER_V3.md) ·
> **Pro command center:** [docs/PRO_COMMAND_CENTER.md](docs/PRO_COMMAND_CENTER.md) ·
> **Command center v2 (market cockpit):** [docs/COMMAND_CENTER_V2.md](docs/COMMAND_CENTER_V2.md) ·
> **System hardening v2:** [docs/SYSTEM_HARDENING_V2.md](docs/SYSTEM_HARDENING_V2.md) ·
> **Market intelligence UX (Render):** [docs/MARKET_INTELLIGENCE_UX.md](docs/MARKET_INTELLIGENCE_UX.md) ·
> **Live pipeline repair:** [docs/LIVE_PIPELINE_REPAIR.md](docs/LIVE_PIPELINE_REPAIR.md) ·
> **Live pipeline repair v3:** [docs/LIVE_PIPELINE_REPAIR_V3.md](docs/LIVE_PIPELINE_REPAIR_V3.md)

---

## Audit verdict (Apr–May 2026)

**Do not trade the 9-family firehose.** Unfiltered overlap lost **−$79,297**; sequential one-position **−$334**.

The defensible path is **IFVG Grade A only** via `./start` scout with hard sentiment gates:

| Gate | Rule |
|------|------|
| Grade | **A only** — B paper-only, C/D blocked |
| Alignment | **mixed bearish bias** required |
| Macro | **`macro_regime=mixed` required** — hard block `aligned` / `opposed` / `partial` / `unavailable` |
| Risk | **$30/trade** sizing reference — not fixed 0.05 lot on $100 |
| Targets | Sentiment-based TP — TP1 min 1R floor; TP2/TP3 from swings + `market_levels.json` (no 2R profit cap) |
| Live | **Paper yes, live no** until **20+ forward Grade-A trades** confirm edge |

Macro hard block impact: **110 → 0** `can_enter` signals in 30-day replay (pre-block entries were all `macro=aligned`; Grade A + `macro=mixed` geometry slice **+2.65R / 71% WR** vs aligned **−10.29R**).

Detail, re-run commands, and output paths: **[docs/AUDIT_RESULTS.md](docs/AUDIT_RESULTS.md)**.

---

## Quickstart

```bash
# 1) one-time
bash scripts/setup_wine_mt5.sh        # Wine + MT5 + embedded Win Python
# (then perform manual MT5 GUI steps — see HANDBOOK §12)

# 2) every day — one command (MT5 + bridge + AI scout + UI)
./start
# open http://127.0.0.1:8770
# AI scans live bars every ~60s — your only action: click Enter trade when green

# 3) one time, to enable autonomous trading
bash scripts/install_cron.sh          # */15 agent-cycle + Sunday champion
```

Stop: `bash scripts/stop.sh`. Status: `bash scripts/status.sh`.

No separate bridge or env-export steps — `./start` loads credentials and secrets automatically.

When the broker is connected, charts and the IFVG AI scan use **live MT5 candles** (refreshed every 5 s). Cached CSV is preview-only when the bridge is offline.

**Operator workflow:** `./start` → open Trade tab → read the approval brief (why you may enter) → click **Enter trade** when the banner turns green. The background scout runs automatically; you do not refresh research manually.

---

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
# 421 passing, 1 failed (pre-existing macro_bundle drift), 1 skipped
```

---

For everything else, see [docs/HANDBOOK.md](docs/HANDBOOK.md).

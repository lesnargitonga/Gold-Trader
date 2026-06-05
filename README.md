# Gold Trader

Autonomous XAUUSD research and supervised paper-trading system. The local PC is the
authoritative trading/data engine. Render is the official remote dashboard that
mirrors synced local state. Live orders remain locked.

> **Current system truth:** [docs/SYSTEM_TRUTH.md](docs/SYSTEM_TRUTH.md)
>
> **Render dashboard mode:** [docs/RENDER_FRONTEND_MODE.md](docs/RENDER_FRONTEND_MODE.md)
>
> **Local operation:** [docs/FINAL_LOCAL_OPERATION.md](docs/FINAL_LOCAL_OPERATION.md)
>
> **Paper evidence:** [docs/PAPER_JOURNAL_AND_PERFORMANCE.md](docs/PAPER_JOURNAL_AND_PERFORMANCE.md)
>
> Older APPLY docs and old Command Center variants are historical implementation notes.
> They are not current operating instructions unless `docs/SYSTEM_TRUTH.md` links to them.

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

For current operating truth, see [docs/SYSTEM_TRUTH.md](docs/SYSTEM_TRUTH.md).

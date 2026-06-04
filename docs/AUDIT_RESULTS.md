# 30-Day Full-Stack Audit (Apr 29 – May 29, 2026)

> **Fresh rerun (2026-05-30 UTC):** All audit tiers re-run with current code (macro=mixed hard block, Grade A gates, `_entry_plan` sentiment TP). IFVG red/green side fix was **UI-only** — core side logic unchanged. **`can_enter=0`** in window (206 Grade A signals, none pass all gates). Geometry **macro=mixed** slice unchanged (**+14.83R**, n=64); Grade A + macro=mixed **+2.65R** (n=7). System sequential sim **worse** with honest TP2 targets: all-families **56 tr / −$638**; IFVG Grade A **14 tr / −$187** (vs prior doc +$43 — do not use old number). Summary: [`logs/fresh_audit_summary.json`](../logs/fresh_audit_summary.json) · run log: [`logs/fresh_audit_run_20260530.log`](../logs/fresh_audit_run_20260530.log) · prior JSON backup: [`logs/audit_backup_20260530T164410Z/`](../logs/audit_backup_20260530T164410Z/).

**Window:** `data/agent_live_xauusd` · **Risk model:** $30/trade · **Cadence:** M60 snapshots (default)

This document is the **audit hub** — pipeline, verdict, live scout rules, forward plan, and caveats.
Primary artefacts: `logs/full_stack_best_combos.json`, `logs/system_full_audit_report.json`,
`logs/ifvg_execution_geometry_audit.json`, `logs/grade_a_live_sim_30d.json`.

Operator guide: [IFVG_CONFLUENCE_ASSISTANT.md](IFVG_CONFLUENCE_ASSISTANT.md) ·
Geometry detail: [IFVG_EXECUTION_GEOMETRY_AUDIT.md](IFVG_EXECUTION_GEOMETRY_AUDIT.md)

---

## Executive verdict

**Unfiltered firehose: not profitable.** Edge exists only in tight IFVG slices with sentiment gates.

| Mode | Trades | WR | Net PnL |
|------|--------|-----|---------|
| Full-stack overlap (all signals) | 4,738 | 25.0% | **−$79,297** |
| Sequential one-position (realistic) | 96 | 42.7% | **−$334** |

**Defensible live path:** `./start` IFVG scout — **Grade A only**, **mixed bearish bias**, **`macro_regime=mixed`**, manual Enter trade, **$30/trade** risk (not fixed 0.05 lot). Sentiment-based TP (TP1 min 1R; TP2/TP3 from levels) — **no 2R profit cap**.

**Paper yes, live no** until **20+ forward Grade-A trades** with journal outcomes confirm the slice.

**One-line:** Do not trade the 9-family firehose. IFVG Grade A + sentiment stack is the only path with positive sequential evidence (+$43 on 18 trades @ $30 risk in system audit).

---

## Audit script hierarchy

Run in order when re-auditing. Each layer adds data; none replaces the others.

| Tier | Script | Scope | Purpose |
|------|--------|-------|---------|
| **1 — IFVG only** | [`scripts/ifvg_deep_audit.py`](../scripts/ifvg_deep_audit.py) | `find_ifvg_setups()` (same as live scout) | Grade A/B/C execution, checklist + HTF + macro + levels |
| **2 — Full system (sequential sim)** | [`scripts/system_full_audit.py`](../scripts/system_full_audit.py) | All 9 families via `build_bundle_snapshot` | One-position PnL, family/grade elimination advice |
| **3 — Unbiased discovery** | [`scripts/full_entry_scan.py`](../scripts/full_entry_scan.py) | Same engine, **no filter gates** on collection | Raw candidate CSV; bundle accept/reject = metadata only |
| **3b — Evaluate discovery** | [`scripts/evaluate_entry_scan.py`](../scripts/evaluate_entry_scan.py) | Reads tier-3 CSV | Per-signal overlap sim + slice rankings |
| **4 — Full-stack discovery** | [`scripts/full_stack_entry_scan.py`](../scripts/full_stack_entry_scan.py) | Tier 3 + workflow context, OpenAI cache lookup, per-TF bundle fields | Widest CSV (70+ columns) |
| **5 — Full-stack evaluate** | [`scripts/evaluate_full_stack.py`](../scripts/evaluate_full_stack.py) | Reads tier-4 CSV | Combo grid search + sequential sim for top slices |
| **6 — Execution geometry** | [`scripts/ifvg_execution_geometry_audit.py`](../scripts/ifvg_execution_geometry_audit.py) | M15+M60 IFVG, 4 SL/TP models | Zone vs structural SL; sentiment slice R-multiples |
| **6b — Live profile sim** | [`scripts/grade_a_live_sim.py`](../scripts/grade_a_live_sim.py) | Production `can_enter` gates on historical bars | Before/after macro block; fixed-lot stress test |

**IFVG-only** answers “does our scout strategy pay?” **System audit** adds all families + bundle sentiment.
**Unbiased / full-stack** answers “what entry points exist, and which slices help?” without applying live gates at collection time.
**Geometry + live sim** answers “does execution geometry and production gating hold up?”

---

## Data layers by tier

| Layer | IFVG deep | System / entry scan | Full-stack scan |
|-------|-----------|---------------------|-----------------|
| Multi-TF bars (5/15/60/240) | ✓ | ✓ | ✓ |
| Bundle sentiment (HTF bias, alignment, oscillation) | HTF only | ✓ | ✓ + per-TF trend/RSI/MACD/structure |
| Macro CSV (dxy, us10y, real10y) | ✓ | ✓ | ✓ + 5d deltas, `macro_regime` |
| News calendar | ✓ | ✓ | ✓ + `news_blackout` |
| `config/market_levels.json` | ✓ | ✓ | ✓ |
| IFVG checklist + A/B/C/D grades | ✓ | ✓ (IFVG rows) | ✓ + workflow metadata |
| OpenAI web research | **OFF** | config only; no historical replay | **Cache hour-bucket lookup**; else `research_unavailable_at_signal` |
| CME / options live feed | **No** | **No** — proxy via `market_levels.json` (+ OpenAI cache block when hit) | same |
| Bundle decision (accept/reject/hold) | — | metadata only | metadata only |
| 8-step workflow hard gates | **Not applied** in sim | — | `workflow_ready` recorded, not enforced |

**Gaps (all tiers):** no per-bar OpenAI web replay; no CME API; M1 bars absent from `agent_live_xauusd` (workflow step 4 may wait); `./start` runs IFVG scout only, not full bundle on every tick.

---

## IFVG grade rules (keep / tighten / eliminate)

| Grade | Overlap (entry scan) | Parallel IFVG (system audit) | Sequential 1-pos | Action |
|-------|----------------------|------------------------------|------------------|--------|
| **A** | 102 tr · 69.6% WR · **+$113** | 100 tr · 70% WR · **+$123** | 18 tr · 72% WR · **+$43** | **KEEP** — only grade green in both sims |
| **B** | 185 tr · 55% WR · **−$618** | 161 tr · 55% WR · **−$478** | 12 tr · 67% WR · **−$14** | **TIGHTEN** — paper-only or require `workflow_ready` + 60m |
| **C** | 64 tr · 62% WR · **−$195** | 53 tr · 62% WR · **−$150** | 7 tr · 86% WR · **−$12** | **ELIMINATE** live — WR misleading, PnL negative |
| **D** | — | — | — | **ELIMINATE** — `verdict=ignore`; never emitted in CSV |

Best IFVG TF slices (overlap): A @ 15m (+$152), A @ 60m (+$29).

Geometry (519 signals, structural baseline): Grade A aggregate **−7.64R**; Grade A + **`macro_regime=mixed`** only **+2.65R / 71% WR (n=7)** vs aligned **−10.29R (n=176)**.

---

## Family keep / eliminate

| Family | Overlap net | Sequential 1-pos | Verdict |
|--------|-------------|------------------|---------|
| `asian_range_breakout` | **+$925** | −$124 (n=3) | **PROMOTE** — watchlist; seq sample tiny |
| `liquidity_sweep` | **+$438** | −$92 | **KEEP** paper |
| `inversion_fair_value_gap` | −$700 | **+$17** (73% WR) | **KEEP** — scout core; grade A essential |
| `london_breakout` | −$36,427 | **+$113** | **TIGHTEN** — raw toxic; filtered 15m slices work |
| `trend_pullback` | −$1,420 | −$182 | **DISABLE** raw; keep `tf=15 + macro=aligned` slice only |
| `momentum_burst` | −$5,047 | −$24 | **DISABLE** |
| `ny_session_breakout` | −$28,623 | — | **ELIMINATE** |
| `compression_breakout` | −$594 (0% WR) | — | **ELIMINATE** |
| `timed_horizon_macro_regime` | −$1,021 | — | **ELIMINATE** |

---

## Sentiment filters (helps vs hurts)

**Prefer:** `alignment=mixed bearish bias`, `macro_regime=mixed`, `htf_bias=bearish` or `neutral`.

**Avoid as standalone gates:** `macro_regime=aligned`, `alignment=mixed bullish bias`, `decision_status=accept`, `oscillation=mixed transition regime`.

**Paradox:** bundle `reject` outperformed `accept` in sequential sim (+$38 vs −$313). Do not auto-trade accepts.

**OpenAI / news:** 100% of IFVG scan rows = `research_unavailable_at_signal`; `news_clear=clear` tags all rows — not discriminative in backtest. Enable OpenAI forward-only live.

---

## Macro hard block (before / after)

Production code in [`ifvg_workflow.py`](../src/gold_trader/assistants/ifvg_workflow.py) (`evaluate_live_sentiment`) and
[`ifvg_scout.py`](../src/gold_trader/assistants/ifvg_scout.py) (`build_approval_brief`).

| State | `can_enter` (30-day replay, M15+M60 Grade A path) | Notes |
|-------|-----------------------------------------------------|-------|
| **Before** macro hard block | **110** | All had `macro_regime=aligned`; geometry aligned slice **−48.66R** aggregate |
| **After** `macro_regime=mixed` required | **0** | Only **9** Grade-A signals hit `macro=mixed` in window — rare but positive (+2.65R on 7 geometry trades) |

Override for discretionary entries: `IFVG_MACRO_OVERRIDE=1` (downgrades macro block to warning).

---

## Live scout `can_enter` rules (current code)

All must pass for green **Enter trade** banner (`build_approval_brief`):

1. **Grade A** — B/C/D blocked (`LIVE_GRADE_AUDIT_NOTE`)
2. **Verdict** `valid_entry` or `alert_wait`
3. **Technical score ≥ 65**
4. **`workflow_ready=true`** — resolve workflow blockers first
5. **No workflow hard-fail** on steps 1 (HTF bias) or 3 (price location)
6. **No sentiment blockers:**
   - Alignment = **`mixed bearish bias`** (blocks: mixed bullish, fully aligned stacks, compression-heavy mixed)
   - **`macro_regime=mixed`** (blocks: aligned, opposed, partial, unavailable)
7. **Not externally blocked** — only when OpenAI **hard** mode (default is soft)
8. **Manual approval always required** — bundle accept/reject is not permission

**Risk shown in brief:** `suggested_risk_usd = $30` for Grade A (`LIVE_RISK_USD`).

**TP messaging:** TP1 min 1R floor; TP2/TP3 from swings + `market_levels.json` — no fixed 2R runner cap.

---

## Recommended live scout profile (Monday checklist)

Apply until the next 30-day audit or 20+ forward Grade-A trades. Matches current `./start` IFVG scout scope.

- [ ] **Grade A only** — block B/C/D and `verdict=ignore`
- [ ] **Workflow ready** — resolve M1 blockers before size
- [ ] **HTF bias** — prefer bearish or neutral; skip bullish-only unless grade A @ 60m
- [ ] **Alignment** — require **mixed bearish bias**
- [ ] **Macro** — require **`macro_regime=mixed`** (override: `IFVG_MACRO_OVERRIDE=1`)
- [ ] **Timeframe** — 15m primary, 60m secondary for grade A
- [ ] **News** — respect `news_blackout=true` and `news_blackout_min` in runtime config
- [ ] **Bundle decision** — manual approve only; do **not** treat `accept` as permission
- [ ] **OpenAI** — live forward via scout; refresh `config/market_levels.json` weekly
- [ ] **Risk** — **$30/trade** until 20+ sequential grade-A trades confirm edge (not fixed 0.05 lot)
- [ ] **Do not enable:** NY session, compression, timed_horizon, raw London 5m/60m, trend_pullback without macro filter
- [ ] **Paper journal** — log every Enter trade + outcome_r in shadow CSV before live promotion

**Paper-only expansions (not scout):** `london_breakout | tf=15 | bearish | mixed bearish | macro=aligned | score 80-89`; `asian_range_breakout` 5m/15m with neutral HTF + mixed macro.

---

## Forward validation plan

1. **Run scout paper-only** — `./start`, Enter trade on green banner only, fill `outcome_r` in `data/agent_live_xauusd/ifvg_shadow_setups.csv`
2. **Target n ≥ 20** closed Grade-A trades with full sentiment stack (mixed bearish + macro mixed)
3. **Track:** win rate, avg R, max DD, execution drift vs plan TP1/SL
4. **Weekly:** `scripts/ifvg_shadow_report.py` + compare to geometry baseline (+2.65R on n=7 is hypothesis, not proof)
5. **Re-audit** after 30 calendar days or at n=20 — re-run tiers 1–6b
6. **Live promotion gate:** sequential sim positive at $30 risk **and** forward journal PF ≥ 1.2 **and** operator sign-off

---

## How to re-run

From repo root. Default window matches the audit (`--end 2026-05-29`, 30 days).

```bash
# Tier 1 — IFVG deep (fastest sanity check)
PYTHONPATH=src .venv/bin/python scripts/ifvg_deep_audit.py --days 30 --end 2026-05-29

# Tier 2 — full system sequential sim (~5–15 min @ cadence 60)
PYTHONPATH=src .venv/bin/python scripts/system_full_audit.py --days 30 --end 2026-05-29 --cadence 60

# Tier 3 — unbiased discovery + evaluate
PYTHONPATH=src .venv/bin/python scripts/full_entry_scan.py --days 30 --end 2026-05-29 --cadence 60
PYTHONPATH=src .venv/bin/python scripts/evaluate_entry_scan.py

# Tier 4–5 — full-stack (widest CSV + combo rankings)
PYTHONPATH=src .venv/bin/python scripts/full_stack_entry_scan.py --start 2026-04-29 --end 2026-05-29 --cadence 60
PYTHONPATH=src .venv/bin/python scripts/evaluate_full_stack.py
# Optional: forward OpenAI for signals in last 2h only
PYTHONPATH=src .venv/bin/python scripts/full_stack_entry_scan.py --with-openai-live

# Tier 6 — IFVG execution geometry (4 models, sentiment slices)
PYTHONPATH=src .venv/bin/python scripts/ifvg_execution_geometry_audit.py --start 2026-04-29 --end 2026-05-29

# Tier 6b — Grade A live profile sim (production can_enter gates)
PYTHONPATH=src .venv/bin/python scripts/grade_a_live_sim.py --start 2026-04-29 --end 2026-05-29
```

Use `--data-dir data/agent_live_xauusd` (default). Lower `--cadence` (15 or 5) finds more candidates but runs slower.

---

## Output paths

| File | Producer |
|------|----------|
| `logs/ifvg_deep_audit_report.json` | `ifvg_deep_audit.py` |
| `logs/ifvg_deep_audit_trades.csv` | `ifvg_deep_audit.py` |
| `logs/system_full_audit_report.json` | `system_full_audit.py` |
| `logs/system_full_audit_trades.csv` | `system_full_audit.py` |
| `logs/full_entry_scan_signals.csv` | `full_entry_scan.py` |
| `logs/full_entry_scan_report.json` | `full_entry_scan.py` |
| `logs/entry_scan_evaluation.json` | `evaluate_entry_scan.py` |
| `logs/full_stack_scan_signals.csv` | `full_stack_entry_scan.py` |
| `logs/full_stack_scan_report.json` | `full_stack_entry_scan.py` |
| `logs/full_stack_evaluation.json` | `evaluate_full_stack.py` |
| `logs/full_stack_best_combos.json` | `evaluate_full_stack.py` |
| `logs/ifvg_execution_geometry_audit.json` | `ifvg_execution_geometry_audit.py` |
| `logs/ifvg_execution_geometry_trades.csv` | `ifvg_execution_geometry_audit.py` |
| `logs/grade_a_live_sim_30d.json` | `grade_a_live_sim.py` |
| `logs/ifvg_scout_state.json` | live scout (`ifvg_auto_scout.py`) |

---

## Caveats

| Issue | Impact |
|-------|--------|
| Overlap vs sequential | Combo rankings use independent per-signal sim; top combo (+$1,730 overlap) → **−$9** on 9 sequential trades |
| OpenAI cache gap | Historical sentiment untested; live-only |
| Sample size | Sequential sim = 96 trades total; Grade A + macro mixed = **7 geometry trades** — treat as hypothesis |
| Macro gate rarity | After hard block, **0** historical `can_enter` — live may wait long between setups |
| Fixed-lot stress test | `grade_a_live_sim` at 0.05 lot / $100 can blow account despite high WR — use **$30 risk** sizing, not fixed lots |
| Live vs audit | Operator skip/approve, limit-in-zone entry, workflow hard gates not fully modeled |
| Zone SL hypothesis | Zone SL + 1R did not beat structural SL on aggregate R; sentiment stack matters more than SL model alone |

Re-audit after 30 days of sequential live grade-A trades or at forward n=20.

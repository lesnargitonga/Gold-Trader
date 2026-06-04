# Gold Trader — Handbook

**Single source of truth.** This document supersedes the previous
`README.md` long-form, `docs/system_status.md`, `docs/research_plan.md`,
`docs/runbook_mt5.md`, and `docs/evaluation.md`. All operational, research,
runbook and empirical detail lives here.

_Last updated: 2026-05-30_

---

## Recent: 30-day full-stack audit + execution geometry + live gates (2026-05-30, latest)

Apr 29 – May 29 2026 audit on `data/agent_live_xauusd`: **seven-script pipeline**
(IFVG deep → system audit → unbiased scan → full-stack scan → combo evaluate →
execution geometry → Grade A live sim).

**Verdict:** unfiltered system not profitable (−$79k overlap / −$334 sequential).
**IFVG Grade A only** defensible path — B tighten (paper), C/D eliminate.
Scout Monday profile: Grade A + **mixed bearish bias** + **`macro_regime=mixed`**
(hard block aligned). **$30/trade** risk — not fixed 0.05 lot. Sentiment-based TP
(TP1 min 1R; TP2/TP3 from levels) — no 2R profit cap.

Macro hard block: **110 → 0** `can_enter` in 30-day replay. Grade A + macro mixed
geometry slice **+2.65R / 71% WR (n=7)** vs aligned **−10.29R**. **Paper yes, live no**
until 20+ forward Grade-A trades.

Full detail, Monday checklist, forward plan, re-run commands:
**[docs/AUDIT_RESULTS.md](AUDIT_RESULTS.md)** ·
**[docs/IFVG_EXECUTION_GEOMETRY_AUDIT.md](IFVG_EXECUTION_GEOMETRY_AUDIT.md)** ·
**[docs/IFVG_CONFLUENCE_ASSISTANT.md](IFVG_CONFLUENCE_ASSISTANT.md)**

---

## Recent: Full ensemble scoring — universal scorer + strategy weights + concurrence (2026-05-10)

**Wired up.** Every strategy in the system now carries a confluence
score on every signal it emits, plus a strategy-level weight derived
from observatory PF×√n, plus a concurrence multiplier when multiple
strategies fire on the same bar.  Three layers of evidence combine
into one live operator metric.

### Architecture

**Layer 1 — inside scoring (filter → signal score 0..100):**
- Strategies with bespoke scoring (IFVG, RSI) keep their internal
  scorer (see "Scoring redesign" + v2 sections below).
- Every other strategy auto-receives a *universal score* via the
  engine layer: `engine.run_backtest` calls
  `filters.universal_score(bars, index, side)` after `signal_for`
  returns a non-None signal, attaching the score to the signal.
- Universal scorer (in [src/gold_trader/strategies/filters.py](../src/gold_trader/strategies/filters.py))
  combines 7 strategy-agnostic confluence features summing to 100:

  | feature | type | pts | rationale |
  |---|---|---:|---|
  | u_htf_alignment    | binary | 20 | HTF EMA20 vs EMA50 agrees with side |
  | u_news_clear       | binary | 15 | ≥30min from scheduled news |
  | u_weekend_clear    | binary | 10 | not Friday-late or weekend |
  | u_session_quality  | 3-way  | 20 | 20 in core London/NY, 10 edge, 0 off |
  | u_spread_quality   | binary | 15 | spread ≤ 1.2× rolling mean |
  | u_atr_regime       | 3-way  | 10 | 10 in healthy band, 5 edge, 0 extreme |
  | u_not_overextended | binary | 10 | last 5 bars not all same direction |

  No sign-error risk: every feature has empirically uncontroversial
  direction on XAUUSD intraday.

**Layer 2 — strategy weight (0..1 from observatory):**
- [scripts/compute_strategy_weights.py](../scripts/compute_strategy_weights.py)
  reads `reports/observatory/<run>/per_strategy.csv` and writes
  `data/strategy_weights.json`:

      raw    = clip(PF, 0, 5) × √n   (n ≥ min_n=20, else 0)
      weight = raw / max(raw)

- Weights for the 5y/15m default-params run (n=279):

  | strategy | weight | n | PF |
  |---|---:|---:|---:|
  | opening_range_breakout | 1.000 | 51 | 1.04 |
  | asian_range_breakout   | 0.747 | 33 | 0.97 |
  | momentum_burst         | 0.671 | 22 | 1.07 |
  | asian_range_fade       | 0.541 | 21 | 0.88 |
  | ny_session_breakout    | 0.523 | 36 | 0.65 |

- Strategies with n<20 (e.g. liquidity_sweep n=16) get weight=0
  pending more data.  ny_close_compression scored PF=2.21 but only
  n=16 — not yet eligible for weight; needs more samples.

**Layer 3 — concurrence multiplier:**
- [src/gold_trader/ensemble.py](../src/gold_trader/ensemble.py) carries
  empirically calibrated table from observatory:

      1 strategy alone   → 1.00
      2 concurrent       → 0.85   (n=56 PF=0.62, slightly worse than alone)
      3 concurrent       → 1.75   (n=9  PF=2.41)
      4 concurrent       → 2.00   (n=4  PF=∞, capped)

**Combined live metric:**
```
signal_strength = max(0, inside_score)
                × strategy_weight(name)
                × concurrence_multiplier(count)
```

### Validation (5y/15m, n=279, default params)

`signal_strength` is **monotonically predictive** — exactly the
property v1 RSI scoring lacked:

| signal_strength | n | avg_R | PF | win% |
|---:|---:|---:|---:|---:|
| <30        | 116 | −0.195 | 0.69 | 37.9% |
| [30,60)    |  75 | −0.089 | 0.80 | 48.0% |
| [60,100)   |  76 | −0.061 | 0.85 | 42.1% |
| **[100,150)** | **11** | **+0.628** | **17.90** | **72.7%** |
| ≥150       |   1 | +0.460 | ∞    | 100%  |

PF rises monotonically.  `signal_strength ≥ 100` (top 4% of fires) =
PF=17.9 across 11 trades.  Threshold candidate for the live gate.

### Per-strategy sweet spots (from per_strategy_bucket.csv)

Where each family shines on inside_score:

| strategy | best bucket | n | PF |
|---|---|---:|---:|
| ny_close_compression     | [85,100] | 6 | **4.74** |
| inversion_fair_value_gap | [70,85)  | 3 | 2.53 |
| ny_close_compression     | [70,85)  | 10 | 1.53 |
| opening_range_breakout   | [70,85)  | 33 | 1.15 |
| momentum_burst           | [70,85)  | 21 | 1.10 |
| opening_range_breakout   | [85,100] | 16 | 1.05 |

### Ops notes

- All 280 tests still pass (1 pre-existing unrelated macro_bundle drift).
- Engine emit-rate is unchanged: `gate_universal_score=False` by
  default, so universal scoring is purely instrumentation.  Set
  `BacktestConfig(gate_universal_score=True)` to enable
  size_multiplier-based gating once thresholds are calibrated.
- `data/strategy_weights.json` was generated from in-sample data
  (the same 5y/15m we backtest on) — **walk-forward required** before
  going live.  Next step: split 5y into train/test, recompute weights
  on train, validate signal_strength PF on test.
- Observatory artefacts live in `reports/observatory/5y_15m_v3/`.

### What's open

1. Walk-forward validation of strategy weights (5y → 4y train +
   1y test; recompute weights on train, score signals on test).
2. Calibrate `gate_universal_score` threshold from
   `signal_strength ≥ 100` empirically.
3. Roll observatory across the **full grid** of every family — the
   default-params n=279 is small.  Full grid would give n in the
   tens of thousands and sharper buckets.
4. Build live "concurrence dashboard" web tab: at each refresh, query
   every strategy's `signal_for` at the latest bar, render
   inside_score + strategy_weight + concurrence + signal_strength
   side-by-side; ARM trade when signal_strength ≥ 100.
5. Roll bespoke filter weights to the 12 strategies that currently
   only carry the universal score, calibrated via `score_vs_r.py`
   per family (avoid the v1 RSI sign-error trap).

---

## Recent: Strategy observatory — scoring as instrumentation (2026-05-10, very late)

**Reframing.** The scoring system is *not* primarily a trade gate.
It's **diagnostic observability**.  Every strategy fires; we record
each signal with its score, verdict, filter breakdown, and outcome,
then derive empirical rankings + concurrence rules from the population.
Live, when an indicator triggers, the operator sees its strength,
which filters drove the score, and what every other strategy is
saying simultaneously.

**Tooling**: [scripts/strategy_observatory.py](../scripts/strategy_observatory.py)
runs all 15 self-contained families on a CSV at default params,
captures every emitted `TradeSignal` with score/verdict/exit, and
writes four artefacts to `reports/observatory/<name>/`:

- `signal_log.csv` — one row per (strategy, signal): timestamp,
  side, score, verdict_bucket, pnl_r, pnl, exit_reason.
- `per_strategy.csv` — overall family ranking by `PF × √n`.
- `per_strategy_bucket.csv` — per (strategy × score-bucket): n, PF,
  avg_R, win-rate.  Where each strategy *shines*.
- `concurrence.csv` — how many strategies fire on the same bar →
  joint forward-R / PF / win-rate.  Multi-strategy confluence as
  meta-signal.

### First-look results (5y/15m, default params per family)

**Per-strategy rank** (top 5 by PF × √n; full table in
`reports/observatory/5y_15m/per_strategy.csv`):

| rank | strategy | n | win% | avg_R | PF |
|---:|---|---:|---:|---:|---:|
| 1 | `ny_close_compression`     | 16 | — | +0.402 | **2.21** |
| 2 | `momentum_burst`           | 22 | — | +0.036 | 1.07 |
| 3 | `liquidity_sweep`          | 16 | — | +0.048 | 1.09 |
| 4 | `opening_range_breakout`   | 51 | — | +0.014 | 1.05 |
| 5 | `asian_range_breakout`     | 33 | — | −0.014 | 0.97 |

**Concurrence finding** — striking:

| concurrent strategies | n | avg_R | PF | win% |
|---:|---:|---:|---:|---:|
| 1 (alone)             | 210 | −0.097 | 0.81 | 43.8% |
| 2                     |  56 | −0.202 | 0.62 | 37.5% |
| **3**                 |   9 | **+0.372** | **2.41** | 44.4% |
| **4**                 |   4 | **+0.438** | **∞** | 100.0% |

**When ≥3 strategies fire the same bar, joint PF jumps from 0.62 to
2.41+.** Sample is small (default params only — one grid combo per
family, n=279 total signals) but the structural signal is clear.
Multi-strategy concurrence is itself a tradable feature.

**Implication for the live agent**: instead of selecting one
"champion" strategy and running only it, run the full ensemble in
shadow-mode, log every fire with full filter breakdown, and only
trade when concurrence count ≥ 3 (or weighted-rank sum exceeds a
threshold).  This is a different bet than "find the one true edge"
— it's "find the moments when many independent signals agree".

### What's next

1. Re-run the observatory across the **full grid** of every family
   (instead of default params only) → vastly larger n, sharper
   ranking, and concurrence with deduplication by (timestamp, side).
2. Roll scoring to ARB + the 14 pending strategies so the bucket
   analysis applies to the full ensemble (currently only IFVG, RSI,
   and ARB-default carry meaningful score values; the rest report
   `score=0.0` ⇒ "unscored" bucket).
3. Build a live "concurrence dashboard" tab: at each web-UI refresh,
   query every strategy's `signal_for(bars, last_index)` and render
   their current scores + top-contributing filters + concurrence
   count.  When the count ≥ 3, ARM the trade.

---

## Recent: Scoring redesign — three-tier filter architecture (2026-05-10, very late)

**Replaces the binary pass/fail filter stack from earlier today.** The
binary approach reduced IFVG signal count by 99.6% and RSI by 97.8%,
pushing holdout n below the n≥30 statistical gate. The fix is a
three-tier scoring system that lets every signal trade — at full,
half, or log-only size — based on a 0-100 confluence score.

**Architecture** ([src/gold_trader/strategies/scoring.py](../src/gold_trader/strategies/scoring.py)):

| Tier | Behaviour | Examples |
|---|---|---|
| `UNIVERSAL_VETO` | Any failure → REJECT regardless of score | news within 60min, weekend, spread above hard ceiling |
| `STRATEGY_VETO`  | Any failure → REJECT regardless of score | IFVG: prior_swing_sweep absent. RSI: min swing size below |
| `SCORED`         | Pass = full points, partial = partial points, fail = 0 | All other "confluence" filters |

**Verdict bands** at thresholds 70 / 55 / 40:

| Verdict | Score range | Size multiplier | Meaning |
|---|---|---:|---|
| `FULL_SIZE` | ≥ 70 | 1.0 | A-grade — trade at configured risk |
| `HALF_SIZE` | 55–69 | 0.5 | B-grade — trade at half risk |
| `LOG_ONLY`  | 40–54 | 0.0 | C-grade — record but don't trade |
| `REJECT`    | < 40 OR any veto failed | 0.0 | Don't even log |

Engine (`backtest/engine.py` line 122) multiplies risk units by
`signal.size_multiplier`. `TradeSignal` and `ExecutedTrade` carry
`.score` + `.size_multiplier` so journal/calibration scripts can
group results by score bucket.

**Strategies refactored to scoring**: IFVG (10 filters, 2 vetos +
8 scored=100pts) and RSI (10 filters, 3 vetos + 7 scored=100pts).
ARB and the 14 other strategies still on the binary stack pending
the calibration findings below.

### 5y/15m signal expansion at default params

| Family | Binary filtered | Scored: FULL | HALF | LOG | Total |
|---|---:|---:|---:|---:|---:|
| IFVG | 53 | 1,394 | 2,889 | 2,136 | **6,419** (~120×) |
| RSI  |  4 |    14 |    63 |   121 |    **198** (~50×) |

### Empirical score-vs-R calibration

[scripts/score_vs_r.py](../scripts/score_vs_r.py) runs a backtest
with scoring active and groups every closed trade by its score bucket,
reporting per-bucket n / win-rate / avg_R / PF. This is the empirical
calibration data for setting the 70/55/40 thresholds correctly.

**IFVG** — full pool-grid 5y/15m (864 param combos, n=15,488 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 3,628 | 34.2% | +0.026 | 1.05 |
| [60,70) | 5,852 | 23.5% | −0.371 | **0.51** |
| [70,80) | 4,176 | 44.0% | +0.244 | **1.48** |
| [80,90) | 1,764 | 30.4% | −0.170 | 0.77 |
| [90,100] |   68 | 76.5% | +0.863 | **4.43** |

**RSI** — 64-grid sample 5y/15m (n=1,092 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 311 | 53.7% | +0.014 | 1.03 |
| [60,70) | 494 | 32.8% | −0.265 | 0.64 |
| [70,80) | 251 | 29.1% | −0.375 | 0.49 |
| [80,90) |  36 | 19.4% | −0.691 | **0.16** |

### Findings

1. **IFVG score IS predictive but NOT monotonic.** [60,70) is a
   trough (PF 0.51), [70,80)+ recovers (PF 1.48), [90,100] is excellent
   (PF 4.43). Most likely a single scored filter is creating a
   "fake confluence" trap mid-range — top suspect is `htf_trend`
   `scored_three_way` awarding 8 partial-credit points when EMA-fast
   and EMA-slow are within 0.05·ATR of each other (i.e. flat HTF =
   no real trend, but counted as partial confluence). Options:
   - Remove partial-credit on `htf_trend` (full or zero).
   - Raise threshold to 70 (skip the trough entirely).
2. **RSI scoring is actively miscalibrated.** PF *declines*
   monotonically with score. High-score RSI signals are *worse*.
   Top suspects:
   - `htf_counter` (awards points when HTF is AGAINST trade
     direction) — sign or weight may be wrong.
   - `rsi_extreme` partial-credit at 40/60 is too lenient (60 is
     neutral, not "approaching overbought").
3. **Holdout-eval IFVG with scoring**: n=839 PF=0.84 — sample size
   now adequate (vs binary n=7) but PF<1 because the [60,70) drag
   bucket pollutes the population. Restricting to score≥70 would
   isolate the actual edge.

### Status

- Scoring infrastructure: **complete** (scoring.py, TradeSignal/
  ExecutedTrade fields, engine wiring, IFVG + RSI refactored).
- Tests: **280/281 pass** (1 pre-existing macro drift unrelated).
- Calibration: **partial** — IFVG empirically validated as
  predictive-but-non-monotonic; RSI weights need audit before
  threshold tuning.
- ARB + 14 strategies: rollout **deferred** until IFVG `htf_trend`
  trough is debugged and RSI weights are re-audited. Don't propagate
  the same calibration mistakes to every strategy.

### Scoring v2 — penalties + binary HTF + tightened extremes (2026-05-10, very late)

After the v1 calibration data showed RSI score ↔ PF was *inverse*
and IFVG had a [60,70) trough, three surgical fixes:

1. **`scoring.scored_penalty(name, penalty_points, predicate)`** —
   contributes negative points (without raising `max_score`) when the
   predicate trips.  Aggregator sums signed points; verdict uses
   `max(score, 0)` for classification.  Lets us punish "fake confluence"
   (e.g. trading against HTF) instead of just withholding credit.
2. **IFVG `htf_trend` → binary** with 0.10·ATR flat-zone (was 3-way
   20/8/0 at 0.05·ATR).  Plus new `htf_counter_penalty` (−20) when
   HTF actively opposes trade.  Total HTF spread: aligned +20 vs
   opposing −20 = 40 points.
3. **RSI `rsi_extreme` → binary** at strict oversold/overbought only
   (was 3-way at oversold / 40/60 partial bands).  RSI=40 in a
   parabolic gold market is a breather, not "approaching oversold".
4. **RSI `htf_counter` semantics FLIPPED** — empirical 5y data
   showed the textbook "reversal-against-HTF = max confluence" was
   *the cause* of inverse monotonicity.  Now: full points when HTF
   *aligned* with trade direction (continuation-divergence wins on
   dominantly-bullish XAUUSD).  Plus new `htf_against_penalty` (−15)
   when HTF opposes.

**v2 calibration on 5y/15m:**

IFVG full pool-grid (n=15,016 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 3,892 | 34.0% | −0.102 | 0.85 |
| [60,70) | 6,100 | 38.0% | +0.025 | **1.04** ← was 0.51 |
| [70,80) | 2,936 | 21.8% | −0.505 | **0.40** ← was 1.48 (bucket reshuffled) |
| [80,90) | 1,700 | 34.4% | −0.072 | 0.90 |
| [90,100] |  388 | 71.1% | +0.837 | **3.74**  (n grew 68 → 388 — actionable) |

RSI 64-grid sample (n=1,168 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 168 | 45.2% | −0.212 | 0.63 |
| [60,70) | 512 | 23.8% | −0.448 | 0.40 |
| [70,80) | 473 | 41.9% | +0.071 | **1.12** ← was 0.49 |
| [80,90) |  12 | 75.0% | +0.061 | **1.23** ← was 0.16 |
| [90,100] |   3 | 33.3% | −0.026 | 0.96 (n=3, noise) |

**Outcome:** RSI inverse monotonicity is **eliminated**.  IFVG top
bucket [90,100] grew from n=68 to n=388 — finally a statistically
actionable population.  Both families still show a [60,70) trough
but threshold=70 cleanly avoids it (and trades only the FULL_SIZE
verdict population).

Logs: [logs/option_e_v2/ifvg_score_vs_r_v2.log](../logs/option_e_v2/ifvg_score_vs_r_v2.log),
[logs/option_e_v2/rsi_score_vs_r_v2.log](../logs/option_e_v2/rsi_score_vs_r_v2.log).

---

## Recent: Option E filter framework first-look (2026-05-10, late)

**Built and tested.** `src/gold_trader/strategies/filters.py` is a new
shared filter primitive library: HTF EMA alignment, prior-swing-sweep
detection, displacement-min, gap-recency / unmitigated-gap, min swing
size, RSI extreme, confirmation-lag, session/hour windows, news,
weekend, DXY alignment, spread-relative, not-overextended. Each filter
returns `(passed: bool, reason: str)` for use with the new
`dump-signals` near-miss workflow.

`InversionFairValueGapStrategy`, `RsiDivergenceStrategy`, and
`AsianRangeBreakoutStrategy` now take a `filters_enabled: tuple[str,…]`
constructor field; defaults follow the CRITICAL+HIGH set from the
operator-checklist spec for each family. Tests: 280 passing
(1 pre-existing macro drift).

**5y/15m holdout-eval results with default filter sets:**

| family | unfiltered signals | filtered signals (5y) | holdout n | holdout PF | p | verdict |
|---|---:|---:|---:|---:|---:|---|
| `inversion_fair_value_gap` | 14,909 | 53 | 7 | 0.37 | 0.816 | **NOISE** — n too small + overfit (train PF 2.53) |
| `rsi_divergence`           |    186 |  4 | 1 |    — |    — | **INCONCLUSIVE** — n<5 |

**Diagnosis**: canonical/textbook filters at default thresholds reduce
signal counts by 99.6%/97.8% — below the n≥30 holdout gate. Three
options:

1. **Tune filter knobs via the optimizer** (most rigorous). Thread
   `swing_min_atr`, `sweep_lookback`, `gap_max_age_bars`,
   `impulse_min_atr`, `htf_minutes`, `confirmation_max_lag` through
   `experiments.py` params + `_factory_registry.py` + default grids.
   Then `holdout-eval` selects the train-PF-best combination subject
   to `n_train ≥ 30`. **Recommended next step.**
2. **Loosen canonical defaults manually.** e.g. `swing_min_atr=0.5`
   instead of `1.0`, `sweep_lookback=40` instead of `20`,
   `impulse_min_atr=1.0` instead of `1.5`.
3. **Replace canonical with operator-actual.** Original Option E
   intent: capture the *real* operator's mental checklist by reviewing
   their winning/losing trades — likely thresholds differ from the
   textbook.

**Working framework, untuned thresholds.** The plumbing is correct;
the filter library is reusable; toggling individual filters works
(`filters_enabled=("htf_trend",)` etc.). What needs work is making the
thresholds tunable instead of hardcoded.

**Remaining 14 strategies**: filter wiring not yet applied. Spec-
canonical answers for each are recorded in repo memory
(`/memories/repo/notes.md`); rollout deferred pending the threshold-
tuning question above so we don't mass-apply over-tight filters.

---

## Table of contents

1. [Elevator pitch](#1-elevator-pitch)
2. [Quickstart](#2-quickstart)
3. [Architecture](#3-architecture)
4. [Strategy library](#4-strategy-library)
5. [Data flow](#5-data-flow)
6. [Confluence + probability engine](#6-confluence--probability-engine)
7. [Web UI tabs](#7-web-ui-tabs)
8. [CLI cheat sheet](#8-cli-cheat-sheet)
9. [Research methodology](#9-research-methodology)
10. [Evidence thresholds](#10-evidence-thresholds)
11. [Empirical state](#11-empirical-state)
12. [MT5 runbook (manual one-time setup)](#12-mt5-runbook-manual-one-time-setup)
13. [Operational lifecycle](#13-operational-lifecycle)
14. [Risk + safety guards](#14-risk--safety-guards)
15. [Roadmap](#15-roadmap)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Elevator pitch

Autonomous XAUUSD research + live trading agent. Pure-stdlib Python on Linux.
Live execution via MT5-under-Wine bridge. Web UI for live monitoring,
research, and operator controls. Cron drives the loop. Research is
evidence-first: every family is gated on holdout PF, permutation p-value,
walk-forward stability, and conditional-probability slice expectancy
before it can place real orders.

---

## 2. Quickstart

```bash
# 1) one-time
bash scripts/setup_wine_mt5.sh        # Wine + MT5 + embedded Win Python
# (then perform manual MT5 GUI steps — see §12)

# 2) every day
bash scripts/start.sh                 # web UI on :8770 + bridge ping
# open http://127.0.0.1:8770

# 3) one time, to enable autonomous trading
bash scripts/install_cron.sh          # */15 agent-cycle + Sunday champion
```

Stop: `bash scripts/stop.sh`. Status: `bash scripts/status.sh`.

The Wine-side bridge runs in its own terminal (once per reboot):

```bash
bash ~/.gold-mt5-wine/start-bridge.sh
```

The web UI banner shows green/red so you always know whether live data and
order placement are reachable.

---

## 3. Architecture

```
                    ┌──────────────────────┐
                    │  data sources        │
                    │  · Dukascopy (hist)  │
                    │  · MT5 bridge (live) │
                    │  · FRED (macro)      │
                    └──────────┬───────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │  research                                    │
        │  · holdout-eval + permutation tests          │
        │  · pattern miner (FDR-corrected)             │
        │  · probability slicer (per-regime stats)     │
        │  · weekly champion selector (Sundays)        │
        └──────────────────────┬───────────────────────┘
                               │ config/champion.json
                               │ config/probability_tables/*.json
                               ▼
        ┌──────────────────────────────────────────────┐
        │  agent-cycle (cron */15)                     │
        │  · regime tags + macro filter + news gate    │
        │  · probability gate (slice-conditional)      │
        │  · risk + kill-switch + tick-age watchdog    │
        │  · place market or pending order via bridge  │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │  observability                               │
        │  · web UI (live chart + zones + confluence)  │
        │  · paper_state.json + trade_journal.csv      │
        │  · execution drift + filter-lift stats       │
        └──────────────────────────────────────────────┘
```

### Repository layout

```
src/gold_trader/
├── strategies/            17 families (incl. rsi_divergence + IFVG)
├── research/
│   ├── experiments.py     parameter dataclasses + default grids
│   ├── _factory_registry  family_name -> Strategy instance
│   ├── family_grids.py    SELF_CONTAINED_FAMILIES + family_spec()
│   ├── holdout.py         train/holdout + WF + permutation
│   ├── permutation.py     sign-randomization
│   ├── pattern_miner.py   FDR-corrected combo mining
│   ├── probability_slicer.py   per-regime conditional stats
│   ├── features.py        69 base features
│   ├── macro_features.py  50 macro features (no lookahead)
│   └── parallel_search.py ProcessPoolExecutor + governor
├── live/                  mt5_broker + bridge_server + bridge_client
├── infra/                 risk guards + resource governor
├── web/                   stdlib HTTP server + SPA + lightweight-charts
├── data/                  Dukascopy + FRED + resampling + calendar
├── paper/                 paper trading state machine
├── reports/dashboard.py   static HTML report
├── regime.py              RegimeDetector (8 tags)
├── macro_filter.py        MacroDecisionFilter (allow/warn/block)
├── probability_gate.py    slice-conditional gate (allow/block/no_table)
├── zones.py               FVG / IFVG / swings / PDH-L / Asian zones
├── confluence.py          multi-TF zone clustering + scoring
├── calendar.py            NewsCalendar + blackout window
└── journal.py             append closed trades to CSV
scripts/
├── start.sh / stop.sh / status.sh    one-shot lifecycle
├── install_cron.sh                   idempotent cron installer
├── run_agent_cycle.sh                cron entry point
├── weekly_champion.py                Sunday champion selector
├── setup_wine_mt5.sh                 one-time MT5+Wine bootstrap
├── update_journal.py                 appends closed trades + regime tags
├── execution_drift.py                drift analyser
└── paper_stats.py                    filter-lift + promotion gate
config/
├── runtime_config.json               UI-driven flags
├── champion.json                     this week's active families
└── probability_tables/<family>.json  per-family slice tables
```

---

## 4. Strategy library

**17 families.** Two are direct codifications of the operator's profitable
real edges (in **bold**).

**Self-contained** (run from any CSV):

`asian_range_breakout`, `asian_range_fade`, `compression_breakout`,
`fair_value_gap`, **`inversion_fair_value_gap`**, `liquidity_sweep`,
`london_breakout`, `momentum_burst`, `ny_close_compression`,
`ny_session_breakout`, `opening_range_breakout`, `previous_day_breakout`,
**`rsi_divergence`**, `session_continuation`, `trend_pullback`

**External-data:**

- `dxy_lead_lag` (DXY column required — populate via `merge-dxy` CLI)
- `real_yield_reversal` (FRED macro cache required — `sync-macro` CLI)

### Operator's two edges (bold above)

- **`rsi_divergence`** — Wilder RSI(14), centred-pivot detection
  (no lookahead), regular bullish divergence (price LL + RSI HL with
  RSI(B)<oversold) confirmed by hammer OR bullish engulfing → LONG;
  bearish mirror → SHORT. Stop = prior swing extreme ± `stop_buffer_atr × ATR`.
  `risk_reward=2.0`.
- **`inversion_fair_value_gap`** — ICT-style: detect 3-bar FVG, wait for an
  inversion bar that closes through the FVG level, then retest from the
  opposite side → entry on close back in inverted direction; stop at
  inverted-zone far edge ± `buffer × ATR`.

---

## 5. Data flow

| Stream         | Source         | Destination                                     |
|----------------|----------------|-------------------------------------------------|
| Historical 1m  | Dukascopy      | `data/<dataset>/*.csv` (auto on demand)         |
| Live ticks     | MT5 bridge     | bridge buffer (1 Hz, ~2 h ring)                 |
| Live candles   | MT5 bridge     | UI `/api/live/candles` — **bridge first** when online; CSV only in preview/offline (`prefer_cache=1`) |
| Macro (FRED)   | FRED           | `data/macro/<series>.csv` (`sync-macro` CLI)    |
| Trades         | agent-cycle    | `data/{agent_live,live}_xauusd/paper_state.json`|
| Journal        | update_journal | `logs/trade_journal.csv`                        |

### Syncable timeframe rule

Don't mix separately downloaded 5m / 15m / 1h files. Download one canonical
1m base, resample upward with fixed UTC bucket boundaries, and only compare
signals across timeframes that came from the same base dataset.

### Macro bundle (FRED)

`us10y, us2y, real10y, real5y, bei10, vix, dxy, spx, usdjpy, usdcny,
fedfunds, wti, brent` (13 series; `gold_lbma` retired by FRED).
`tedspr` discontinued; soft-fails. Cached in `data/macro/`.

---

## 6. Confluence + probability engine

### Probability slicer (`research/probability_slicer.py`)

Stops asking _"does the strategy work overall?"_ and asks _"under which
conditions does it work, with what n and lower-bound expectancy?"_

`compute_probability_table(bars, strategy, config)` runs the strategy and
tags every closed trade by 12 dimensions at entry time:

- **session** (asia/london/ny), **dow**, **hour_bucket**
- **vol_pct**, **trend**, **compression**, **spread**, **session_vwap**
- **macro_real10y**, **macro_dxy**, **macro_vix**
- **side**

For each single-dim and 2-dim joint slice it reports
`n, win_rate, avg_r, expectancy, profit_factor, lower_ci_r`.

`lookup_slice_probability(table, current_dims)` returns the most-specific
edge slice (pair > single, ties broken by `expectancy × √n`).

CLI: `slice-probabilities <csv> --family <name|all>` writes
`config/probability_tables/<family>.json`.

For selective strategies (rsi_divergence, IFVG) where a single param set
fires < 20 times across the full dataset, add `--pool-grid` to run every
grid combo and pool. Pooling deduplicates trades sharing
`(entry_time, side)` by averaging R, so the resulting `n` is the count
of unique physical signals (not param-combo-multiplied duplicates).

**`min_n` floor (2026-05-09):** raised from 10 → 20 in both
`edge_slices()` and `lookup_slice_probability()`. n=5 slices with
PF=∞ are not edges, they're noise. The probability gate now returns
`no_table` rather than `allow` for any candidate whose best matching
slice has n < 20.

**Demo:** `compression_breakout` overall PF=1.18, avg_r=+0.086 (looks
break-even). Slice `spread=normal`: n=5, PF=∞, avg_r=+1.016, lower-CI=+0.434.
The entire edge lives in one regime — but n=5 means the gate now reports
`no_table`. The demo is illustrative; not yet a tradeable slice.

### Probability gate (`probability_gate.py`)

Wired into `agent-cycle` after the macro filter and before order placement.
Gated by `GOLD_PROBABILITY_GATE = off | soft | hard`. Verdicts:
`allow | block | no_table`.

### Zones (`zones.py`)

Strategy zones with status tracking:

- **FVG / IFVG** — 3-bar imbalance gaps and their inversions
- **Swing pivots** — centred, no lookahead
- **PDH / PDL** — most recently completed UTC day
- **Asian range** — last contiguous block of `session=='asia'` bars within 12 h

Each zone has `status ∈ {pending, active, mitigated, invalidated}`.
Mitigated = wick touched; invalidated = closed past the opposite side.

### Confluence (`confluence.py`)

`score_confluence(zones_by_tf, tolerance, ...)` clusters overlapping
same-side zones across M1→W1 with union-find, then scores each cluster:

```
score = Σ (tf_weight × status_weight × age_decay)
        × (1 + 0.25·family_diversity_bonus)
        × (1 + 0.10·kinds_diversity_bonus)
```

Default TF weights:
`1m=0.30 · 5m=0.45 · 15m=0.65 · 60m=0.85 · 240m=1.10 · D1=1.40 · W1=1.70`.

Mitigated zones contribute half weight; invalidated zones drop out. Zones
older than `max_age_bars` decay with a half-life. Endpoint
`/api/live/confluence?timeframes=15,60,240&tolerance=0.5`.

---

## 7. Web UI tabs

Default home is **Live**. Advanced research tabs are collapsed.

| Tab          | Content                                                          |
|--------------|------------------------------------------------------------------|
| Live         | MT5 candle chart + zone overlays, account, open position, manual close, ARM/PAUSE |
| Dashboard    | KPIs, equity sparkline, recent trades, news countdown            |
| Charts       | Historical candles + EMA/VWAP overlays                           |
| Bridge       | Bridge URL/secret/symbol; ping; force-close current position     |
| Journal      | Closed trades with regime tags + filter verdicts                 |
| Risk         | Equity curve, max-DD, kill-switch arm/disarm                     |
| Logs         | `agent.log` / `web.log` / `champion.log`                         |
| Settings     | Bridge URL, secret, symbol, macro filter, news blackout          |
| Strategy Lab | Launch holdout-eval / permutation-test on any family             |
| Pattern Miner| Run `mine-all` from the UI; browse survivors                     |
| Macro        | FRED series viewer                                               |
| Replay       | Trade markers overlaid on candle chart                           |

Live-tab zone overlay: each zone is rendered as horizontal price-lines on
the candle series (FVG↑/↓ green/red, IFVG↑/↓ blue/purple, swings amber,
PDH/L grey, Asian H/L cyan). Mitigated → dashed thinner; invalidated →
hidden. Toggle next to EMA.

UI default port: `8770` (chosen so it doesn't collide with the MT5 bridge
default `8765`).

---

## 8. CLI cheat sheet

All commands run as
`PYTHONPATH=src .venv/bin/python -m gold_trader.cli <subcommand>`.

```
sync-dukascopy --days 30 --output-dir data/recent30/   # fresh OHLCV
sync-macro                                             # FRED bundle
holdout-eval <csv> --family rsi_divergence             # evaluate one family
holdout-eval <csv> --family X --quick                  # 64-grid, no WF, 500 perms (~20s)
holdout-eval <csv> --family X --grid-sample 256 --skip-walk-forward
slice-probabilities <csv> --family <name|all>          # build probability tables
slice-probabilities <csv> --family rsi_divergence --pool-grid   # pool selective strategies
mine-all <csv> --timeframes 15,60,240 --horizons 4,8,16
dump-signals <csv> --family X --output reports/dumps/X.csv
dump-signals <csv> --family X --output ... --pool-grid # dedupe by (entry_time, side)
holdout-mined-pattern <csv> --features "hour_o7,range_q0" --direction long
permutation-test <csv> --family ...                    # standalone p-value
agent-cycle ... --families "...,..."                   # one live cycle
serve --port 8770                                      # web UI
broker-info                                            # live broker introspection
panic                                                  # flatten + arm kill switch
dashboard <paper_state.json> --output reports/dashboard/index.html
```

Tests: `PYTHONPATH=src .venv/bin/python -m pytest -q` — **421 passing**
(1 failed pre-existing macro-bundle drift, 1 skipped).

---

## 9. Research methodology

### Non-negotiable rules

1. No live automation before out-of-sample edge is proven.
2. LLMs may explain and summarise but must not decide entries or exits.
3. Every feature must have a timestamp-safe definition.
4. Every backtest must include spread, slippage assumptions, and risk sizing.
5. Broker state always wins over internal assumptions in live phases.

### Pipelines

- **Holdout-eval** — 75/25 train/holdout split. Parallel grid search on the
  training slice with `parallel_best_params()`; permutation significance
  test (2,000 permutations) on the held-out slice; true walk-forward on
  the train slice (params re-fit per window).
- **Permutation test** (standalone) — sign-randomization, default 10,000
  shuffles. Returns `SIGNAL / WEAK / MARGINAL / NOISE / INCONCLUSIVE`.
- **Pattern miner** — 119-feature vocabulary (69 base + 50 macro). Mines
  1- and 2-feature conjunctions on train slice; centred moving-block
  bootstrap p (block ≈ 4 h); Benjamini–Hochberg FDR at q=0.10; holdout
  re-eval; survivors sorted by `|holdout_mean_r|`.
- **Probability slicer** — see §6.
- **Weekly champion selector** — Sundays 22:00 UTC, auto-fetches last 30
  days from Dukascopy, evaluates every self-contained family, writes
  ranked `config/champion.json`. `active_families_csv` feeds the next
  cron cycle; falls back to defaults if no family clears the gates.

### Engine realism

- `commission_per_trade = $10/round-trip` active in all evaluations.
- Opt-in `slippage_bps` (adverse on entry + exit) and `fill_aware_stops`
  (stop AND target translate by the entry drift).
- **Engine quirk**: signal stop/target are computed at `bar[i].close` but
  entry is `bar[i+1].open`. Strategies can set `risk_reward > 0` so the
  engine recomputes target from actual fill (preserves structural stop).
  Currently used by `asian_range_breakout` and `dxy_lead_lag`.

---

## 10. Evidence thresholds

A family graduates from research to paper only when **all** are satisfied:

1. Positive expectancy after realistic costs.
2. Holdout permutation **p ≤ 0.20** on a 75/25 split over ≥ 12 months.
3. Holdout **PF ≥ 1.20** on the sealed test period.
4. Drawdown inside a predeclared budget (max **15%** on holdout).
5. Holdout **trade count ≥ 30**.
6. Walk-forward **positive-window ratio ≥ 40%**.

For paper → live promotion, additionally:

7. **n ≥ 30 closed paper trades.**
8. Macro-filter **allow-vs-block delta ≥ +0.10R** (validates regime gate).
9. **Execution drift |E − R| ≤ 0.05R** average (validates broker geometry).

---

## 11. Empirical state

### Scoring redesign — first calibration data (2026-05-10, very late)

After the binary filter framework collapsed signal counts (IFVG 14,909→53,
RSI 186→4 — see further below), IFVG and RSI were rebuilt onto a
three-tier scoring stack (see top-of-file "Recent" section for the
architecture). Score-vs-R per bucket on `data/xauusd_5y/xauusd_5y_15m.csv`:

**IFVG** — full pool-grid (864 combos, n=15,488 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 3,628 | 34.2% | +0.026 | 1.05 |
| [60,70) | 5,852 | 23.5% | −0.371 | **0.51** |
| [70,80) | 4,176 | 44.0% | +0.244 | **1.48** |
| [80,90) | 1,764 | 30.4% | −0.170 | 0.77 |
| [90,100] |   68 | 76.5% | +0.863 | **4.43** |

**RSI** — 64-grid sample (n=1,092 trades):

| bucket | n | win% | avg_R | PF |
|---|---:|---:|---:|---:|
| [55,60) | 311 | 53.7% | +0.014 | 1.03 |
| [60,70) | 494 | 32.8% | −0.265 | 0.64 |
| [70,80) | 251 | 29.1% | −0.375 | 0.49 |
| [80,90) |  36 | 19.4% | −0.691 | **0.16** |

Findings: IFVG score is predictive but non-monotonic with a [60,70)
trough (likely `htf_trend` partial-credit awarding 8pts on near-flat
EMAs creating "fake confluence"). RSI scoring is *miscalibrated* —
PF declines with score; suspect `htf_counter` weight sign and
`rsi_extreme` partial bands. Holdout-eval IFVG with scoring active:
n=839 PF=0.84 — sample size now adequate (vs binary n=7) but
[60,70) drag pollutes the PF. Tooling: [scripts/score_vs_r.py](../scripts/score_vs_r.py),
[logs/option_e_v2/](../logs/option_e_v2/).

### Currently proven on the 15-month dataset

- **None.** `asian_range_breakout` was the long-standing single passing
  family but degraded to PF=1.07, p=0.385 on the latest re-run. Earlier
  PF=1.298 was on a smaller window.

### 5-year operator-edge re-evaluation (2026-05-10, 15m, 118,292 bars)

Re-ran `holdout-eval` on the full 5-year Dukascopy bundle
(`data/xauusd_5y/xauusd_5y_15m.csv`, 2021-05-04 → 2026-05-04, 75/25
split = 78,861 train / 39,431 holdout).

| family | grid | train PF | holdout n | holdout PF | avg_R | p | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| `rsi_divergence`           | 384 | 0.86 |   143 | 0.64 | −0.22 | 0.994 | **FAIL — NOISE** |
| `inversion_fair_value_gap` | 384 | 0.75 | 1,330 | 0.83 | −0.16 | 0.999 | **FAIL — NOISE** |

Reading: with **5× more holdout data** the verdict didn't move toward
"signal" — it hardened toward NOISE. The RSI 15-month MARGINAL (p=0.175,
n=6) does **not** generalise: at n=143 the same logic produces
PF=0.64/p=0.994. **More data did not rescue these rules; it falsified
them.**  This puts both families squarely in "rebuild" territory and
removes "we just need more data" as an excuse.

Verdict instability across windows (PF=3.20 on n=6 → PF=0.64 on n=143)
is the signature of a noise-driven number — see lesson #9 below.

### 5-year pattern miner (2026-05-10)

Ran `mine-all` across `tf ∈ {15m, 60m, 240m} × horizon ∈ {4, 8, 16}` with
the 5y bundle. Wall=6m9s, 12 workers, 36m47s of CPU.

- 15m: 104+198+211 = **513** FDR survivors (h=4/8/16)
- 60m:  78+0+0 = **78** survivors (h=8 and h=16 emit zero — the
  `min_signals=80` floor is too high vs. the 29,590 60m bars at longer
  horizons; lower the floor for sparse-tf runs in future)
- 240m: 0 survivors

Cross-TF replication (same `(features, direction)` cell appearing on ≥ 2
distinct timeframes with consistent sign) → **79 cross-TF replicators**
in `reports/mined_patterns/5y_sweep/cross_tf_replicators.csv`. Top
themes are tightly clustered around **late NY → Asia open bullishness on
XAUUSD**:

| rank | features | dir | mean R | p   | tfs |
|-----:|---|---|---:|---:|:--|
| 1 | `doji_body & hour_o7`           | long | +0.554 | 0.002 | 15m,60m |
| 2 | `hour_q3 & month_q0`            | long | +0.528 | 0.002 | 15m,60m |
| 3 | `hour_o6 & month_q0`            | long | +0.495 | 0.002 | 15m,60m |
| 4 | `hour_o7 & range_q0`            | long | +0.493 | 0.002 | 15m,60m |
| 5 | `hour_o7 & inside_bar`          | long | +0.487 | 0.002 | 15m,60m |
| 6 | `hour_q3 & range_q0`            | long | +0.449 | 0.002 | 15m,60m |
| 7 | `body_q3 & hour_o6`             | long | +0.448 | 0.002 | 15m,60m |
| 8 | `hour_o7 & ret5_flat`           | long | +0.439 | 0.002 | 15m,60m |
| 9 | `hour_q3` (alone)               | long | +0.411 | 0.002 | 15m,60m |
|  ~25 | `close_above_ema20 & month_q3` | long | +0.177 | 0.002 | 15m,60m,240m |

Feature semantics:
- `hour_q3` = 18:00–24:00 UTC; `hour_o6` = 18:00–21:00; `hour_o7` =
  21:00–24:00 (i.e. last NY hour into Sydney/Tokyo open).
- `range_q0` = lowest range bucket (compression bars).
- `month_q0` = Jan–Mar (calendar Q1).

This is a substantially firmer signal base than the 15-month miner: p≤0.002
across all of the top, and sign-stable across distinct timeframes — *not*
the kind of noise we saw before.

### Mined patterns → trade rules — holdout verdict (2026-05-10)

Tested whether the strong forward-R edges actually survive stop/target
conversion. New tooling:

- `MinedPatternStrategy` (`src/gold_trader/strategies/mined_pattern.py`)
  — generic conjunction-of-features → ATR-stop trade rule.
- `holdout-mined-pattern <csv> --features ... --direction ...` CLI —
  inline 75/25 holdout with a small (stop_atr × RR) train sweep,
  permutation test on holdout, gates from §10.

Top 5 cross-TF survivors run on `xauusd_5y_15m.csv`, grid =
`stop_atr ∈ {0.5, 1.0, 1.5, 2.0} × RR ∈ {1.0, 1.5, 2.0, 3.0}`:

| pattern (long) | best train PF | train n | holdout n | holdout PF | p | verdict |
|---|---:|---:|---:|---:|---:|---|
| `hour_q3`                  | 0.84 |  37 |  31 | 1.29 | 0.274 | FAIL — p>0.20 |
| `hour_q3 & month_q0`       |  —   |  ≤23 |  —  |  —   |  —    | FAIL — too sparse for n_train≥30 |
| `dow_thu & hour_q3`        | 0.97 |  48 |  30 | 1.15 | 0.363 | FAIL — PF<1.20 |
| `doji_body & hour_o7`      | 0.92 |  43 |  55 | 1.27 | 0.203 | FAIL — p>0.20 |
| `hour_o7 & range_q0`       |  —   |  ≤28 |  —  |  —   |  —    | FAIL — too sparse for n_train≥30 |

**Smoking gun**: across all 5 patterns, **the best in-sample (train) PF
is 0.97 — i.e. the optimizer cannot even find a stop/target combination
that breaks even on the same data the rule was discovered on**. The
forward-R edge of +0.5 over 8 bars is real, but the *path* from entry to
+8 bars is statistically too volatile relative to the available stop
distances: any tight stop is hit too often; any wide stop yields a
target that is rarely reached before the time-out.

This is the empirical confirmation of HANDBOOK lesson #2, now at 5×
the data:

> **Forward-R surfaces and trade-rule surfaces are fundamentally
> different statistical objects.** A 5y/p=0.002 forward-R signal does
> not imply a tradeable edge. Direction is real, *executable PF is not*.

Outputs:
- `reports/mined_patterns/5y_sweep/all_survivors.csv` (738 rows)
- `reports/mined_patterns/5y_sweep/cross_tf_replicators.csv` (79 rows)
- `logs/5y/mine_all.log`, `logs/5y/mined_holdout.log`
- `reports/pattern_dumps/{rsi_divergence,ifvg}_5y_15m.csv` — every
  unique signal each operator-edge rule fires on the 5y/15m base
  (71 and 99 unique respectively; both base-PF<1).

### Operator-edge holdout evaluation (2026-05-09, 15m, 28,844 bars)

Run command (after the speed fix below):

```
PYTHONPATH=src .venv/bin/python -m gold_trader.cli holdout-eval \
  data/xauusd_full_15m.csv --family <name> \
  --grid-sample 256 --skip-walk-forward --n-permutations 1000 --workers 12
```

| family | n (holdout) | holdout PF | p | verdict |
|---|---:|---:|---:|---|
| `rsi_divergence`            |   6 | 3.20 | 0.175 | MARGINAL — directionally good, underpowered |
| `inversion_fair_value_gap`  | 329 | 0.77 | 0.987 | FAIL — pure noise, must be rebuilt |

Reading:

- **IFVG fails decisively.** n=329 is plenty of statistical power; PF<1 and
  p≈1 means the systematic implementation does not capture the operator's
  discretionary IFVG edge. Likely causes: entry trigger too literal
  (3-bar gap → inversion close → opposite-side retest), discretionary
  filters not encoded, timeframe mismatch.
- **RSI divergence is suggestive but underpowered.** PF=3.20 is high,
  p=0.175 is not significant at the n=6 level over 9,615 holdout bars
  (~10 months). Need wider entry conditions to grow n into the n≥30 range
  where a verdict is meaningful.

Best params found (for reference / next iteration, not for trading):

- RSI: `rsi_period=10, atr_period=14, RR=2.5, oversold=30, pivot_window=3,
  pivot_lookback=60, min_pivot_separation=6, stop_buffer_atr=0.2`
- IFVG: `atr_period=14, RR=2.0, min_gap_atr=0.05, fvg_lookback=20,
  inversion_lookback=10, retest_lookback=10, stop_buffer_atr=0.2`

### Holdout-eval performance fix (2026-05-09)

`rsi_divergence._rsi_series` was O(N²) per backtest — rebuilding RSI from
bar 0 on every signal call. Cached as O(N) sweep keyed by
`(id(bars), len, period)`; same for ATR. Combined with new CLI flags
(`--grid-sample N`, `--skip-walk-forward`, `--quick`, plan banner),
holdout runtime dropped from **40+ min → ~20 sec** on the same dataset
with identical numerical output.

### Pooled probability tables (2026-05-09)

After widening the RSI grid (oversold ∈ {30, 35}, pivot_separation ∈
{3, 5, 7}, pivot_lookback ∈ {30, 45, 60}) the holdout n moved from 6 → 7
— statistical noise. The grid-optimizer still picks canonical strict
params because they maximize train PF, regardless of train trade count.
Conclusion: **the rule logic, not the parameters, is what's missing**.

To populate slice tables despite per-config selectivity, added
`slice-probabilities --pool-grid` which runs every grid combo and
collapses trades sharing `(entry_time, side)` into a single sample with
the **mean** R across variants (deduplicates the same physical signal
appearing 50× under different RR/stops). Honest unique-signal count on
`data/xauusd_full_15m.csv`:

| family | unique signals | base PF | edge slices (n≥20, pf≥1.10, exp≥0.05R) |
|---|---:|---:|---:|
| `rsi_divergence`           | 70 | 0.33 | 0 |
| `inversion_fair_value_gap` | 59 | 0.70 | 0 |

Even the IFVG slice tops (NY/12-18/flat-trend, n≈24, pf≈1.4) have
negative lower-CI bounds — not statistically distinguishable from
chance. **Conclusion: 15 months × the current rule logic cannot
populate a defensible probability gate.** The path forward is rule
refactor + operator-journal ground truth (see [§15](#15-roadmap)),
*not* more permutations or more grid widening.

### Pattern miner — significant forward-return effects

The miner finds robust forward-return patterns (FDR-corrected, replicating
across timeframes), but converting them to stop/target trade rules
collapses the edge. Major themes documented:

- `near_20_high & trend_up` — momentum continuation (overall theme; failed
  holdout when converted to rules)
- `macro_real10y_5d_down & macro_vix_5d_flat` — falling real yields + calm
  vol (avg_R=+1.89 forward 16 bars, p=0.002)
- `macro_dxy_20d_flat & trend_up` — multi-week dollar pause + trending gold
- `dow_fri & macro_dxy_5d_flat` — Friday DXY pause
- `hour_q3` — UTC 18–24 (late NY → Asian open) bullish on XAUUSD
- `bei10_hi & real10y_lo` — stagflation regime is gold-bearish for longs

### Lessons learned

1. **Single-strategy validation is not enough.** Same entry behaves
   differently across regimes — tag and slice instead of optimise.
2. **Pattern-miner edges don't survive stop/target conversion.** Forward-R
   surfaces and trade-rule surfaces are different statistical objects.
3. **Single-strategy view obscures conditional edges.** Sliced analysis
   surfaces hidden regimes (see §6 demo).
4. **Holdout-grid-search overfits when training has < ~80 trades.**
5. **Don't build research-tooling UIs first.** Build the live operator
   view first; collapse research tabs by default.
6. **Profile before scaling permutations.** A single missing cache turned
   a 20-second run into a 40-minute one. Always emit a workload preview
   (`[plan] grid=… wf_windows=… approx_train_backtests=…`) before
   committing CPU.
7. **n=5 PF=∞ is not an edge, it's noise.** Slice tables and probability
   gates default to `min_n=20` (raised from 10 on 2026-05-09). Below
   that threshold, return `no_table` rather than `allow`. Pooled grid
   tables must dedupe by `(entry_time, side)` to avoid the same physical
   signal counting once per param combo.
8. **Widening parameters cannot rescue a wrong rule.** RSI divergence on
   15m grew from 6 → 7 holdout trades after the grid widening because
   the optimizer still selects strict params for max train-PF. When
   widening doesn't move n, the rule itself doesn't fire on the
   discretionary signal — refactor the rule, don't tune it.
9. **More data hardens, doesn't rescue.** 15-month → 5-year (5×) on the
   same operator-edge rules: RSI `n=6 PF=3.20 p=0.175` (MARGINAL) →
   `n=143 PF=0.64 p=0.994` (NOISE). IFVG: `n=329 PF=0.77` (NOISE) →
   `n=1330 PF=0.83 p=0.999` (NOISE). When verdict instability collapses
   *toward* noise as n grows, the original "marginal" was an artifact
   of low n, not an underpowered edge.  Equally: a strong forward-R
   pattern (mean R=+0.5 over 8 bars at p=0.002 across 2 timeframes,
   5y) can still produce best-train-PF=0.97 once translated into stop
   /target — the *path* from entry to +N bars is dominated by noise
   at scales where any usable stop sits. **Discover candidates with
   the miner; never trade them without a holdout-mined-pattern PASS.**
10. **Binary filter pass/fail at canonical thresholds is overfitting
    on a tiny sample.** IFVG canonical filters reduced 14,909 signals
    → 53 (99.6%); RSI 186 → 4 (97.8%) — both below the n≥30 holdout
    gate. Fix: three-tier scoring (UNIVERSAL_VETO / STRATEGY_VETO /
    SCORED) with verdict bands (FULL/HALF/LOG/REJECT) and size
    multipliers (1.0/0.5/0.0/0.0). Lets every signal contribute
    something — full size at A-grade, half at B-grade, log-only at
    C-grade — and lets the empirical score-vs-R distribution
    calibrate the thresholds. *Caveat:* scoring weights themselves
    can be miscalibrated (RSI 2026-05-10 showed inverse PF-vs-score)
    so always validate per-bucket PF before relying on the score
    threshold; non-monotonic buckets (IFVG [60,70) trough) reveal
    individual filters whose partial-credit rules are creating "fake
    confluence" — fix the filter, not the threshold.

We're at a clean decision point. Every front-tested rule (operator edges
+ top 5 mined survivors at 5y) returns "no edge". The next move is no
longer "more permutations" — it's choosing which qualitatively different
bet to make.

**Diagnosis.** The miner certified that the *time* and *direction*
contain real forward-return predictability (p≤0.002, sign-stable across
TFs at 5y). Holdout-eval tells us the trigger logic is wrong. The
operator trades RSI divergence and IFVG profitably by hand — so the
underlying edge is real, *the systematic encoding is missing the
discretionary filters*. The mechanical rules check 3 conditions; the
human brain runs a 20-step unconscious checklist. PF=0.83 on n=1,330
is exactly what you'd expect when you fire on every signal the human
would have rejected.

> **Forward-return signal exists. Trigger logic is the variable. Fix
> the trigger before doing anything else.**

Five options, in priority order:

#### E. Encode the operator's discretionary checklist  *(NEW — top priority)*

The operator already runs a profitable mental checklist for IFVG and
RSI divergence. The job is *translation*, not discovery.

**Process:**

1. **Manual sample (2–3 h, no code).** Pull the chart. Pick 10 IFVG
   trades that made money — for each, write down every reason it was
   taken (sweep before gap, HTF aligned, session, displacement,
   confluence, …). Pick 5 IFVG signals the code currently fires on
   that you would have rejected — write down why. The delta is the
   missing filter set. Repeat for RSI divergence.
2. **Encode filters one at a time.** Each filter is an independently
   toggleable boolean returned by the strategy alongside the existing
   trigger. Wire each through `dump-signals` so we can see exactly
   which filter killed which signal.
3. **Manual signal inspection on history.** Run `dump-signals` on the
   filtered strategy. Visually inspect the first 30 signals it fires.
   If any of them look like garbage, the filters aren't tight enough —
   tighten before wasting a holdout-eval.
4. **Holdout-eval.** Only after manual inspection looks clean. Target:
   IFVG n drops from 1,330 → 50–150, PF rises above 1.20. RSI: n
   grows from 6 → 80–150 with meaningful PF.

**Candidate IFVG filters** (must be ranked + confirmed by operator
during step 1):

- *Liquidity sweep before the FVG* — within the last N bars, did price
  sweep a prior swing high/low before the impulse that created the
  gap? (ICT core concept; not currently checked at all.)
- *HTF trend alignment* — only bullish IFVGs in 4H/D bullish trend;
  only bearish in bearish. Counter-trend IFVGs at HTF resistance are
  traps.
- *Session filter on FVG formation* — the gap must have *formed*
  during London or NY, not just the entry. Asian-session gaps are
  low-quality.
- *Freshness* — only trade FVGs formed within the last 8–12 bars on
  the execution TF.
- *Displacement minimum* — the candle(s) that created the FVG must be
  impulsive, ≥ 1.5× ATR. Slow drifts that happen to leave a gap are
  not the same structural event.

**Candidate RSI-divergence filters:**

- *Minimum swing size* — price low at point B must be ≥ 1.0× ATR
  below the prior 20-bar low. Minor wiggles don't count.
- *Genuine oversold at B* — RSI(B) < 35 absolute, not just RSI(A) > RSI(B).
- *Confirmation candle timing* — hammer/engulfing must occur *at* the
  swing low, not several bars later.
- *HTF context* — block bullish divergences when 4H trend is clearly
  down.

**Why this leapfrogs A/B/D.** A/B/D are exploratory — they ask "is
there an edge anywhere in this dataset?" E is *executable* — we know
the edge exists (operator P&L proves it); we're just translating it.
Higher prior, faster path to a tradeable system.

#### A. Time-exit instead of stop/target

The miner says **+0.5 R at exactly +8 bars**; the holdout says no
usable stop/target. Together: **the edge is in the time domain, not
the price domain**. Action: extend `MinedPatternStrategy` +
`holdout-mined-pattern` with `--exit-mode horizon --horizon-bars N`
(skip stops, exit at `bar+N`'s open). Re-run the same 5-pattern set.
Useful complement to E if the miner survivors turn out to overlap
with operator setups; otherwise lower priority than E.

#### B. Short-side miner pass + macro-conjunction holdout

Every 5y cross-TF top is long — directional sampling artifact. Re-run
`mine-all` with `--with-macro` on the 5y bundle. Useful as a parallel
exploration but does not benefit from the operator-P&L prior that E
has, so likely lower hit rate.

#### C. Operator journal as ground truth  *(runs in parallel, always)*

Log every discretionary trade taken (entry/exit/reason) in
`data/operator_trades/`. Once n ≥ 30, fit slice tables on those
instead of mechanical-rule trades. **C is the long-term version of E:
E encodes the operator's *checklist*; C measures the operator's
*outcomes*. They feed each other.** Should run regardless of E/A/B
status.

#### D. Lower the miner's `min_signals` floor  *(cleanup)*

Two-line patch in `pattern_miner.py`. Do it before the next miner
sweep but don't expect a different species of survivor.

#### Recommendation order

**E → C-in-parallel → D before next miner sweep → A → B.**

E is the move with the highest prior of producing a tradeable system,
because it inherits the operator's existing profitability rather than
trying to discover an edge from scratch. The interview (step 1 of E)
is happening now in the conversation; the encoding (step 2) follows
once the checklist is captured.

---

## 12. MT5 runbook (manual one-time setup)

Live execution runs MT5 inside Wine on the same host. Bridge speaks HTTP
with shared-secret auth.

### Pre-flight

- Ubuntu 24.04
- MT5 demo account credentials (login / password / server)
- ~3 GB free disk

### Step 1 — bootstrap

```bash
cd "/home/lesnar/Documents/Gold trader"
./scripts/setup_wine_mt5.sh
```

Installs Wine + winetricks, builds isolated `WINEPREFIX` at
`~/.gold-mt5-wine`, downloads MT5 + embeddable Win Python 3.11 +
`MetaTrader5` pkg + `numpy<2` (Wine 9.x ucrtbase quirk), generates
`start-bridge.sh`. Idempotent.

### Step 2 — install MT5 (GUI)

```bash
WINEPREFIX="$HOME/.gold-mt5-wine" wine ~/.gold-mt5-wine/mt5setup.exe
```

Cancel the **Open Account** dialog (we log in manually next).

```bash
WINEPREFIX="$HOME/.gold-mt5-wine" wine "C:/Program Files/MetaTrader 5/terminal64.exe" &
```

If `terminal64.exe` is elsewhere: `find ~/.gold-mt5-wine -name 'terminal64.exe'`.

### Step 3 — log into broker (inside MT5)

File → Login to Trade Account → enter demo login / password / server
(e.g. `XMGlobal-MT5 6`). Wait for the bottom-right ping to go green.

### Step 4 — add GOLD to Market Watch

`Ctrl+M` → Right-click → Symbols → find `GOLD` (or `GOLDmicro` etc.) →
**Show**. Confirm bid/ask are flowing. Close MT5.

### Step 5 — env vars (one-time)

```bash
SECRET="$(openssl rand -hex 32)"
echo "$SECRET"   # save it
```

Append to `~/.bashrc`:

```bash
export GOLD_BRIDGE_SECRET="<the secret>"
export MT5_LOGIN="<demo account number>"
export MT5_PASSWORD="<demo password>"
export MT5_SERVER="<server, e.g. XMGlobal-MT5 6>"
export MT5_ACCOUNT_TYPE="demo"
export GOLD_SYMBOL="GOLD"
```

`source ~/.bashrc`.

### Step 6 — start the bridge (Wine side)

```bash
~/.gold-mt5-wine/start-bridge.sh
```

Expected: `[bridge] listening on http://127.0.0.1:8765 symbol=GOLD`. Leave
the terminal open. **Important:** must run in a real terminal — `nohup` /
`setsid` detached stdin causes `init_sys_streams: WinError 6`.

### Step 7 — verify from Linux side

```bash
cd "/home/lesnar/Documents/Gold trader"
export GOLD_BROKER=mt5_remote
export GOLD_BRIDGE_SECRET="<same secret>"
.venv/bin/python -m gold_trader.cli broker-info
```

Expect: equity, balance, currency, leverage, `open_position: none`. If you
see that, the entire chain works end-to-end.

### Step 8 — promote to real money (only after weeks of clean demo)

Replicate steps 3, 5, 6, 7 with a separate real-account login. Use a
different prefix (`GOLD_MT5_PREFIX=$HOME/.gold-mt5-wine-real
./scripts/setup_wine_mt5.sh`). Set `MT5_ACCOUNT_TYPE=real`.

### Critical Wine gotchas

- `numpy` must be pinned `<2` (Wine ucrtbase lacks `crealf`).
- `python311._pth` must contain the **winepath** of `<repo>/src` or the
  `gold_trader` import fails (embeddable Python ignores `PYTHONPATH`).
- MT5 GUI → **AutoTrading / Algo Trading** toolbar button must be **ON**
  or `order_send` returns retcode 10027.
- Credentials live in `chmod 600 ~/.gold-mt5-wine/credentials.env`,
  sourced by `~/.bashrc` and `scripts/run_agent_cycle.sh`.

---

## 13. Operational lifecycle

### Daily

```bash
./start                         # MT5 + bridge + watcher + web UI (:8770)
bash scripts/status.sh          # snapshot every moving part
bash scripts/stop.sh            # tear down what start.sh launched
```

`./start` is the only command needed for live trading. It loads credentials
and saved secrets, starts MT5 under Wine, launches the bridge (via a
pseudo-TTY — required for Wine Python), starts the live watcher, and opens
the web UI.

When the broker is connected, the Trade tab chart, IFVG zones, and AI
checklist all read **live MT5 candles** from the bridge on every refresh
(5 s auto-refresh). Cached CSV is used only when the bridge is offline
(preview mode).

The Trade screen shows IFVG levels as **colour-coded horizontal bands**
(TradingView-style — no overlapping axis labels): red supply / green demand
IFVG, target and risk shading, plus options/CME strike lines from web research.

**Automatic AI scout** (`scripts/ifvg_auto_scout.py`, started by `./start`):
every ~60s runs the **full-system IFVG engine** (`scripts/ifvg_full_system_engine.py`)
then the legacy IFVG + OpenAI scan on your chart timeframe (default M15).
Outputs: `logs/ifvg_mtf_decision_state.json`, `logs/ifvg_mtf_operator_brief.md`,
`logs/operator_alerts.jsonl` (optional webhook/Telegram via env). Scout alerts remain in
`logs/ifvg_scout_state.json`. Policy: `config/execution_policy.json`
(see `config/execution_policy.json.example`). Specs:
[FULL_SYSTEM_IFVG.md](FULL_SYSTEM_IFVG.md) · [HTF_IFVG_RESET.md](HTF_IFVG_RESET.md).
Your job is only **Enter trade** when the approval brief shows all gates passed — the UI explains why.

**Live scout gates (Apr–May 2026 audit — see [AUDIT_RESULTS.md](AUDIT_RESULTS.md)):**
Grade **A only** · **`mixed bearish bias`** · **`macro_regime=mixed`** (hard block
aligned/opposed — override `IFVG_MACRO_OVERRIDE=1`) · `workflow_ready` · manual
approval always · **$30/trade** sizing reference · TP from swings/levels (no 2R cap).
**Paper yes, live no** until 20+ forward Grade-A journal trades.

**Operator workflow (8 steps, shown in the Trade tab approval panel):**

1. HTF bias — 4H + 1H EMA structure (bullish / bearish / ranging)
2. Intraday zone — M15/M5 IFVG + nearby S/R · round numbers
3. Live price location — top / middle / bottom of zone (blocks buying at resistance, selling at support)
4. 5M · 1M confirmation — rejection, break/retest, sweep/reclaim, IFVG retest
5. Macro · options · news — DXY, US10Y, futures context, CME/options strikes
6. Entry type — pullback (safest) · breakout (close + retest) · aggressive (smaller risk)
7. Invalidation — where the idea is wrong (stop level)
8. TP1 · TP2 · TP3 — nearest liquidity, prior swing, extended target

Formula: **HTF bias → key zone → live price location → 5M/1M confirmation → macro/options check → entry zone → SL → TP1/TP2/TP3 → invalidation**

Do **not** run `~/.gold-mt5-wine/start-bridge.sh` in a separate terminal —
`./start` handles it.

### Cron (autonomous loop)

```bash
bash scripts/install_cron.sh    # idempotent, marker-based installer
```

Installs:

```
*/15 * * * *  cd <repo> && scripts/run_agent_cycle.sh >> logs/agent.log 2>&1
0 22 * * 0    cd <repo> && PYTHONPATH=src .venv/bin/python scripts/weekly_champion.py \
              --output config/champion.json --top 5 >> logs/champion.log 2>&1
```

`run_agent_cycle.sh` sources `~/.gold-mt5-wine/credentials.env`, reads
`config/runtime_config.json` for `macro_filter_mode` and
`news_blackout_min`, auto-routes broker (paper if no bridge secret,
`mt5_remote` otherwise), and finally runs `update_journal.py` so the next
filter-lift report is current.

### Runtime config (`config/runtime_config.json`)

UI-driven flags. Keys:
`macro_filter_mode (off|soft|hard)`, `auto_trade_enabled (bool)`,
`news_blackout_min (int)`, `bridge_url`, `bridge_secret_set`, `symbol`,
`notes`. The bridge secret is **never** echoed in API responses.

---

## 14. Risk + safety guards

Evaluated every cycle in `infra/risk.py`. Env vars and defaults:

| Variable | Default | Purpose |
|---|---|---|
| `GOLD_DAILY_LOSS_FRACTION` | 0.04 | Kill switch on equity drop from peak |
| `GOLD_MIN_EQUITY` | 0 | Hard floor |
| `GOLD_MAX_CYCLE_LOSS` | 0.025 | Per-cycle ceiling |
| `GOLD_DIVERGENCE_ABS_TOL` | 5.0 | Broker-vs-paper divergence absolute |
| `GOLD_DIVERGENCE_REL_TOL` | 0.005 | Broker-vs-paper divergence relative |
| `GOLD_TICK_AGE_MAX_SEC` | 300 | Tick-feed staleness watchdog |
| `GOLD_MACRO_FILTER` | off | `off` / `soft` (log) / `hard` (block) |
| `GOLD_PROBABILITY_GATE` | off | `off` / `soft` (log) / `hard` (block) |
| `GOLD_NEWS_BLACKOUT_MIN` | 0 | ±N min around high-impact events |

**Panic CLI:** `gold-trader panic` flattens broker positions + arms
kill switch + emits `KILL_SWITCH_TRIGGERED` to the journal.

---

## 15. Roadmap

> Reordered 2026-05-10 in light of the 5-year operator-edge holdout
> failure and the 5y miner→trade-rule failure ([§11](#11-empirical-state)).
> The previous "encode rule then test" loop has been **demoted** in
> favour of a **pattern-discovery-first** workflow: only convert
> patterns the miner has already certified at p≤0.01 across two
> timeframes, and only via `holdout-mined-pattern` (so each candidate
> gets a clean stop/target verdict before it earns a strategy file).
> Items previously demoted remain demoted.

### Short term (next 2–4 weeks)

> Decision menu with rationale lives in [§11 → Next steps decision menu](#11-empirical-state).
> The numbered items below are the same options, prioritised. Option E
> (encode the operator's discretionary checklist) leapfrogs A/B/D
> because it inherits the operator's existing profitability prior
> instead of trying to discover an edge from scratch.

0. **[Scoring calibration v2 — DONE; v3 pending]** (updated
   2026-05-10 very late). v2 surgical fixes landed: `scored_penalty`
   helper, IFVG `htf_trend` binary + `htf_counter_penalty`, RSI
   `rsi_extreme` binary + `htf_counter` sign-flipped + `htf_against_penalty`.
   v2 calibration eliminated RSI inverse monotonicity (top buckets now
   PF 1.12–1.23 vs v1 0.16–0.49) and grew IFVG [90,100] from n=68 to
   n=388 at PF 3.74. Both families still show a [60,70) trough but
   threshold=70 cleanly avoids it. **Remaining v3 tasks**: (a) run
   `holdout-eval` with `score_full_threshold=70` (rejects everything
   below) on IFVG and RSI to confirm holdout PF lift; (b) operator
   chart-review (G–L from teardown) — pull 10 winning + 5 rejected
   IFVG/RSI trades, identify the missing discretionary filters,
   encode them; (c) only after v3 → roll scoring to ARB + 14 remaining
   strategies. Don't propagate calibration mistakes.
1. **[Option E] Encode the operator's discretionary checklist** for
   IFVG and RSI divergence. Process: (a) operator manual review — list
   every reason behind 10 winning trades and 5 rejected signals per
   strategy; (b) encode each filter as an independently-toggleable
   boolean inside the strategy, exposed in `dump-signals` so we can
   see which filter killed which signal; (c) manual eyeball of the
   first 30 historical fires post-filter; (d) holdout-eval. Target:
   IFVG n drops 1,330 → 50–150 with PF ≥ 1.20; RSI n grows 6 → 80–150
   with meaningful PF. Candidate filters and rationale in §11.
2. **[Option C] Operator trade journal as ground truth.** Log every
   discretionary trade taken (entry/exit/reason) in
   `data/operator_trades/`. Once n ≥ 30, fit slice tables on those
   instead of mechanical-rule trades. **Runs in parallel with E** —
   E captures the *checklist*, C measures the *outcomes*; they feed
   each other.
3. **[Option D] Lower the miner's `min_signals` floor for sparse TFs.**
   60m/h=16 produced zero survivors at floor=80 but 5y of 60m bars
   only has ~29,590 rows; the floor effectively requires a
   0.27%-frequency pattern at h=16. Make the floor adapt to
   `n_bars / horizon`. Do this before the next miner sweep.
4. **[Option A] Time-exit on the 5y miner survivors.** Extend
   `MinedPatternStrategy` + `holdout-mined-pattern` with
   `--exit-mode horizon --horizon-bars N` and re-run the 5-pattern
   batch. Lower priority than E because it lacks the operator-P&L
   prior; promote if E surfaces a setup that overlaps with a miner
   survivor (e.g. `hour_o7 & operator_ifvg_filters`).
5. **[Option B] Short-side + macro-conjunction miner pass.**
   `mine-all <5y> --with-macro` to surface short-side and macro-
   conditional survivors absent from the 2026-05-10 sweep.
   Holdout-mined-pattern any survivor with `min_holdout_n ≥ 30`.
6. **Pattern-discovery-first workflow** — remains the default loop
   for any future *exploratory* work (i.e. no operator P&L prior):
   - `mine-all` on the largest available bundle across multiple TFs.
   - Filter to cross-TF FDR replicators with `|R| ≥ 0.20` and
     `signs_consistent ≥ 2`.
   - For each survivor: `holdout-mined-pattern` with a 4×4
     (`stop_atr × RR`) grid **plus** the new horizon-exit mode (#4).
   - Promote to a strategy file *only* on a PASS (PF ≥ 1.20 ∧ p ≤ 0.20
     ∧ holdout n ≥ 30).
7. **Probability gate hardening.** `min_n` floor raised to 20 in slicer
   + gate (2026-05-09). Hard mode remains off until at least one family
   has ≥ 1 qualifying slice (`n ≥ 20`, `lower_ci_r > 0`, `pf ≥ 1.20`).
8. **Promote macro filter `soft → hard`** once `paper_stats` shows
   n ≥ 30 and allow-vs-block delta ≥ +0.10R.

### Medium term (1–3 months)

9. **Bracket / trailing-stop OCO** orders broker-side — preserves more
   upside on structural setups (especially IFVG and RSI divergence).
10. **Multi-symbol generalisation** — refactor `XAUUSD` constants out of
    bridge / cron / data layer.
11. **News-calendar autopopulation** from a structured source instead of
    manual UI entry.
12. **Position-size-by-regime** — risk fraction conditional on regime tags
    (e.g. half-size in `vol=high` + `trend=flat`).
13. **Champion loop weekly re-optimization** — run 4 consecutive Sundays
    to ground-truth the rolling-30-day hypothesis.

### Long term (3–6 months)

14. **Daily-bar mining** — the 5y bundle now includes a 1440m series
    (~1,300 bars). Re-run `mine-all` with `--timeframes 1440 --horizons
    1,3,5` once #3 (adaptive `min_signals`) lands.
15. **Add EUR/USD intraday data** so `dxy_lead_lag` joins the champion
    search.
16. **Broker fail-over** — secondary cTrader / Forex.com bridge so a
    single broker outage doesn't silently disable the system.
17. **Out-of-sample validation across 2018–2023** before any policy
    layer is trusted live (the 5y bundle already starts 2021-05; pre-
    pandemic regime is still untested).

### Deferred (no near-term work)

18. **RL policy layer.** Removed from active roadmap. Prerequisites
    (none of which are met today): 3+ mechanical strategies that pass
    holdout, 3+ years of data, ≥ 200 operator-journal trades. Returning
    to this before those are in place would amplify the same noise the
    rule-based search already amplifies.

---

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `wine: command not found` after step 1 | apt install failed silently — re-run without `-y` and read errors |
| `MetaTrader5 module not found` when bridge starts | `wine ~/.gold-mt5-wine/drive_c/winpy/python.exe -m pip install MetaTrader5` |
| `mt5.initialize failed: ('Terminal: Path not found',)` | Find `terminal64.exe`, set `MT5_TERMINAL_PATH` to its Windows path (`winepath -w /linux/path`) |
| `mt5.symbol_select('GOLD', True) failed` | Broker uses different gold symbol — check Market Watch → Symbols → set `GOLD_SYMBOL=<exact text>` |
| Bridge returns 401 to `broker-info` | `GOLD_BRIDGE_SECRET` differs between terminals — re-source `~/.bashrc` |
| Chart shows **CACHED** while banner is LIVE | Hard-refresh after `./start`. Check `/api/live/candles` returns `"source":"bridge"`. Secret mismatch causes 401 candle fetches — re-run `./start`. |
| `volume_min` error on order | Risk × stop produces < 0.01 lots — loosen stop or use `GOLDmicro` |
| `init_sys_streams: WinError 6` | Bridge launched with detached stdio — use `./start` (launches bridge via `script` pseudo-TTY) |
| `gold_trader` import fails inside Wine Python | `python311._pth` missing the winepath of `<repo>/src` |
| `order_send` retcode 10027 | MT5 GUI AutoTrading / Algo Trading button is OFF |

---

## Pointers

- `/memories/repo/notes.md` — append-only repo log of every change with
  exact file paths, command outputs, test counts, and lessons learned.
  When in doubt about state, read it.
- `tests/` — 281 passing, 1 deselected. Source of truth for expected
  behaviour.
- `logs/agent.log`, `logs/champion.log`, `logs/web.log` — runtime evidence.
- `logs/trade_journal.csv` — every closed paper trade tagged with regime,
  filter verdict, expected/realised R, drift R.

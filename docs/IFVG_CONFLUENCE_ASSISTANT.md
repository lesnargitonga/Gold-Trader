# Gold IFVG Confluence Assistant

This system is a **manual-approval IFVG execution assistant** for XAUUSD.

It is not trying to discover random edges. It scans for the specific model we want:

1. Liquidity sweep
2. Strong displacement
3. IFVG inversion
4. Retest with rejection
5. Higher-timeframe agreement
6. DXY/yields confirmation
7. CME/options/news context
8. No immediate support/resistance danger
9. Entry plan generated
10. Manual approval required
11. Shadow journal records result

The assistant produces an entry plan and records shadow outcomes. Live auto-entry remains off.

**Audit hub:** [AUDIT_RESULTS.md](AUDIT_RESULTS.md) · **Geometry:** [IFVG_EXECUTION_GEOMETRY_AUDIT.md](IFVG_EXECUTION_GEOMETRY_AUDIT.md)

**Post-audit posture:** **Paper yes, live no** until **20+ forward Grade-A trades** with journal outcomes.

## What Exists Now

### Strict IFVG Assistant

Main module:

`src/gold_trader/assistants/ifvg_confluence.py`

It provides:

- IFVG candidate scanning
- Sequence validation: sweep -> displacement -> inversion -> retest
- Checklist scoring from 0 to 100
- Setup grades and verdicts
- Entry zone, stop, TP1, TP2, TP3
- Manual approval flag
- Warning list
- Optional external OpenAI research context
- Shadow journal writing

Verdicts:

- `80-100`: `valid_entry`
- `65-79`: `alert_wait`
- `<65`: `ignore`
- `externally_blocked`: hard-mode external research block

### Checklist Weights

| Condition | Points |
|---|---:|
| Liquidity sweep before IFVG | 20 |
| Strong displacement | 15 |
| Clean IFVG inversion | 20 |
| HTF direction agrees | 15 |
| Retest gives rejection | 15 |
| DXY/yields confirm | 10 |
| Not trading into support/resistance | 5 |

Missing macro or market-level data is treated as neutral, not fatal.

## OpenAI External Research Layer

Optional module:

`src/gold_trader/research/realtime_research.py`

Config:

`config/openai_research.json`

Cache:

`data/cache/openai_market_research.json`

Environment variables:

```bash
export OPENAI_API_KEY="..."
export GOLD_OPENAI_RESEARCH="off|soft|hard"
export GOLD_OPENAI_RESEARCH_MODEL="gpt-5.4"
```

Defaults:

- `config/openai_research.json` mode: **soft** (grading + warnings only — never blocks)
- `GOLD_OPENAI_RESEARCH` env overrides when set (`off|soft|hard`)
- `GOLD_OPENAI_RESEARCH_MODEL=gpt-5.4`

**Principle:** IFVG creates the trade idea. External data grades quality and risk — it does not remove setups in soft mode.

### Final A/B/C/D grading (live profile)

| Grade | Score | Live scout | Action |
|---|---:|---|---|
| A | 80–100 | **`can_enter` eligible** (if all gates pass) | Normal **$30/trade** risk after price confirmation |
| B | 65–79 | **Paper only** — blocked for live | Reduce size or wait for cleaner 5M/1M rejection |
| C | 50–64 | **Blocked** | Watch only — not live-eligible |
| D | &lt;50 | **Blocked** | Avoid unless strong discretionary reason |

Technical IFVG score is combined with external confirmation (supportive / mixed / opposing) into `setup.grading` in the API.
Only **Grade A** passes `build_approval_brief` for the green Enter trade banner.

### Modes

- `off`: never call OpenAI.
- `soft` (**default**): attach confidence, warnings, and grade adjustments only — **never block**, never remove IFVG candidates, always show entry plan.
- `hard`: may mark `externally_blocked` — must never be enabled by default.

Hard mode can block only when configured and one of these is true:

- OpenAI returns `should_block_trade=true`.
- `news_risk=high` and `block_on_high_news_risk=true`.
- External context opposes the IFVG direction and `block_if_external_context_opposes_trade=true`.

### Research Output

The external research object includes:

- external bias
- supports trade yes/no
- should block yes/no
- confidence
- news risk
- DXY bias
- U.S. 10Y bias
- real yield bias
- options bias
- important levels
- danger zones
- warnings
- summary
- sources
- last checked time

OpenAI cannot create a setup by itself. No IFVG setup means no trade idea and no OpenAI research call.

## Live Scout (`ifvg_scout.py` + `ifvg_auto_scout.py`)

Background loop started by `./start`. Writes `logs/ifvg_scout_state.json` and feeds the Trade tab approval brief.

### `can_enter` rules (all required)

Implemented in `build_approval_brief` + `evaluate_live_sentiment`:

| Gate | Rule |
|------|------|
| Grade | **A only** — B paper-only, C/D blocked |
| Verdict | `valid_entry` or `alert_wait` |
| Score | Technical ≥ 65 |
| Workflow | `workflow_ready=true`; no hard-fail on steps 1 (HTF) or 3 (price location) |
| Alignment | **`mixed bearish bias`** — blocks mixed bullish, fully aligned stacks, compression-heavy mixed |
| Macro | **`macro_regime=mixed`** — blocks aligned, opposed, partial, unavailable |
| Override | `IFVG_MACRO_OVERRIDE=1` downgrades macro block to warning |
| External | Not `externally_blocked` (hard OpenAI mode only; default soft never blocks) |
| Approval | **Manual Enter trade always** — bundle accept/reject is not permission |

**30-day replay:** 206 Grade-A signals → **110 `can_enter`** before macro hard block (all aligned) → **0** after.

### Entry plan & targets (`ifvg_confluence.py`)

- **SL:** zone-end (IFVG gap edge + buffer / sweep invalidation)
- **TP1:** nearest swing or liquidity level, **minimum 1R floor**
- **TP2/TP3:** next structural swings + `config/market_levels.json` — **no fixed 2R runner cap**
- **Risk shown:** `suggested_risk_usd = $30` for Grade A (`LIVE_RISK_USD`)

Approval brief `model_watch` documents TP1/TP2/TP3 and explicitly states targets come from sentiment/levels, not an arbitrary 2R cap.

### Key modules

| Module | Role |
|--------|------|
| `src/gold_trader/assistants/ifvg_scout.py` | Approval brief, `can_enter`, scout loop |
| `src/gold_trader/assistants/ifvg_workflow.py` | 8-step workflow, `evaluate_live_sentiment`, macro/alignment gates |
| `src/gold_trader/assistants/ifvg_confluence.py` | Setup scan, checklist, `_entry_plan` |
| `scripts/ifvg_auto_scout.py` | Background interval runner |
| `scripts/grade_a_live_sim.py` | Historical replay of production gates |

## Agent-Cycle Integration

The assistant is wired into:

`src/gold_trader/research/state.py`

When `inversion_fair_value_gap` is enabled as a family, agent-cycle can now produce real IFVG confluence candidates in `entry_candidates`.

The cycle now supports this flow:

1. Scan for strict IFVG candidates.
2. Score the checklist.
3. Produce an entry plan.
4. If score is `>=65`, optionally attach cached OpenAI external research.
5. Record qualifying setups to shadow journal.
6. Require manual approval.

Agent-cycle prints IFVG plans like:

```text
IFVG plan: zone=4398.00-4402.00 entry=4398.00-4402.00 SL=4412.00 TP1=4382.00 TP2=4375.00 TP3=4368.00
```

## Web UI

New endpoint:

`/api/live/ifvg/checklist`

Implemented in:

`src/gold_trader/web/server.py`

Live UI card added in:

`src/gold_trader/web/static/index.html`

UI rendering added in:

`src/gold_trader/web/static/app.js`

The Live tab now shows:

- IFVG verdict
- Score and grade
- Direction
- IFVG zone
- Entry, SL, TP1, TP2, TP3
- Checklist pass/partial/fail rows
- External Research section
- Warnings
- `manual required`

## Market Levels

Manual CME/options/round-number levels live in:

`config/market_levels.json`

Current starter levels:

- 4350
- 4400
- 4450
- 4500

These are used as danger zones and target references. Update this file manually from CME options, open interest, Investing.com, or operator levels.

## Macro And News

Macro confirmation uses existing cached sidecar data:

`data/macro/*.csv`

Code path:

`src/gold_trader/data/macro.py`

The assistant checks DXY, U.S. 10Y, and real 10Y changes when available.

News warnings use:

`data/macro/news_calendar.csv`

Code path:

`src/gold_trader/calendar.py`

If macro/news data is missing, the assistant does not crash and does not hard-block. It marks the condition neutral or warns.

## Shadow Journal

Agent-cycle records IFVG setups with score `>=65` to:

`data/<agent-output-dir>/ifvg_shadow_setups.csv`

For the default paper path, this is typically:

`data/agent_live_xauusd/ifvg_shadow_setups.csv`

The CSV includes:

- timestamp
- timeframe
- side
- score
- grade
- verdict
- zone
- entry
- stop
- TP1/TP2/TP3
- checklist JSON
- warnings
- external bias
- external confidence
- external news risk
- external supports trade
- external should block
- external warnings JSON
- external summary
- external sources JSON
- blank `outcome_r`
- blank `outcome_note`

Fill `outcome_r` manually after reviewing the setup result.

## Shadow Report

Report script:

`scripts/ifvg_shadow_report.py`

Run:

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_shadow_report.py
```

Or point it at a specific CSV:

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_shadow_report.py data/agent_live_xauusd/ifvg_shadow_setups.csv
```

It summarizes reviewed outcomes by:

- score bucket
- side
- win rate
- average R
- total R

## Safety Posture

Live auto-entry is **off**.

| Mode | Status |
|------|--------|
| Scan / confirm / entry-plan | **Active** via `./start` |
| Shadow journal | **Active** — fill `outcome_r` after review |
| Paper Enter trade (manual) | **Yes** — when green banner + operator agrees |
| Live real-money automation | **No** until 20+ forward Grade-A trades with positive journal stats |

Do not promote to automatic execution until forward validation passes (see [AUDIT_RESULTS.md](AUDIT_RESULTS.md) forward plan).

## Tests Added

New tests:

`tests/test_ifvg_confluence_assistant.py`

OpenAI research tests:

`tests/test_realtime_research.py`

Updated web tests:

`tests/test_web.py`

Covered behavior:

- Sweep must happen before IFVG formation.
- Weak/no rejection does not produce a valid entry.
- Missing macro/levels are neutral.
- Nearby configured support/resistance reduces score.
- Agent-cycle includes IFVG assistant candidates.
- Shadow CSV writer works.
- OpenAI disabled returns neutral.
- Missing `OPENAI_API_KEY` does not crash.
- Cached research is reused.
- Soft mode never blocks.
- Hard mode can externally block.
- No IFVG candidate triggers no OpenAI call.
- Web endpoint returns stable JSON.

Verified:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_realtime_research.py tests/test_ifvg_confluence_assistant.py tests/test_web.py
```

Result:

```text
38 passed
```

Broader integration subset:

```text
81 passed
```

## Practical Workflow

1. Start the system:

```bash
bash scripts/start.sh
```

2. Open:

```text
http://127.0.0.1:8770
```

3. Watch the Live tab IFVG assistant card.

4. When a setup appears, review:

- direction
- IFVG zone
- checklist score
- macro/news warnings
- support/resistance danger
- entry/SL/TPs

5. Decide manually.

6. Later, fill `outcome_r` in the shadow CSV and run the report.

## Key Files

| Area | File |
|---|---|
| Assistant logic | `src/gold_trader/assistants/ifvg_confluence.py` |
| Assistant exports | `src/gold_trader/assistants/__init__.py` |
| OpenAI research | `src/gold_trader/research/realtime_research.py` |
| Agent-cycle wiring | `src/gold_trader/research/state.py` |
| CLI output | `src/gold_trader/cli.py` |
| Web API | `src/gold_trader/web/server.py` |
| Live UI markup | `src/gold_trader/web/static/index.html` |
| Live UI logic | `src/gold_trader/web/static/app.js` |
| Market levels | `config/market_levels.json` |
| OpenAI research config | `config/openai_research.json` |
| OpenAI research cache | `data/cache/openai_market_research.json` |
| Shadow report | `scripts/ifvg_shadow_report.py` |
| Tests | `tests/test_ifvg_confluence_assistant.py` |
| OpenAI tests | `tests/test_realtime_research.py` |

## 30-day audit (Apr 29 – May 29 2026)

Historical replay documented in **[AUDIT_RESULTS.md](AUDIT_RESULTS.md)** and **[IFVG_EXECUTION_GEOMETRY_AUDIT.md](IFVG_EXECUTION_GEOMETRY_AUDIT.md)**.

**Live rules from that window:**

- Grade **A only**; B paper-only with `workflow_ready`; C/D blocked
- **`mixed bearish bias`** + **`macro_regime=mixed`** required
- **$30/trade** risk — not fixed 0.05 lot on $100
- Macro block: 110 → 0 `can_enter`; Grade A + macro mixed **+2.65R / 71% WR (n=7)**

**Re-run:**

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_deep_audit.py --days 30 --end 2026-05-29
PYTHONPATH=src .venv/bin/python scripts/ifvg_execution_geometry_audit.py --start 2026-04-29 --end 2026-05-29
PYTHONPATH=src .venv/bin/python scripts/grade_a_live_sim.py --start 2026-04-29 --end 2026-05-29
```


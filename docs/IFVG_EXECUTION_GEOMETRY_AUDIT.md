# IFVG Execution Geometry Audit

**Window:** 2026-04-29 → 2026-05-29 UTC (30 days)

## Hypothesis
IFVG becomes profitable with zone-end SL (not wide structural sweep stop), min 1R TP1, partial/runner, sentiment as metadata then slice analysis.

## Entry assumption
Signal-bar close (retest at close); no limit-in-zone, no hindsight

## Model comparison (independent per-signal, R-multiples, $30 risk ref)

| Model | Trades | WR | Avg R | Total R | PF | Max DD (R) |
|-------|--------|-----|-------|---------|-----|------------|
| IFVG_ZONE_SL_1R_2R | 518 | 49.4% | -0.084 | -43.36 | 0.85 | 59.80 |
| IFVG_ZONE_SL_PARTIAL_RUNNER | 517 | 49.3% | -0.224 | -115.71 | 0.59 | 119.97 |
| STRUCTURAL_SL_MICRO_TP_BASELINE | 518 | 49.8% | -0.065 | -33.84 | 0.88 | 51.67 |
| STRUCTURAL_SL_2R | 514 | 31.3% | -0.122 | -62.87 | 0.83 | 87.40 |

## Verdict
- **Best model:** STRUCTURAL_SL_MICRO_TP_BASELINE
- **Zone SL vs structural:** Structural SL preferred (zone hypothesis not confirmed)
- **Theory supported:** No — Zone SL / 1R TP1 did not beat structural execution on aggregate R

### Best sentiment slices (total R, min 5 trades)
- `macro_regime=mixed`: +14.83 R (64 tr, WR 64%)
- `htf_bias=bullish`: +8.44 R (109 tr, WR 57%)
- `alignment=mixed bullish bias`: +5.60 R (115 tr, WR 56%)
- `dxy_bias=supports_buy`: +4.06 R (68 tr, WR 56%)
- `us10y_bias=supports_sell`: +2.37 R (30 tr, WR 57%)

### Grade A only
- Best model @ A: **IFVG_ZONE_SL_1R_2R** (total R -2.46)
  - IFVG_ZONE_SL_1R_2R: 183 tr, WR 53.0%, avg R -0.013, total R -2.46
  - IFVG_ZONE_SL_PARTIAL_RUNNER: 182 tr, WR 52.8%, avg R -0.162, total R -29.49
  - STRUCTURAL_SL_MICRO_TP_BASELINE: 183 tr, WR 50.8%, avg R -0.042, total R -7.64
  - STRUCTURAL_SL_2R: 182 tr, WR 30.2%, avg R -0.152, total R -27.62

## Outputs
- JSON: `/home/lesnar/Documents/Gold trader/logs/ifvg_execution_geometry_audit.json`
- CSV: `/home/lesnar/Documents/Gold trader/logs/ifvg_execution_geometry_trades.csv`

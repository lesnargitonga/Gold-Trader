# Higher-Timeframe IFVG Reset

This reset simplifies Gold Trader around one job: wait for high-quality XAUUSD inversion-FVG opportunities confirmed across all available timeframes.

## Non-negotiables

- IFVG only; the 9-family firehose cannot create trades.
- Use D1, H4, H1, M30, M15, M5, and M1 for confirmation.
- D1/H4/H1 define bias.
- M30/M15 confirm the setup.
- M5/M1 time the entry only after higher-timeframe alignment.
- Maximum 3 trades per UTC day.
- Maximum 1 open position.
- Stop for the day after 2 losses.
- Grade A / score 82+ only.
- Live trading remains disabled until explicitly enabled and forward evidence supports it.

## Outputs

- `logs/ifvg_mtf_decision_state.json`
- `logs/ifvg_mtf_operator_brief.md`

Run manually (legacy HTF-only engine):

```bash
PYTHONPATH=src .venv/bin/python scripts/htf_ifvg_decision_engine.py
```

Production scout uses the expanded engine — see [FULL_SYSTEM_IFVG.md](FULL_SYSTEM_IFVG.md):

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_full_system_engine.py
```

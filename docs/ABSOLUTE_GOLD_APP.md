# Absolute Gold App

This patch replaces the single long dashboard with a multi-page trading command center.

## Pages

- Trade: live candlestick workbench, timeframe switching, decision hero, entries, stop, targets, reasons, blockers.
- Market: live data provider, spread, volatility, macro, sentiment, broker/order status.
- Signals: IFVG-only model rules and multi-timeframe confirmation.
- Risk: daily guard, open position guard, live-order lock.
- Journal: operator alerts and paper evidence.
- Settings: cloud runtime status without exposing secrets.

## API endpoints

- `/api/decision`
- `/api/candles?tf=M15&count=260`
- `/api/alerts`
- `/health`

## Render start command

With full market awareness (spread, macro, sentiment, COT, cross-market):

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_full_context.py
```

App only (no context layer):

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_app.py
```

## Safety

Live orders remain disabled unless `GOLD_ENABLE_LIVE_ORDERS=true`. The UI never unlocks live orders.

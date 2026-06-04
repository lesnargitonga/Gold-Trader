# Full-System IFVG Integration

This patch keeps the bot simple where it matters: **IFVG is the only execution trigger**. Everything else becomes confirmation, blocking, risk control, or operator guidance.

## What is now checked

- All-timeframe confirmation: `D1`, `H4`, `H1`, `M30`, `M15`, `M5`, `M1`
- Higher-timeframe bias: `D1/H4/H1`
- Entry timing: `M5/M1`
- IFVG retest/inversion confirmation
- Liquidity sweep / displacement
- Max 3 trades per UTC day
- Max 1 open position
- Stop after 2 losses
- Candle data: MT5 bridge (local) → **Twelve Data** (cloud) → CSV cache fallback
- Spread filter from MT5 bridge tick data when available
- Session filter: London, New York, and overlap by default
- M15 volatility filter
- High-impact macro calendar filter when `data/macro/economic_calendar.json` or CSV exists
- Sentiment filter when `logs/sentiment_state.json` or `data/sentiment/news_sentiment.json` exists
- Journal guard from `logs/trade_journal.csv`, `data/paper/trades.csv`, and `data/live/trades.csv`
- Operator brief: `logs/ifvg_mtf_operator_brief.md`
- Machine-readable state: `logs/ifvg_mtf_decision_state.json`
- Alert log: `logs/operator_alerts.jsonl`
- Optional webhook: `GOLD_ALERT_WEBHOOK_URL`
- Optional Telegram: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

## Expected sentiment file

```json
{
  "score": 0.32,
  "state": "bullish",
  "summary": "Gold sentiment is positive because...",
  "updated_at": "2026-06-04T12:00:00+00:00"
}
```

Score range is `-1.0` to `1.0`.

## Expected macro calendar file

```json
[
  {
    "time_utc": "2026-06-05T12:30:00+00:00",
    "currency": "USD",
    "impact": "high",
    "name": "Non-Farm Payrolls"
  }
]
```

CSV also works with columns like `time_utc,currency,impact,name`.

## Twelve Data (Render / cloud)

Set an API key (free tier works for paper scouting):

```text
TWELVE_DATA_API_KEY=your_key
```

Optional:

```text
GOLD_TWELVE_DATA_SYMBOL=XAU/USD
GOLD_TWELVE_DATA_CACHE_SECONDS=120
```

Or store in `config/secrets.json` as `twelve_data_api_key`.

The full-system engine maps `XAUUSD` / `GOLD` → `XAU/USD` and loads `D1` … `M1` via
`src/gold_trader/data/twelvedata.py` when the MT5 bridge is offline.

## Run

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_full_system_engine.py
```

Or run the loop:

```bash
PYTHONPATH=src .venv/bin/python scripts/ifvg_auto_scout.py
```

## Safety

The engine returns `TRADE_READY_PAPER_AUTO_ALERT_AUTO`; it does **not** place live trades by itself. Live execution should only be enabled after enough forward paper evidence supports the edge.

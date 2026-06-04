# Apply System Hardening v2

```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_system_hardening_v2.zip -d /tmp/gold_hardening_v2
cp -R /tmp/gold_hardening_v2/* .
chmod +x scripts/fetch_calendar.py scripts/harden_market_state.py scripts/provider_health.py scripts/render_hardened_market_awareness.py
```

Test locally:

```bash
PYTHONPATH=src python3 scripts/fetch_calendar.py
PYTHONPATH=src python3 scripts/harden_market_state.py
PYTHONPATH=src python3 scripts/provider_health.py
PYTHONPATH=src python3 scripts/render_hardened_market_awareness.py
```

Commit:

```bash
git add src/gold_trader/core/market_state_hardening.py \
        scripts/fetch_calendar.py \
        scripts/harden_market_state.py \
        scripts/provider_health.py \
        scripts/render_hardened_market_awareness.py \
        docs/SYSTEM_HARDENING_V2.md \
        APPLY_SYSTEM_HARDENING_V2.md

git commit -m "Harden market awareness pipeline"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_hardened_market_awareness.py
```

Keep:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_STRICT_UNKNOWN_CONTEXT=true
```

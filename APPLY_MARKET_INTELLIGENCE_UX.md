# Apply: Market Intelligence UX Hardening

This patch hardens the cloud command center around real market-awareness problems:

- readable blocker text
- score decomposition visible in the decision payload
- missing-input penalties
- source-age/stale-data status
- provider-health summary
- market intelligence API surface
- Render runner that runs context + engine + hardening before serving UI

## Apply

```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_market_intelligence_ux.zip -d /tmp/miux
cp -R /tmp/miux/* .
chmod +x scripts/harden_market_intelligence_ux.py scripts/render_market_intelligence_ux.py

PYTHONPATH=src python3 scripts/harden_market_intelligence_ux.py
PYTHONPATH=src python3 -m gold_trader.web.market_intelligence_api
```

Open:

```text
http://localhost:8770
```

## Commit

```bash
git add src/gold_trader/core/market_intelligence_ux.py \
        src/gold_trader/web/market_intelligence_api.py \
        scripts/harden_market_intelligence_ux.py \
        scripts/render_market_intelligence_ux.py \
        docs/MARKET_INTELLIGENCE_UX.md \
        APPLY_MARKET_INTELLIGENCE_UX.md

git commit -m "Harden command center market intelligence UX"
git push
```

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_market_intelligence_ux.py
```

Keep:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_STRICT_UNKNOWN_CONTEXT=true
```

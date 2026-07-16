"""Pin ~/.gold-mt5-wine/credentials.env to bridge port 8766 with the secrets.json
bridge_secret (so bridge, scout, web, cron all agree). Local alt-port setup:
cypher keeps :8765. Run: .venv/bin/python scripts/_pin_creds_8766.py
"""
import json
import pathlib
import re

bs = str(json.loads(pathlib.Path("config/secrets.json").read_text()).get("bridge_secret") or "")
cr = pathlib.Path.home() / ".gold-mt5-wine" / "credentials.env"
t = cr.read_text()
t = re.sub(r"(export\s+)?GOLD_BRIDGE_URL=.*", 'export GOLD_BRIDGE_URL="http://127.0.0.1:8766"', t)
t = re.sub(r"(export\s+)?GOLD_BRIDGE_PORT=.*", "export GOLD_BRIDGE_PORT=8766", t)
if bs:
    t = re.sub(r'(export\s+)?GOLD_BRIDGE_SECRET=.*', 'export GOLD_BRIDGE_SECRET="%s"' % bs, t)
cr.write_text(t)
print("creds pinned: GOLD_BRIDGE_URL/PORT=8766, secret unified=%s" % bool(bs))

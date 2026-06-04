"""Cloud entrypoint: ``python -m gold_trader.web.app`` (e.g. Render)."""
from __future__ import annotations

import os

from gold_trader.web import serve


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("GOLD_WEB_PORT", "8770")))
    serve(host=host, port=port)


if __name__ == "__main__":
    main()

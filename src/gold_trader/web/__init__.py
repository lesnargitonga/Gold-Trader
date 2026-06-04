"""Lightweight web control panel for gold-trader.

Stdlib-only HTTP server (http.server) + single-page app served from
``static/``.  Exposes JSON endpoints under ``/api/`` and serves the SPA at
``/``.  Designed for local-only operation (binds 127.0.0.1 by default).
"""
from .server import build_server, serve

__all__ = ["build_server", "serve"]

#!/usr/bin/env python3
"""Backward-compatible entrypoint — delegates to market intelligence UX."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "render_market_intelligence_ux.py"), run_name="__main__")

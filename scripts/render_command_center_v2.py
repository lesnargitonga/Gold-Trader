#!/usr/bin/env python3
"""Backward-compatible entrypoint — delegates to hardened market awareness runner."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "render_hardened_market_awareness.py"), run_name="__main__")

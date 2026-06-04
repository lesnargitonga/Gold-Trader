#!/usr/bin/env python3
"""Backward-compatible entrypoint — delegates to React command center v2."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "render_react_command_center_v2.py"), run_name="__main__")

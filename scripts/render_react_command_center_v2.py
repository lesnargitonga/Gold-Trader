#!/usr/bin/env python3
"""Backward-compatible entrypoint — delegates to React command center v3."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "render_react_command_center_v3.py"), run_name="__main__")

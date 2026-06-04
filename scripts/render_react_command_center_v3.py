#!/usr/bin/env python3
"""Backward-compatible entrypoint — delegates to professional command center."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "render_pro_command_center.py"), run_name="__main__")

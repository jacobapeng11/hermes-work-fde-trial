#!/usr/bin/env python3
"""Run the focused active-Run Hermes raw MCP reload smoke."""

from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from hermes_raw_mcp_reload_smoke.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

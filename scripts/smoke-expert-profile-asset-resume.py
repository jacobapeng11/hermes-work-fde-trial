#!/usr/bin/env python3
"""Run the Profile asset isolation and targeted Session resume smoke."""

from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from expert_profile_resume_smoke.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

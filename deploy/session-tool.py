#!/usr/bin/env python3
"""Compatibility entrypoint; the maintained implementation lives in board/."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "board" / "session.py"), run_name="__main__")

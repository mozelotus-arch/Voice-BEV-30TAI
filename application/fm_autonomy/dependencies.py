"""Explicit, lazy loading of pinned local dependencies (no hardware import at startup)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def add_vendor(*names):
    for name in names:
        path = str(ROOT / 'vendor' / name)
        if path not in sys.path:
            sys.path.insert(0, path)

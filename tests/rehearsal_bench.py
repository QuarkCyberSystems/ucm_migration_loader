"""Locate the bench for a checkout under the bench root or its apps directory."""

from pathlib import Path

BENCH = next(root for root in Path(__file__).resolve().parents if (root / "sites" / "apps.txt").is_file())

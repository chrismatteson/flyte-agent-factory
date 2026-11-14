"""Offline analysis and fresh-context model experiments for the factory."""

import sys
from pathlib import Path

# `python -m ops.model_eval` runs under `uv run`, which does not install this project (`package = false`):
# without this its imports of the package fail with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

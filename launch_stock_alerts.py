"""Dependency-path bootstrap for running ChartSage Stock Alerts from source."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_DIR / "_packaging_runtime"
APPLICATION = PROJECT_DIR / "ChartSageStockAlerts.py"


def main() -> int:
    if not RUNTIME_DIR.is_dir():
        raise FileNotFoundError(
            f"Required consolidated runtime folder is missing: {RUNTIME_DIR}"
        )
    if not APPLICATION.is_file():
        raise FileNotFoundError(f"Stock Alerts application is missing: {APPLICATION}")
    # Insert—not append—so an incomplete global/user package can never win.
    sys.path.insert(0, str(RUNTIME_DIR))
    sys.argv = [str(APPLICATION), *sys.argv[1:]]
    runpy.run_path(str(APPLICATION), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

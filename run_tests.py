#!/usr/bin/env python
"""Run the line-whitelist test suite with per-file process isolation.

Each test file gets its own fresh ``python -m pytest <file>`` subprocess. The
Dashboard test injects a fake ``plugins.platforms.line.whitelist_store`` module
into ``sys.modules``, while the store test imports the real one — running every
file in one interpreter would let that injected module leak across files. One
process per file keeps each run clean and mirrors the upstream Hermes harness.

Exit code is non-zero if any file fails, so CI can gate on it.

Usage:
    python run_tests.py            # run all test files
    python run_tests.py -k store   # extra args are forwarded to pytest
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent / "tests"


def main() -> int:
    extra = sys.argv[1:]
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("no test files found", file=sys.stderr)
        return 1

    failed: list[str] = []
    for f in files:
        print(f"\n=== {f.name} ===", flush=True)
        rc = subprocess.call(
            [sys.executable, "-m", "pytest", str(f), "-q", *extra]
        )
        if rc != 0:
            failed.append(f.name)

    print("\n" + "=" * 48)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print(f"OK — {len(files)} test files passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

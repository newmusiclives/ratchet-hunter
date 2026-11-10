"""Run every test suite; exit non-zero if any test fails.  python3 tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
failed = 0
for suite in ["test_hunter", "test_backtest", "test_live", "test_merger", "test_rocket"]:
    r = subprocess.run([sys.executable, str(here / f"{suite}.py")], capture_output=True, text=True)
    oks = r.stdout.count("\nok ") + r.stdout.startswith("ok ")
    print(f"{suite:14} {'PASS' if r.returncode == 0 else 'FAIL'} ({oks} ok)")
    if r.returncode:
        failed += 1
        print(r.stdout[-800:], r.stderr[-1500:])
sys.exit(1 if failed else 0)

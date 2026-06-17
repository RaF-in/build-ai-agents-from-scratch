"""Consolidated test runner (Phase 10): run every test_phase*.py in order and report
one green/red. Each suite is a standalone script (no pytest), so we just shell out.

Run: `uv run python testing_files/run_all.py`
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    suites = sorted(glob.glob(os.path.join(HERE, "test_phase*.py")))
    failed: list[str] = []
    for path in suites:
        name = os.path.basename(path)
        print(f"\n===== {name} =====")
        proc = subprocess.run([sys.executable, path])
        if proc.returncode != 0:
            failed.append(name)

    print("\n" + "=" * 50)
    if failed:
        print(f"FAILED ({len(failed)}/{len(suites)}): {', '.join(failed)}")
        return 1
    print(f"ALL {len(suites)} PHASE SUITES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

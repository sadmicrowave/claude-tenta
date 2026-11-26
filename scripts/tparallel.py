#!/usr/bin/env python3
"""Run unit-test modules in parallel, one process each: `scripts/t --parallel [modules]` (names of files in tests/, without .py).

Every module runs in its own interpreter, the largest files first (they are the slowest, so they start at once and the run ends when the last one does).
Prints only the failures, then one summary line; exits 1 if any module failed. A module that cannot share the machine with the others goes in tests/serial.txt and runs alone, after the rest."""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def modules(tests, names=()):
    """The modules to run, largest file first."""
    found = [p for p in tests.glob("test_*.py")] if not names else [tests / f"{n}.py" for n in names]
    return [p.stem for p in sorted(found, key=lambda p: (-p.stat().st_size, p.name))]


def serial_only(tests):
    f = tests / "serial.txt"
    return {line.split("#")[0].strip() for line in f.read_text().splitlines() if line.split("#")[0].strip()} if f.exists() else set()


def run_one(python, tests, name):
    start = time.time()
    done = subprocess.run([python, "-m", "unittest", name], cwd=tests, capture_output=True, text=True)
    return name, done.returncode, time.time() - start, done.stdout + done.stderr


def main(python, tests, names=(), jobs=None):
    tests = Path(tests)
    todo = modules(tests, names)
    alone = [n for n in todo if n in serial_only(tests)]
    together = [n for n in todo if n not in alone]
    started = time.time()
    results = []
    with ThreadPoolExecutor(jobs or os.cpu_count() or 4) as pool:
        results += list(pool.map(lambda n: run_one(python, tests, n), together))
    results += [run_one(python, tests, n) for n in alone]
    failed = [r for r in results if r[1] != 0]
    for name, _, _, out in failed:
        print(f"--- {name}")
        print("\n".join(out.rstrip().splitlines()[-60:]))
    slowest = max(results, key=lambda r: r[2], default=("", 0, 0, ""))
    print(f"{len(results)} modules, {len(failed)} failed, {time.time() - started:.0f}s (slowest: {slowest[0]} {slowest[2]:.0f}s)")
    print("slowest eight: " + ", ".join(f"{r[0]} {r[2]:.0f}s" for r in sorted(results, key=lambda r: -r[2])[:8]))
    if failed:
        print("failed: " + " ".join(r[0] for r in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))

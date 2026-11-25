"""The orchestrator was once called the scheduler; that name is retired everywhere (docs, code, tests, scripts). Only the Windows operating system's own "Task Scheduler" / "Scheduled Task" may remain."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r"(?<![A-Za-z])[Ss]chedulers?(?![A-Za-z])|(?<![A-Za-z])[Ss]chedulers?_")


class NoSchedulerWord(unittest.TestCase):
    def test_the_retired_name_is_not_used(self):
        try:
            names = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).split("\n")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        bad = []
        for n in names:
            f = ROOT / n
            if not n or n == "tests/test_no_scheduler_word.py" or not f.is_file():
                continue
            try:
                lines = f.read_text().split("\n")
            except (OSError, UnicodeDecodeError):
                continue
            bad += [f"{n}:{i}" for i, line in enumerate(lines, 1) if WORD.search(line) and "Task Scheduler" not in line]
        self.assertEqual(bad, [], "say 'orchestrator' (Windows' own Task Scheduler is the only exception)")


if __name__ == "__main__":
    unittest.main()

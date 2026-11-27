"""Vocabulary guard: the reasons to offload a command to a worker are called "offload reasons" (rungs of the ladder, docs/design/OFFLOAD-REASONS.md). Nothing the person reads may say
that a command or a reason "pays" ("why it pays", "reason it pays", "repay", ...): the term is the owner's, set 2026-11-27 for the whole theme/offload-value."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"\b(pay|pays|paying|repay|repays|repaying|payoff)\b", re.I)
SCOPE = ("src", "docs", "config", "README.md", "scripts")
LEGIT = re.compile(r"payment|pay-as-you|per-pay", re.I)


class Terms(unittest.TestCase):
    def test_no_reason_to_offload_is_said_to_pay(self):
        files = subprocess.run(["git", "ls-files", *SCOPE], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        bad = []
        for f in files:
            p = ROOT / f
            if p.suffix not in (".py", ".md", ".toml", ".sh", "") or not p.is_file():
                continue
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if BANNED.search(line) and not LEGIT.search(line):
                    bad.append(f"{f}:{n}: {line.strip()[:100]}")
        self.assertEqual(bad, [], 'say "offload reason" (or what the reason is), never that something "pays"')


if __name__ == "__main__":
    unittest.main()

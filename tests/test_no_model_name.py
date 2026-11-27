"""The app is model-agnostic: no AI model or vendor name may appear in any tracked file or file name.
The one exception is a third-party tool's own literal path (its skills folder), which has to be spelled exactly to work."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = re.compile("clau" + "de|anthro" + "pic", re.I)
ALLOWED = re.compile(r"~/\." + "clau" + r"de/skills\b")           # the README line that installs the skill where the assistant looks for it


def tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    return [p for p in out.split("\0") if p and (ROOT / p).is_file()]


class NoModelName(unittest.TestCase):
    def test_no_file_name_carries_a_model_name(self):
        self.assertEqual([p for p in tracked() if BANNED.search(p)], [])

    def test_no_file_content_carries_a_model_name(self):
        bad = []
        for p in tracked():
            if p == "tests/test_no_model_name.py":
                continue
            try:
                text = (ROOT / p).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if BANNED.search(ALLOWED.sub("", line)):
                    bad.append(f"{p}:{n}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "model or vendor name found; reword it neutrally")


if __name__ == "__main__":
    unittest.main()

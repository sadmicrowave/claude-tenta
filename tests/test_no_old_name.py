"""The app is called armada: its commands start with arm-, its package is `armada`, its variables ARM_*, its pin file .armada.toml.
The former names (the old product name, the ct- command prefix, CT_ variables, the ct package) must not come back in any tracked file or file name.

Exceptions, each with its reason:
- this file (it spells the old names to look for them);
- src/armada/client/ignore_templates/ (third-party .gitignore files, kept byte for byte so their checksums hold);
- fake product names in tests (a name merely ending in "ct", such as tct or homect, proves the command prefix is configurable): the patterns refuse a word character in front, so they do not match;
- `%ct`, the git format for a commit time: a "ct" after a percent sign is not our name."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = "te" + "nta"                  # the old product name, spelled in two pieces so this file itself stays clean
CT = "c" + "t"
CONTENT = re.compile("|".join([
    "(?i:" + OLD + r")(?!cl)",                                              # tentacle art is the logo's own word
    rf"(?<![\w-]){CT}-",                                          # the old command prefix
    rf"(?<![A-Za-z0-9_]){CT.upper()}_",
    rf"(?<=\\[nt])({CT}-|{CT.upper()}_)",                           # after an escaped newline or tab inside a string
    rf"(?<![\w.%/\\-]){CT}(?![\w.\-/\\])",                        # the old package name used alone (import, -m, prose)
    rf"(?<![\w%]){CT}[./\\_]",                                    # package paths and dotted modules
    rf"(?<=/){CT}(?![\w-])",                                     # a path ending in the old package or folder name
    rf"\.{CT}\b",                                                 # the old client folder
    rf"-{CT}-",
    rf"{CT}worker",
]))
NAME = re.compile(rf"(^|/)({CT}[-_.]|{CT}/|{CT}$)|(?i:{OLD})")
SKIP = ("tests/test_no_old_name.py", "src/armada/client/ignore_templates/")


def tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    return [p for p in out.split("\0") if p and (ROOT / p).is_file() and not p.startswith(SKIP[1]) and p != SKIP[0]]


class NoOldName(unittest.TestCase):
    def test_no_file_name_carries_an_old_name(self):
        self.assertEqual([p for p in tracked() if NAME.search(p)], [])

    def test_no_file_content_carries_an_old_name(self):
        bad = []
        for p in tracked():
            try:
                text = (ROOT / p).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                m = CONTENT.search(line)
                if m:
                    bad.append(f"{p}:{n}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "an old name came back; use armada, arm- or ARM_")

    def test_the_guard_catches_each_old_spelling(self):
        for s in (OLD, f"{CT}-run", f"{CT.upper()}_CONFIG", f"import {CT}", f"{CT}.theme", f"~/.{CT}", f"{CT}worker"):
            self.assertTrue(CONTENT.search(s), s)
        for s in ("tct-admin", "homect-step", "%ct", "tentacled", "direct-compress", "arm-run"):
            self.assertFalse(CONTENT.search(s), s)


if __name__ == "__main__":
    unittest.main()

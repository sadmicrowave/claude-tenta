"""The documentation stays true: every relative link resolves, the generated pages are current, and the README does not promise what is not there."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

import support  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


class Docs(unittest.TestCase):
    def test_every_relative_link_points_at_a_file(self):
        bad = []
        for doc in DOCS:
            for target in LINK.findall(doc.read_text()):
                if re.match(r"^(https?:|mailto:|#)", target):
                    continue
                if not (doc.parent / target.split("#")[0]).exists():
                    bad.append(f"{doc.relative_to(ROOT)} -> {target}")
        self.assertEqual(bad, [])

    def test_the_generated_pages_are_current(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-docs"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_every_component_page_has_a_plain_summary_and_a_specification(self):
        for page in sorted((ROOT / "docs" / "components").glob("*.md")):
            text = page.read_text()
            self.assertIn("## In plain words", text, page.name)
            self.assertIn("## Technical specification", text, page.name)
            self.assertLess(text.index("## In plain words"), text.index("## Technical specification"), page.name)

    def test_the_index_lists_every_component_page(self):
        index = (ROOT / "docs" / "README.md").read_text()
        for page in (ROOT / "docs" / "components").glob("*.md"):
            self.assertIn(f"components/{page.name}", index)

    def test_the_readme_no_longer_says_nothing_installs(self):
        self.assertNotIn("Nothing here installs yet", (ROOT / "README.md").read_text())

    def test_documented_commands_exist(self):
        from cw.admin import cli as admin
        from cw.client import cli as client
        current = [p for p in DOCS if p.name not in ("DESIGN.md", "REVIEW.md", "COMMANDS.md")]      # the design papers describe commands that were renamed or removed
        text = "\n".join(p.read_text() for p in current)
        for name in re.findall(r"cw-admin ([a-z][a-z-]*)", text):
            self.assertTrue(name in admin.HELP or name in ("help",), f"cw-admin {name}")
        for name in re.findall(r"cw-run ([a-z][a-z-]*)", text):
            self.assertTrue(name in client.HELP or name in ("help",), f"cw-run {name}")


if __name__ == "__main__":
    unittest.main()

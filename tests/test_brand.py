"""The splash header: the letters are drawn from a consistent glyph table, it fits the terminal, and it says what it is."""
import re
import tomllib
import unittest
from pathlib import Path

import support  # noqa: F401

from cw import __version__
from cw.setup import brand

ROOT = Path(__file__).resolve().parents[1]


def plain(lines):
    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]


class Art(unittest.TestCase):
    def test_the_picture_is_a_table_of_runs_that_fit_the_declared_width(self):
        self.assertEqual(len(brand.ART), 14)
        for row in brand.ART:
            self.assertLessEqual(sum(len(t) for _, t in row), brand.WIDTH)
            self.assertTrue(all(i is None or 0 <= i < len(brand.PALETTE) for i, _ in row))

    def test_spaced_subtitle(self):
        self.assertEqual(brand.spaced("install"), "I N S T A L L")
        self.assertEqual(brand.spaced("install preview"), "I N S T A L L   P R E V I E W")


class Banner(unittest.TestCase):
    def test_a_wide_enough_terminal_gets_the_picture_and_a_caption_naming_this_tool(self):
        lines = plain(brand.banner(100, brand.spaced("install")))
        text = "\n".join(lines)
        self.assertIn("█", text)
        self.assertIn(f"CW-BALLAST  I N S T A L L   v{__version__}", text)
        self.assertLessEqual(max(len(ln) for ln in lines), 100)

    def test_a_narrow_terminal_gets_a_short_header_instead(self):
        lines = [ln for ln in plain(brand.banner(40, "S E T U P")) if ln.strip()]
        self.assertLessEqual(len(lines), 2)
        self.assertIn("@cw-ballast", lines[0])
        self.assertIn(f"v{__version__}", "\n".join(lines))

    def test_no_colour_codes_when_colour_is_off(self):
        for cols in (100, 40):
            self.assertNotIn("\x1b", "\n".join(brand.banner(cols, "S E T U P", color=False)))

    def test_the_picture_with_colour_off_has_the_same_shapes(self):
        self.assertEqual(plain(brand.art_rows(True)), brand.art_rows(False))


class Version(unittest.TestCase):
    def test_the_version_matches_the_packaging(self):
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()

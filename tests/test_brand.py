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


class Letters(unittest.TestCase):
    def test_every_glyph_is_six_rows_of_one_width(self):
        for ch, rows in brand.GLYPHS.items():
            self.assertEqual(len(rows), 6, ch)
            self.assertEqual(len({len(r) for r in rows}), 1, f"{ch}: rows are different widths")

    def test_the_name_is_spelled_with_glyphs_that_exist(self):
        self.assertEqual(brand.NAME, "cw-ballast")
        rows = brand.word(brand.NAME)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r for r in rows))

    def test_spaced_subtitle(self):
        self.assertEqual(brand.spaced("install"), "I N S T A L L")
        self.assertEqual(brand.spaced("install preview"), "I N S T A L L   P R E V I E W")


class Banner(unittest.TestCase):
    def test_wide_terminals_get_the_mark_and_the_name(self):
        lines = plain(brand.banner(130, brand.spaced("install")))
        text = "\n".join(lines)
        self.assertIn("@@", text)
        self.assertIn("██╗", text)
        self.assertIn(f"@cw-ballast   I N S T A L L   v{__version__}", text)
        self.assertLessEqual(max(len(ln) for ln in lines), 130)

    def test_a_medium_terminal_gets_the_name_alone(self):
        lines = plain(brand.banner(100, "S E T U P"))
        self.assertNotIn("@@", "\n".join(lines[:-2]))
        self.assertLessEqual(max(len(ln) for ln in lines), 100)

    def test_a_narrow_terminal_gets_one_line(self):
        lines = [ln for ln in plain(brand.banner(60, "S E T U P")) if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("@cw-ballast", lines[0])

    def test_no_colour_codes_when_colour_is_off(self):
        self.assertNotIn("\x1b", "\n".join(brand.banner(130, "S E T U P", color=False)))

    def test_the_mark_is_a_two_tone_shape_that_fits_beside_the_letters(self):
        art = brand.mark()
        self.assertTrue({"@", "#"} <= set("".join(art)))
        self.assertLessEqual(max(len(r) for r in art), 24)


class Version(unittest.TestCase):
    def test_the_version_matches_the_packaging(self):
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()

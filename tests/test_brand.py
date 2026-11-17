"""The splash header: the letters are drawn from a consistent glyph table, it fits the terminal, and it says what it is."""
import re
import tomllib
import unittest
from pathlib import Path

import support  # noqa: F401

from ct import __version__
from ct.setup import brand

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
    def test_a_wide_enough_terminal_gets_the_picture_in_a_frame_across_its_whole_width(self):
        for cols in (60, 100, 173):
            lines = [ln for ln in plain(brand.banner(cols, brand.spaced("install"), light=False)) if ln]
            self.assertEqual({len(ln) for ln in lines}, {cols})                         # every row, frame included, is exactly as wide as the terminal
            self.assertTrue(lines[0].startswith("╭─ ") and lines[0].endswith(" ─╮"))
            self.assertTrue(lines[-1].startswith("╰") and lines[-1].endswith("╯"))
            self.assertIn("CLAUDE-TENTA  I N S T A L L", lines[0])
            self.assertTrue(lines[0].rstrip(" ─╮").endswith(f"v{__version__}"))
            self.assertIn("█", "\n".join(lines))

    def test_the_water_reaches_both_edges_and_is_one_row_shallower_at_the_same_height(self):
        rows = plain(brand.art_rows(True, 120))
        self.assertEqual(len(rows), len(brand.ART))                                     # same height as before
        self.assertTrue(all(ch != " " for ch in rows[-1] + rows[-2]))                  # the last two rows are water from edge to edge
        self.assertEqual(rows[0].strip(), "")                                           # the row that took the dropped water's place is sky

    def test_a_narrow_terminal_gets_a_short_header_instead(self):
        lines = [ln for ln in plain(brand.banner(40, "S E T U P")) if ln.strip()]
        self.assertLessEqual(len(lines), 2)
        self.assertIn("@claude-tenta", lines[0])
        self.assertIn(f"v{__version__}", "\n".join(lines))

    def test_no_colour_codes_when_colour_is_off(self):
        for cols in (100, 40):
            self.assertNotIn("\x1b", "\n".join(brand.banner(cols, "S E T U P", color=False)))

    def test_the_picture_with_colour_off_has_the_same_shapes(self):
        self.assertEqual(plain(brand.art_rows(True)), brand.art_rows(False))


class LightAndDark(unittest.TestCase):
    def test_the_near_white_parts_become_dark_on_a_light_terminal_and_nothing_else_moves(self):
        dark, light = brand.art_rows(True, 80, False), brand.art_rows(True, 80, True)
        self.assertEqual(plain(dark), plain(light))                                     # same shapes
        self.assertNotEqual(dark, light)
        self.assertIn("38;2;245;245;250", "".join(dark))
        self.assertNotIn("38;2;245;245;250", "".join(light))                             # the letters would vanish on white
        self.assertIn("38;2;38;42;76", "".join(light))

    def test_the_terminals_own_answer_decides(self):
        from ct import theme
        self.assertGreater(theme.parse_background_reply("\x1b]11;rgb:ffff/ffff/ffff\x07"), 0.9)
        self.assertLess(theme.parse_background_reply("\x1b]11;rgb:1e1e/1e1e/1e1e\x07"), 0.2)
        self.assertGreater(theme.parse_background_reply("rgb:ff/ff/ff"), 0.9)
        self.assertIsNone(theme.parse_background_reply("nothing"))
        self.assertEqual(theme.background({"CT_BACKGROUND": "light"}), "light")


class Version(unittest.TestCase):
    def test_the_version_matches_the_packaging(self):
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()

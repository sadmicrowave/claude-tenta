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
    @staticmethod
    def contrast(c1, c2):
        def lum(c):
            f = [(v / 255) / 12.92 if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4 for v in c]
            return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]
        hi, lo = sorted((lum(c1), lum(c2)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_the_name_in_the_picture_reads_on_a_white_terminal_and_on_a_dark_one(self):
        """Seen on a real dark terminal: the letters came out dark navy on dark because the background was guessed wrong. One mid-tone now serves both."""
        letters = brand.PALETTE[6]
        for background in ((255, 255, 255), (30, 30, 30), (38, 38, 38), (0, 0, 0)):
            self.assertGreaterEqual(self.contrast(letters, background), 3.0, background)

    def test_the_cloud_is_visible_on_both_too(self):
        for background in ((255, 255, 255), (30, 30, 30), (0, 0, 0)):
            self.assertGreaterEqual(self.contrast(brand.PALETTE[0], background), 2.0, background)

    def test_the_picture_is_the_same_whatever_the_terminal_is_guessed_to_be(self):
        self.assertEqual(brand.art_rows(True, 80, False), brand.art_rows(True, 80, True))

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

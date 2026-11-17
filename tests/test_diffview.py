"""Diffs are drawn like GitHub's: green for added, red for removed, blue-grey for hunks and changed files, bands full width, shades for the terminal's background."""
import re
import unittest

import support  # noqa: F401

from ct import diffview, ui

DIFF = "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@ def f():\n context\n-old line\n+new line\n"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Colours(unittest.TestCase):
    def test_each_kind_of_line_gets_its_own_colour_and_context_stays_plain(self):
        rows = diffview.lines(DIFF, 40, "light")
        by = {ANSI.sub("", r).strip(): r for r in rows}
        add, dele, hunk = by["+new line"], by["-old line"], by["@@ -1,2 +1,2 @@ def f():"]
        self.assertIn("48;2;218;251;225;38;2;17;99;41", add)            # soft green band, deep green text
        self.assertIn("48;2;255;235;233;38;2;130;6;29", dele)           # soft red band, deep red text
        self.assertIn("48;2;221;244;255", hunk)                          # neutral band
        self.assertNotIn("\x1b", by["context"])
        self.assertNotIn("48;2", by["+++ b/x.py"])                       # file headers are text only: they are not additions

    def test_a_band_reaches_both_edges_and_a_wrapped_line_keeps_its_colour(self):
        rows = diffview.lines("+" + "x" * 90, 40, "dark")
        self.assertEqual([len(ANSI.sub("", r)) for r in rows], [40, 40, 40])
        self.assertTrue(all("48;2;18;58;32" in r for r in rows))

    def test_the_shades_follow_the_terminal_background(self):
        self.assertNotEqual(diffview.code("add", "light"), diffview.code("add", "dark"))
        self.assertEqual(diffview.status_kind("A"), "add")
        self.assertEqual(diffview.status_kind("D"), "del")
        for s in ("M", "R100", "C", "T"):
            self.assertEqual(diffview.status_kind(s), "neutral")
        self.assertEqual(diffview.status_cell("src/a.py", "A", "dark")[1], diffview.code("add", "dark"))


class EveryPlace(unittest.TestCase):
    def test_every_place_that_shows_a_diff_or_line_counts_uses_the_colours(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "src" / "ct"
        shown = [f for f in src.rglob("*.py") if f.name != "diffview.py" and "diff_text(" in f.read_text().replace("def diff_text(", "")]
        self.assertTrue(shown)
        self.assertEqual([f.name for f in shown if "as_diff=True" not in f.read_text()], [], "a diff shown to a person must go through ui.document(..., as_diff=True)")
        counted = [line for f in src.rglob("*.py") if f.name != "diffview.py" for line in f.read_text().splitlines() if "+{add}" in line or "+{added}" in line]
        self.assertEqual(counted, [], "line counts must use diffview.counts")


class Counts(unittest.TestCase):
    def test_a_files_line_counts_are_green_and_red_and_read_the_same_as_plain_text(self):
        got = diffview.counts("src/a.py", "3", "1", "light")
        self.assertEqual(got.plain(), "src/a.py  +3 -1")
        self.assertIn(diffview.code("add", "light"), got.lines(60)[0])
        self.assertIn(diffview.code("del", "light"), got.lines(60)[0])
        self.assertEqual(ANSI.sub("", got.lines(60)[0]), "src/a.py  +3 -1")
        self.assertEqual(diffview.counts("img.png", "-", "-").plain(), "img.png  binary")


class Document(unittest.TestCase):
    def test_piped_output_is_the_diff_exactly_as_it_is(self):
        import io
        out = io.StringIO()
        ui.document("The change itself", DIFF, tag="DIFF", stream=out, as_diff=True)
        self.assertEqual(out.getvalue(), DIFF + "\n")


if __name__ == "__main__":
    unittest.main()

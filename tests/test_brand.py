"""The header: a short text caption that says what it is, the same at every width, with colour only when asked."""
import re
import unittest

import support  # noqa: F401

from armada import __version__
from armada.setup import brand


def plain(lines):
    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]


class Header(unittest.TestCase):
    def test_spaced_subtitle(self):
        self.assertEqual(brand.spaced("install"), "I N S T A L L")
        self.assertEqual(brand.spaced("install preview"), "I N S T A L L   P R E V I E W")

    def test_the_header_names_the_tool_the_job_and_the_version_at_every_width(self):
        for cols in (30, 40, 60, 100, 173):
            lines = [ln for ln in plain(brand.banner(cols, brand.spaced("install"))) if ln.strip()]
            self.assertLessEqual(len(lines), 2)
            self.assertIn("@armada", lines[0])
            self.assertIn(f"I N S T A L L   v{__version__}", lines[1])
            self.assertTrue(all(len(ln) <= 40 for ln in lines))

    def test_there_is_no_picture(self):
        self.assertNotIn("█", "\n".join(brand.banner(120, "S E T U P")))

    def test_no_colour_codes_when_colour_is_off(self):
        for cols in (100, 40):
            self.assertNotIn("\x1b", "\n".join(brand.banner(cols, "S E T U P", color=False)))


if __name__ == "__main__":
    unittest.main()

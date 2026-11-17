import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import theme, ui  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "cw"


class Themes(unittest.TestCase):
    def tearDown(self):
        theme.load({})

    def test_every_built_in_theme_gives_every_role_a_valid_code(self):
        for name, colors in theme.THEMES.items():
            self.assertEqual(set(colors), set(theme.ROLES), name)
            for role, code in colors.items():
                self.assertTrue(code == "" or theme.CODE_RE.match(code), (name, role))

    def test_the_main_colour_is_purple_and_blue_is_the_accent(self):
        d = theme.THEMES["default"]
        self.assertTrue(d["primary"].startswith("38;2;") and d["primary_chip"].startswith("48;2;146;46;118"))
        self.assertTrue(d["accent"].startswith("38;5;"))
        self.assertNotEqual(d["primary"], d["accent"])
        self.assertEqual(d["info"], d["primary"])                                 # neutral headings follow the main colour

    def test_truecolour_codes_are_accepted_for_any_role(self):
        colors, bad = theme.parse('[colors]\nprimary_chip = "48;2;146;46;118;38;2;255;255;255;1"\n')
        self.assertEqual(bad, [])
        self.assertTrue(colors["primary_chip"].startswith("48;2;146;46;118"))

    def test_a_file_changes_one_role_and_nothing_else(self):
        colors, bad = theme.parse('[colors]\naccent = "38;5;99"\n')
        self.assertEqual(bad, [])
        self.assertEqual(colors["accent"], "38;5;99")
        self.assertEqual(colors["ok"], theme.THEMES["default"]["ok"])

    def test_a_built_in_theme_can_be_picked_and_then_adjusted(self):
        colors, bad = theme.parse('theme = "light"\n[colors]\nok = "32"\n')
        self.assertEqual((bad, colors["ok"], colors["accent"]), ([], "32", theme.THEMES["light"]["accent"]))

    def test_a_bad_file_is_reported_and_never_reaches_the_terminal(self):
        for text in ('[colors]\nnope = "1"\n', '[colors]\nok = "1;rm -rf"\n', '[colors]\nok = "\\u001b[2J"\n', 'theme = "x"\n', 'extra = 1\n', "[[["):
            colors, bad = theme.parse(text)
            self.assertTrue(bad, text)
            self.assertTrue(all(theme.CODE_RE.match(c) or c == "" for c in colors.values()), text)

    def test_a_theme_file_restyles_everything_that_prints(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "theme.toml"
            f.write_text('[colors]\nok = "38;5;22"\n')
            theme.load({"CW_THEME_FILE": str(f)})
            self.assertEqual(theme.source, str(f))
            with mock.patch.dict(os.environ, {"CW_COLOR": "always"}):
                self.assertIn("38;5;22", ui.paint("x", ui.GREEN))
                self.assertIn("38;5;22", ui.paint("x", "ok"))

    def test_mono_has_no_colour_only_weight(self):
        theme.load({"CW_THEME": "mono"})
        self.assertEqual(theme.resolve("ok"), "1")
        self.assertEqual(theme.resolve("accent"), "")

    def test_the_theme_is_found_by_environment_then_by_home(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".cw").mkdir()
            (Path(d) / ".cw" / "theme.toml").write_text('theme = "mono"\n')
            with mock.patch.object(Path, "home", return_value=Path(d)):
                theme.load({})
            self.assertEqual(theme.code("accent"), "")


class NoColourOutsideTheTheme(unittest.TestCase):
    """The point of the theme: changing a colour never means editing a command. These stop a colour being written anywhere else."""

    def test_no_escape_colour_code_is_written_outside_theme_py(self):
        sgr = re.compile(r"(?:\\x1b|\\033)\[[^A-Za-z\"'\\]*m")
        for f in SRC.rglob("*.py"):
            if f.name != "theme.py":
                self.assertIsNone(sgr.search(f.read_text()), f"{f.relative_to(SRC)} writes a colour code; ask cw.theme for a role instead")

    def test_no_numeric_colour_is_named_outside_theme_py(self):
        number = re.compile(r"[\"']((?:38|48);5;\d+)")
        for f in SRC.rglob("*.py"):
            if f.name != "theme.py":
                self.assertIsNone(number.search(f.read_text()), f"{f.relative_to(SRC)} names a colour number; add or use a role in cw.theme")


if __name__ == "__main__":
    unittest.main()

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
from armada import theme, ui  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "armada"


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
            theme.load({"ARM_THEME_FILE": str(f)})
            self.assertEqual(theme.source, str(f))
            with mock.patch.dict(os.environ, {"ARM_COLOR": "always"}):
                self.assertIn("38;5;22", ui.paint("x", ui.GREEN))
                self.assertIn("38;5;22", ui.paint("x", "ok"))

    def test_mono_has_no_colour_only_weight(self):
        theme.load({"ARM_THEME": "mono"})
        self.assertEqual(theme.resolve("ok"), "1")
        self.assertEqual(theme.resolve("accent"), "")

    def test_the_theme_is_found_by_environment_then_by_home(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".armada").mkdir()
            (Path(d) / ".armada" / "theme.toml").write_text('theme = "mono"\n')
            with mock.patch.object(Path, "home", return_value=Path(d)):
                theme.load({})
            self.assertEqual(theme.code("accent"), "")


class FollowsTheTerminal(unittest.TestCase):
    """With no theme chosen, a light terminal gets the light theme and a dark one the default; a chosen theme is never overridden."""

    def tearDown(self):
        theme.load()

    def test_a_light_terminal_gets_the_light_theme_when_nothing_was_chosen(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ARM_THEME", "ARM_THEME_FILE")}
        with mock.patch.dict(os.environ, {**env, "ARM_BACKGROUND": "light", "HOME": tempfile.mkdtemp()}, clear=True):
            theme.load()
            self.assertEqual(theme.code("primary"), theme.THEMES["light"]["primary"])
            self.assertIn("light terminal", theme.source)
        with mock.patch.dict(os.environ, {**env, "ARM_BACKGROUND": "dark", "HOME": tempfile.mkdtemp()}, clear=True):
            theme.load()
            self.assertEqual(theme.code("primary"), theme.THEMES["default"]["primary"])

    def test_a_chosen_theme_is_never_overridden_by_the_background(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ARM_THEME", "ARM_THEME_FILE")}
        with mock.patch.dict(os.environ, {**env, "ARM_BACKGROUND": "light", "ARM_THEME": "default", "HOME": tempfile.mkdtemp()}, clear=True):
            theme.load()
            self.assertEqual(theme.code("primary"), theme.THEMES["default"]["primary"])
            self.assertEqual(theme.source, "ARM_THEME=default")

    def test_a_piped_command_never_asks_the_terminal(self):
        import io
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.dict(os.environ, {"TERM": "xterm"}):                 # output is not a terminal
            self.assertIsNone(theme._ask_terminal())                       # returns at once: nothing was written to it, nothing read


    def test_the_terminal_is_asked_through_dev_tty_even_when_stdin_is_not_the_terminal(self):
        """Seen on the real fleet: `arm-run classes </dev/null` made the colours follow a DARK default on a light terminal, because the question was only asked when stdin was a terminal."""
        import io
        written, opened = [], []

        class Out:
            def isatty(self):
                return True
        replies = iter([b"\x1b]11;rgb:ffff/ffff/ffff\x07"])
        with mock.patch.object(sys, "stdout", Out()), mock.patch.object(sys, "stdin", io.StringIO()), mock.patch.dict(os.environ, {"TERM": "xterm"}), \
                mock.patch("os.open", lambda p, *a, **k: opened.append(p) or 99), mock.patch("os.close", lambda fd: None), mock.patch("os.write", lambda fd, d: written.append((fd, d)) or len(d)), \
                mock.patch("os.read", lambda fd, n: next(replies)), mock.patch("termios.tcgetattr", lambda fd: []), mock.patch("termios.tcsetattr", lambda *a: None), \
                mock.patch("tty.setcbreak", lambda fd: None), mock.patch("select.select", lambda r, w, x, t: (r, [], [])):
            self.assertGreater(theme._ask_terminal(timeout=1), 0.9)             # white: light
        self.assertEqual(opened, ["/dev/tty"])
        self.assertEqual(written, [(99, b"\x1b]11;?\x07")])                    # asked on the terminal itself, not on stdout


class NoColourOutsideTheTheme(unittest.TestCase):
    """The point of the theme: changing a colour never means editing a command. These stop a colour being written anywhere else."""

    def test_no_escape_colour_code_is_written_outside_theme_py(self):
        sgr = re.compile(r"(?:\\x1b|\\033)\[[^A-Za-z\"'\\]*m")
        for f in SRC.rglob("*.py"):
            if f.name != "theme.py":
                self.assertIsNone(sgr.search(f.read_text()), f"{f.relative_to(SRC)} writes a colour code; ask armada.theme for a role instead")

    def test_no_numeric_colour_is_named_outside_theme_py(self):
        number = re.compile(r"[\"']((?:38|48);5;\d+)")
        for f in SRC.rglob("*.py"):
            if f.name != "theme.py":
                self.assertIsNone(number.search(f.read_text()), f"{f.relative_to(SRC)} names a colour number; add or use a role in armada.theme")


if __name__ == "__main__":
    unittest.main()

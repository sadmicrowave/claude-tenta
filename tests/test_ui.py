import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import clihelp, theme, ui  # noqa: E402


class Tty(io.StringIO):
    def isatty(self):
        return True


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CT_COLOR", "NO_COLOR")}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class Colour(unittest.TestCase):
    def test_plain_when_not_a_terminal_or_when_asked(self):
        with clean_env():
            self.assertEqual(ui.paint("x", ui.BOLD, stream=io.StringIO()), "x")
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), theme.sgr("x", "strong"))
        with clean_env(NO_COLOR="1"):
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), "x")
        with clean_env(CT_COLOR="never"):
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), "x")
        with clean_env(CT_COLOR="always"):
            self.assertEqual(ui.paint("x", ui.RED, stream=io.StringIO()), theme.sgr("x", "bad"))

    def test_no_codes_means_no_escape_sequences(self):
        with clean_env(CT_COLOR="always"):
            self.assertEqual(ui.paint("x", stream=io.StringIO()), "x")


class Say(unittest.TestCase):
    def render(self, *a, **k):
        buf = io.StringIO()
        with clean_env():
            ui.say(*a, stream=buf, **k)
        return buf.getvalue()

    def test_layout(self):
        out = self.render("Installed", ("Version", "1.2"), ("Where", "here"), "a plain line", tag="DONE", steps=["one", "two"], run=["cmd one", "cmd two"])
        self.assertEqual(out, "\nDONE  Installed\n\n  Version  1.2\n  Where    here\n  a plain line\n\nNext\n  - one\n  - two\n\nRun this\n\ncmd one\ncmd two\n\n")

    def test_each_command_is_alone_on_its_own_line(self):
        out = self.render("t", run=["git pull --ff-only", "ct-admin apply"])
        lines = out.splitlines()
        self.assertIn("git pull --ff-only", lines)
        self.assertIn("ct-admin apply", lines)

    def test_a_single_command_string(self):
        self.assertIn("\nct-admin apply\n", self.render("t", run="ct-admin apply"))

    def test_fail_prints_and_exits_with_the_code(self):
        err = io.StringIO()
        with clean_env(), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ui.fail("Broke", "because", steps=["fix it"], example="ct-admin check", code=3)
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("ERROR  Broke", err.getvalue())
        self.assertIn("How to fix it", err.getvalue())

    def test_die_splits_title_from_detail(self):
        err = io.StringIO()
        with clean_env(), redirect_stderr(err), self.assertRaises(SystemExit):
            ui.die("git failed\nfatal: not a repo")
        self.assertIn("ERROR  git failed", err.getvalue())
        self.assertIn("fatal: not a repo", err.getvalue())


class Help(unittest.TestCase):
    def setUp(self):
        self.ap = argparse.ArgumentParser(add_help=False)
        self.sub = self.ap.add_subparsers(dest="cmd")
        s = self.sub.add_parser("merge", add_help=False)
        s.add_argument("branch", nargs="?", help="the branch to merge")
        s.add_argument("--yes", action="store_true", help="do not ask")
        s.add_argument("--wait", type=int, default=5, help="seconds (default %(default)s)")
        self.details = {"merge": ("merge a branch", [("Merge one", "tool merge feat/x")])}

    def run_help(self, argv):
        out = io.StringIO()
        with clean_env(), redirect_stdout(out):
            shown = clihelp.show_cli_help(argv, "tool", "does things", self.sub, self.details, [("Do it", "tool merge feat/x")])
        return shown, out.getvalue()

    def test_overview_and_one_command(self):
        shown, text = self.run_help(["tool"])
        self.assertTrue(shown)
        for needle in ("COMMANDS", "merge a branch", "EXAMPLES", "tool help <command>"):
            self.assertIn(needle, text)
        shown, text = self.run_help(["tool", "help", "merge"])
        self.assertIn("USAGE\n  tool merge [options] [branch]", text)
        self.assertIn("--wait <n>", text)
        self.assertIn("seconds (default 5)", text)

    def test_a_normal_command_line_is_not_help(self):
        self.assertFalse(self.run_help(["tool", "merge", "feat/x"])[0])

    def test_unknown_command_is_explained(self):
        buf = io.StringIO()
        with clean_env(), redirect_stdout(buf):
            clihelp.show_cli_help(["tool", "help", "nope"], "tool", "s", self.sub, self.details, [])
        self.assertIn("No command called nope", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class Related(unittest.TestCase):
    def test_commands_that_come_next_are_in_the_house_shape_and_only_for_a_person(self):
        err = Tty()
        with clean_env():
            self.assertFalse(ui.related(("Add one", "ct-admin add <x>"), stream=err))             # stdout is not a terminal here: nothing is printed
            self.assertEqual(err.getvalue(), "")
            self.assertTrue(ui.related(("Add one", "ct-admin add <x>"), ("Never shown", None), ("Remove one", "ct-admin remove <x>"), stream=err, force=True))
        import re
        text = re.sub(r"\x1b\[[0-9;]*m", "", err.getvalue())
        self.assertIn("Add one\nct-admin add <x>\n\nRemove one\nct-admin remove <x>", text)
        self.assertNotIn("Never shown", text)

    def test_it_can_be_turned_off(self):
        with clean_env(CT_HINTS="never"):
            self.assertFalse(ui.related(("a", "b"), stream=Tty()))

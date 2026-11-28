import argparse
import base64
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
from armada import clihelp, theme, ui  # noqa: E402


class Tty(io.StringIO):
    def isatty(self):
        return True


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("ARM_COLOR", "NO_COLOR")}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class Colour(unittest.TestCase):
    def test_plain_when_not_a_terminal_or_when_asked(self):
        with clean_env():
            self.assertEqual(ui.paint("x", ui.BOLD, stream=io.StringIO()), "x")
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), theme.sgr("x", "strong"))
        with clean_env(NO_COLOR="1"):
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), "x")
        with clean_env(ARM_COLOR="never"):
            self.assertEqual(ui.paint("x", ui.BOLD, stream=Tty()), "x")
        with clean_env(ARM_COLOR="always"):
            self.assertEqual(ui.paint("x", ui.RED, stream=io.StringIO()), theme.sgr("x", "bad"))

    def test_no_codes_means_no_escape_sequences(self):
        with clean_env(ARM_COLOR="always"):
            self.assertEqual(ui.paint("x", stream=io.StringIO()), "x")


class Say(unittest.TestCase):
    def render(self, *a, **k):
        buf = io.StringIO()
        with clean_env():
            ui.say(*a, stream=buf, **k)
        return buf.getvalue()

    def test_layout(self):
        out = self.render("Installed", ("Version", "1.2"), ("Where", "here"), "a plain line", tag="DONE", steps=["one", "two"], run=["cmd one", "cmd two"])
        self.assertEqual(out, "\nDONE  Installed\n\n  Version  1.2\n  Where    here\n\n  a plain line\n\nNext\n  - one\n  - two\n\nRun this\n\ncmd one\ncmd two\n\n")

    def test_each_command_is_alone_on_its_own_line(self):
        out = self.render("t", run=["git pull --ff-only", "arm-admin apply"])
        lines = out.splitlines()
        self.assertIn("git pull --ff-only", lines)
        self.assertIn("arm-admin apply", lines)

    def test_a_single_command_string(self):
        self.assertIn("\narm-admin apply\n", self.render("t", run="arm-admin apply"))

    def test_fail_prints_and_exits_with_the_code(self):
        err = io.StringIO()
        with clean_env(), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ui.fail("Broke", "because", steps=["fix it"], example="arm-admin check", code=3)
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("ERROR  Broke", err.getvalue())
        self.assertIn("How to fix it", err.getvalue())

    def test_die_splits_title_from_detail(self):
        err = io.StringIO()
        with clean_env(), redirect_stderr(err), self.assertRaises(SystemExit):
            ui.die("git failed\nfatal: not a repo")
        self.assertIn("ERROR  git failed", err.getvalue())
        self.assertIn("fatal: not a repo", err.getvalue())


class EmptyDocument(unittest.TestCase):
    def test_an_empty_document_says_so_instead_of_drawing_an_empty_box(self):
        with clean_env():
            out = Tty()
            ui.document("Kept output", "", tag="SHOW", stream=out)
        self.assertIn("nothing to show", out.getvalue())


class CopyTheCommand(unittest.TestCase):
    """A person at a terminal gets the recommended command on the clipboard (OSC 52) and is told; nobody else is touched."""

    def test_the_first_command_is_copied_and_announced(self):
        with clean_env():
            out = Tty()
            ui.say("Stuck", tag="NOTE", run=["arm-admin wake", "arm-admin status"], stream=out)
        text = out.getvalue()
        self.assertIn("\x1b]52;c;" + base64.b64encode(b"arm-admin wake").decode() + "\x07", text)
        self.assertNotIn(base64.b64encode(b"arm-admin status").decode(), text)
        self.assertIn("copied to your clipboard", text)

    def test_nothing_is_copied_when_piped_or_turned_off_or_when_there_is_no_command(self):
        with clean_env():
            piped = io.StringIO()
            ui.say("Stuck", tag="NOTE", run="arm-admin wake", stream=piped)
            tty = Tty()
            ui.say("Stuck", tag="NOTE", stream=tty)
        with clean_env(ARM_COPY="never"):
            off = Tty()
            ui.say("Stuck", tag="NOTE", run="arm-admin wake", stream=off)
        for got in (piped, tty, off):
            self.assertNotIn("\x1b]52", got.getvalue())
            self.assertNotIn("copied to your clipboard", got.getvalue())


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
        self.assertIn("No such command", buf.getvalue())
        self.assertIn("nope", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class Related(unittest.TestCase):
    def test_commands_that_come_next_are_in_the_house_shape_and_only_for_a_person(self):
        err = Tty()
        with clean_env():
            self.assertFalse(ui.related(("Add one", "arm-admin add <x>"), stream=err))             # stdout is not a terminal here: nothing is printed
            self.assertEqual(err.getvalue(), "")
            self.assertTrue(ui.related(("Add one", "arm-admin add <x>"), ("Never shown", None), ("Remove one", "arm-admin remove <x>"), stream=err, force=True))
        import re
        text = re.sub(r"\x1b\[[0-9;]*m", "", err.getvalue())
        self.assertIn("Add one\n arm-admin add <x> \n\nRemove one\n arm-admin remove <x> ", text)
        self.assertNotIn("Never shown", text)

    def test_it_can_be_turned_off(self):
        with clean_env(ARM_HINTS="never"):
            self.assertFalse(ui.related(("a", "b"), stream=Tty()))


class TheClipboardIsReallyUsed(unittest.TestCase):
    """Found on a real Mac: the terminal ignored the escape sequence, the card said 'copied', and the clipboard held something else."""

    def test_a_clipboard_program_gets_the_first_command_and_the_card_says_what_was_copied_on_separate_lines(self):
        with clean_env():
            out, calls = Tty(), []
            with mock.patch.object(ui, "clipboard_tool", return_value=["pbcopy"]), mock.patch.object(ui, "_run_clipboard_tool", side_effect=lambda a, t: calls.append((a, t)) or True):
                ui.say("Done", tag="DONE", run=["arm-admin workers", "arm-admin watch"], stream=out)
        self.assertEqual(calls, [(["pbcopy"], "arm-admin workers")])
        lines = [theme_strip(x) for x in out.getvalue().splitlines()]
        self.assertIn("copied to your clipboard: arm-admin workers", lines)
        self.assertNotIn("paste it at the prompt", lines)
        self.assertNotIn("(ARM_COPY=never turns this off)", lines)

    def test_over_ssh_no_program_is_used_and_off_means_off(self):
        self.assertIsNone(ui.clipboard_tool(env={"SSH_CONNECTION": "x"}, which=lambda n: "/usr/bin/" + n))
        self.assertEqual(ui.clipboard_tool(env={}, which=lambda n: "/x" if n == "pbcopy" else None), ["pbcopy"])
        self.assertIsNone(ui.clipboard_tool(env={}, which=lambda n: "/x" if n == "xclip" else None))                      # no desktop, no clipboard program
        with clean_env(ARM_COPY="never"), mock.patch.object(ui, "_run_clipboard_tool") as run:
            ui.say("Done", tag="DONE", run="arm-admin workers", stream=Tty())
        run.assert_not_called()


class EveryCommandBlockIsACodeChip(unittest.TestCase):
    def test_each_command_of_a_run_list_is_a_chip_without_backticks(self):
        with clean_env():
            out = Tty()
            ui.say("Done", tag="DONE", run=["arm-admin workers", "arm-admin watch"], stream=out)
        chip = theme.sgr(" arm-admin watch ", "code")
        self.assertIn(chip, out.getvalue())
        self.assertIn(theme.sgr(" arm-admin workers ", "code"), out.getvalue())
        self.assertNotIn("`", out.getvalue())


def theme_strip(text):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text).strip()


class NoCommandIsDrawnAnyOtherWay(unittest.TestCase):
    """The standing rule: a command to copy is a code chip everywhere (cards.chip). A command painted bold green is the old look; this finds any left."""

    def test_no_source_file_paints_a_command_in_the_old_bold_green(self):
        import re
        root = Path(__file__).resolve().parents[1] / "src" / "armada"
        allowed = {"review.py": 1, "view.py": 1}          # the exact command a job will run, shown for review and in a job's details: data to read, not a command to copy
        found = {}
        for f in root.rglob("*.py"):
            n = len(re.findall(r'(?:BOLD, GREEN|GREEN, BOLD)\)|"ok", "strong"', f.read_text()))
            if n and f.name not in ("theme.py", "ui.py"):
                found[f.name] = n
        self.assertEqual({k: v for k, v in found.items() if allowed.get(k) != v}, {}, "draw it with ui.chip / cards.chip")

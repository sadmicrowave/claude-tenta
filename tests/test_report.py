import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw import theme  # noqa: E402
from cw.setup import report  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Plain(unittest.TestCase):
    def test_without_a_terminal_it_is_exactly_the_text_it_always_was(self):
        r = report.Styler(styled=False)
        self.assertEqual(r.note("ok", "tools", "git, ssh"), "  OK        tools  (git, ssh)")
        self.assertEqual(r.note("installed", "code"), "  INSTALLED code")
        self.assertEqual(r.head("Accounts"), "\nAccounts")
        self.assertEqual(r.cmd("useradd x"), "    $ useradd x")
        self.assertEqual(r.kv("Memory", "7812 MB"), "  Memory                 7812 MB")


class Styled(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("CW_COLOR")
        os.environ["CW_COLOR"] = "always"

    def tearDown(self):
        os.environ.pop("CW_COLOR") if self.old is None else os.environ.__setitem__("CW_COLOR", self.old)

    def test_statuses_are_capitals_in_one_column_and_the_actions_line_up(self):
        r = report.Styler(styled=True)
        rows = [r.note("ok", "tools"), r.note("installed", "worker account"), r.note("skipped", "Claude sign-in", "not used here"), r.note("failed", "installer")]
        plain = [ANSI.sub("", x) for x in rows]
        cols = {line.index(step) for line, step in zip(plain, ["tools", "worker account", "Claude sign-in", "installer"])}
        self.assertEqual(len(cols), 1, plain)                               # every action starts in the same column
        self.assertTrue(all(re.match(r"  [A-Z]+ +\S", line) for line in plain))
        self.assertTrue(all("\x1b[" in x for x in rows))                    # and the status is coloured

    def test_the_colour_says_what_kind_of_news_it_is(self):
        r = report.Styler(styled=True)
        self.assertIn(theme.code("ok"), r.note("installed", "x").split("INSTALLED")[0])     # green: done
        self.assertIn(theme.code("bad"), r.note("failed", "x").split("FAILED")[0])           # red: a problem
        self.assertIn(theme.code("warn"), r.note("waiting", "x").split("WAITING")[0])         # yellow: wants a look
        self.assertIn("2", r.note("skipped", "x").split("SKIPPED")[0])          # dim: nothing to do

    def test_headings_are_bold_and_coloured(self):
        head = report.Styler(styled=True).head("Hardening")
        self.assertIn("\x1b[", head)
        self.assertEqual(ANSI.sub("", head), "\n▸ Hardening")

    def test_raw_commands_are_hidden_on_a_terminal_unless_asked_for(self):
        self.assertIsNone(report.Styler(styled=True).cmd("apt-get install -y git"))
        shown = report.Styler(styled=True, verbose=True).cmd("apt-get install -y git")
        self.assertIn("$ apt-get install -y git", ANSI.sub("", shown))


if __name__ == "__main__":
    unittest.main()

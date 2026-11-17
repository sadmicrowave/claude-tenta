import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw.setup import ptyrun, tui  # noqa: E402

TOKEN = "sk-ant" + "-oat01-" + "Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_-x-Q9zZ0mmm"

# what `claude setup-token` really printed on the first run: the token wrapped over two lines, with text around it
REAL = (" \x1b[32m\u2713 Long-lived authentication token created successfully!\x1b[39m\r\n\r\n Your OAuth token (valid for 1 year):\r\n\r\n"
        " " + TOKEN[:97] + "\r\n " + TOKEN[97:] + "\r\n\r\n Store this token securely. You won't be able to see it again.\r\n\r\n"
        " Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\r\n")


class Extract(unittest.TestCase):
    def test_a_token_wrapped_over_two_lines_is_joined_and_nothing_after_it_is_added(self):
        self.assertEqual(ptyrun.extract_token(REAL), TOKEN)

    def test_a_token_on_one_line(self):
        self.assertEqual(ptyrun.extract_token(f"x\n{TOKEN}\n\nStore it\n"), TOKEN)

    def test_a_redrawn_screen_gives_the_last_complete_copy(self):
        self.assertEqual(ptyrun.extract_token(f"\x1b[2Jsk-ant-oat01-partial\n\n{REAL}"), TOKEN)

    def test_no_token_is_none(self):
        self.assertIsNone(ptyrun.extract_token("Error: could not sign in\n"))
        self.assertIsNone(ptyrun.extract_token(""))

    def test_the_example_in_the_help_text_is_not_a_token(self):
        self.assertIsNone(ptyrun.extract_token("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\n"))


class Capture(unittest.TestCase):
    def test_what_the_program_prints_is_shown_and_kept_and_its_exit_code_returned(self):
        out = io.BytesIO()
        rc, text = ptyrun.run_capture([sys.executable, "-c", "print('hello'); print('sk-ant-oat01-abc')"], out=out)
        self.assertEqual(rc, 0)
        self.assertIn("hello", text)
        self.assertEqual(ptyrun.extract_token(text), "sk-ant-oat01-abc")
        self.assertIn(b"hello", out.getvalue())

    def test_a_failing_program_returns_its_code(self):
        rc, _ = ptyrun.run_capture([sys.executable, "-c", "raise SystemExit(3)"], out=io.BytesIO())
        self.assertEqual(rc, 3)

    def test_a_missing_program_is_a_failure_not_a_crash(self):
        rc, _ = ptyrun.run_capture(["/nonexistent/program"], out=io.BytesIO())
        self.assertEqual(rc, 127)


class PastedSecrets(unittest.TestCase):
    def test_a_token_copied_from_a_wrapped_terminal_has_its_stray_space_removed(self):
        e = tui.Editor(secret=True)
        e.feed(("paste", TOKEN[:97] + "\n " + TOKEN[97:]))
        self.assertEqual(e.text, TOKEN)

    def test_an_ordinary_answer_keeps_its_spaces(self):
        e = tui.Editor()
        e.feed(("paste", "my server name"))
        self.assertEqual(e.text, "my server name")


if __name__ == "__main__":
    unittest.main()


class NewlineTranslation(unittest.TestCase):
    def test_a_terminal_in_raw_mode_gets_its_newline_handling_back(self):
        import os, pty, termios, tty
        from cw.setup import ptyrun
        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            self.assertFalse(termios.tcgetattr(slave)[1] & termios.OPOST)
            ptyrun.keep_newline_translation(slave)
            attrs = termios.tcgetattr(slave)
            self.assertTrue(attrs[1] & termios.OPOST and attrs[1] & termios.ONLCR)
            self.assertFalse(attrs[3] & termios.ICANON)                       # typing is still raw
        finally:
            os.close(master)
            os.close(slave)

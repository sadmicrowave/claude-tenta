import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct.setup import ptyrun, tui  # noqa: E402

TOKEN = "Ab1_" * 30 + "-x-Q9zZ0mmm"


class Capture(unittest.TestCase):
    def test_what_the_program_prints_is_shown_and_kept_and_its_exit_code_returned(self):
        out = io.BytesIO()
        rc, text = ptyrun.run_capture([sys.executable, "-c", "print('hello')"], out=out)
        self.assertEqual(rc, 0)
        self.assertIn("hello", text)
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
        from ct.setup import ptyrun
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

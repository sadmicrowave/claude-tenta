import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw import cards, theme, ui  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")


class Tty(io.StringIO):
    def isatty(self):
        return True


def shown(*a, **k):
    out = Tty()
    with mock.patch.dict(os.environ, {"CW_COLOR": "always"}):
        ui.say(*a, stream=out, **k)
    return out.getvalue()


class WizardCards(unittest.TestCase):
    def test_ordinary_news_is_a_rail_with_a_chip_a_plain_subject_and_muted_context(self):
        raw = shown("Ready", "It is done.", tag="DONE", color=ui.GREEN)
        plain = ANSI.sub("", raw)
        self.assertIn("┃  DONE ", plain)
        self.assertIn(theme.sgr(" DONE ", "ok_chip"), raw)                      # a filled chip
        self.assertIn(theme.sgr("Ready", "strong"), raw)                        # the subject
        self.assertIn(theme.sgr("It is done.", "muted"), raw)                   # the longer context is muted

    def test_labels_are_coloured_capitals_not_bold(self):
        raw = shown("Added", ("Program", "/x"), ("Pinned to", "ab"), tag="DONE", color=ui.GREEN)
        self.assertIn(theme.sgr("PROGRAM  ", "label"), raw)
        self.assertNotIn(theme.sgr("PROGRAM  ", "strong"), raw)

    def test_a_warning_or_removal_is_drawn_inside_an_outline_in_its_own_colour(self):
        for tag, colour, role in (("REMOVE", ui.RED, "bad"), ("NOTE", ui.YELLOW, "warn"), ("RUNS AS ROOT", ui.ROOT, "root")):
            lines = ANSI.sub("", shown("Take care", ("Program", "x"), tag=tag, color=colour)).strip("\n").split("\n")
            self.assertTrue(lines[0].startswith("╭─") and lines[0].endswith("─╮"), tag)
            self.assertTrue(lines[-1].startswith("╰") and lines[-1].endswith("╯"), tag)
            self.assertEqual({len(x) for x in lines}, {len(lines[0])}, tag)          # flush frame: every line the same visible width
            self.assertIn(theme.code(role), shown("x", tag=tag, color=colour))

    def test_long_values_wrap_inside_the_card_and_never_overflow(self):
        for tag, colour in (("DONE", ui.GREEN), ("ERROR", ui.RED)):
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((70, 24))):
                text = ANSI.sub("", shown("t", ("Pinned to", "ab" * 40), "context " * 30, tag=tag, color=colour))
            self.assertTrue(all(len(x) <= 70 for x in text.split("\n")), tag)
            self.assertEqual(text.replace("\n", "").replace("│", "").replace("┃", "").replace(" ", "").count("ab" * 40), 1)

    def test_commands_sit_beneath_the_card_alone_on_their_line(self):
        raw = shown("Ready", tag="DONE", color=ui.GREEN, run_label="Try one", run=["cw-run submit fan-profile"])
        lines = ANSI.sub("", raw).split("\n")
        self.assertIn("cw-run submit fan-profile", lines)                       # no rail, no frame beside it
        self.assertIn(" TRY ONE ", lines)

    def test_a_plain_log_keeps_the_old_one_line_shape(self):
        out = io.StringIO()
        ui.say("Ready", "It is done.", tag="DONE", stream=out)
        self.assertIn("DONE  Ready", out.getvalue())
        self.assertNotIn("┃", out.getvalue())


class ResultCard(unittest.TestCase):
    RES = {"id": "sys-status-qpr7xp", "status": "ok", "output": " up 10:58,  0 users\n", "created": "2026-11-17T05:35:28Z", "started": "2026-11-17T05:36:11Z",
           "finished": "2026-11-17T05:36:13Z", "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 50, "disk_mb": 0.0}}

    def test_a_person_watching_gets_one_card_with_the_output_and_measurements_inside(self):
        from cw.client import view
        out = Tty()
        with mock.patch.dict(os.environ, {"CW_COLOR": "always"}):
            view.print_result(self.RES, out=out, err=io.StringIO())
        lines = ANSI.sub("", out.getvalue()).strip("\n").split("\n")
        self.assertTrue(all(l.startswith("┃") for l in lines))                       # everything is inside the one card
        text = "\n".join(lines)
        for needle in ("OK", "sys-status-qpr7xp", "OUTPUT", "up 10:58,  0 users", "MEASURED", "ROUND TRIP"):
            self.assertIn(needle, text)

    def test_a_script_still_gets_the_output_alone_on_stdout(self):
        from cw.client import view
        out, err = io.StringIO(), io.StringIO()
        view.print_result(self.RES, out=out, err=err)
        self.assertEqual(out.getvalue().strip(), "up 10:58,  0 users")


class Breakout(unittest.TestCase):
    def test_the_commands_to_copy_are_their_own_breakout_with_a_chip_and_clear_space_above(self):
        raw = shown("Set up", tag="DONE", color=ui.GREEN, run_label="Try it", run=["cw-run classes", "cw-admin status"])
        lines = ANSI.sub("", raw).split("\n")
        at = lines.index(" TRY IT ")
        self.assertEqual(lines[at - 2:at], ["", ""])
        self.assertEqual(lines[at + 1:at + 4], ["", "cw-run classes", "cw-admin status"])
        self.assertIn(theme.sgr(" TRY IT ", "accent_chip"), raw)


class YesNoPrompt(unittest.TestCase):
    def test_the_question_line_is_always_in_the_prompt_colour_with_the_hint_last(self):
        with mock.patch.dict(os.environ, {"CW_COLOR": "always"}):
            text = ui.confirm_prompt("Remove it?")
        self.assertIn(theme.code("prompt"), text)
        self.assertTrue(ANSI.sub("", text).endswith("Remove it?  [y/N] "))

    def test_long_questions_wrap_at_words(self):
        with mock.patch.dict(os.environ, {"CW_COLOR": "always"}), mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((60, 24))):
            text = ANSI.sub("", ui.confirm_prompt("Remove fan-profile and install the new registry on server.example now?"))
        self.assertTrue(all(len(x) <= 60 for x in text.split("\n")))
        self.assertNotIn("claude\n-ballast", text)

    def test_no_prompt_in_the_package_is_written_by_hand(self):
        for f in (Path(__file__).resolve().parents[1] / "src" / "cw").rglob("*.py"):
            if f.name not in ("ui.py", "theme.py"):
                self.assertNotIn("[y/N]", f.read_text(), f"{f.name}: use ui.confirm_prompt")


if __name__ == "__main__":
    unittest.main()

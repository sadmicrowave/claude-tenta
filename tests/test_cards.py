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
from ct import cards, theme, ui  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")


class Tty(io.StringIO):
    def isatty(self):
        return True


def shown(*a, **k):
    out = Tty()
    with mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
        ui.say(*a, stream=out, **k)
    return out.getvalue()


class WizardCards(unittest.TestCase):
    def test_ordinary_news_is_a_rail_with_a_chip_a_plain_subject_and_muted_context(self):
        raw = shown("Ready", "It is done.", tag="DONE", color=ui.GREEN)
        plain = ANSI.sub("", raw)
        top = next(l for l in plain.split("\n") if l.startswith("╭─"))
        self.assertTrue(top.startswith("╭─ Ready ") and top.rstrip().endswith("DONE ─╮"), top)      # every card is in an outline, the tag at its right edge
        self.assertIn(theme.sgr("DONE", "ok"), raw)                             # in the colour of its kind
        self.assertIn(theme.sgr("Ready", "ok", "strong"), raw)                  # the subject, bold in the card's colour
        self.assertIn(theme.sgr("It is done.", "muted"), raw)                   # the longer context is muted

    def test_labels_are_coloured_capitals_not_bold(self):
        raw = shown("Added", ("Program", "/x"), ("Pinned to", "ab"), tag="DONE", color=ui.GREEN)
        self.assertIn(theme.sgr("PROGRAM  ", "ok"), raw)                        # in the card's own colour (green here), as capitals
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
        raw = shown("Ready", tag="DONE", color=ui.GREEN, run_label="Try one", run=["ct-run submit fan-profile"])
        lines = ANSI.sub("", raw).split("\n")
        self.assertIn(" ct-run submit fan-profile ", lines)                     # no rail, no frame beside it
        self.assertIn(" TRY ONE ", lines)

    def test_a_plain_log_keeps_the_old_one_line_shape(self):
        out = io.StringIO()
        ui.say("Ready", "It is done.", tag="DONE", stream=out)
        self.assertIn("DONE  Ready", out.getvalue())
        self.assertNotIn("┃", out.getvalue())


class ResultCard(unittest.TestCase):
    RES = {"id": "sys-status-qpr7xp", "status": "ok", "output": " up 10:58,  0 users\n", "created": "2026-11-17T05:35:28Z", "started": "2026-11-17T05:36:11Z",
           "finished": "2026-11-17T05:36:13Z", "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 50, "disk_mb": 0.0}}

    def printed(self, res=None):
        from ct.client import view
        out = Tty()
        with mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
            view.print_result(res or self.RES, out=out, err=io.StringIO())
        return out.getvalue()

    def test_the_output_is_in_its_own_box_in_the_status_colour_with_the_numbers_quietly_below(self):
        raw = self.printed()
        lines = ANSI.sub("", raw).strip("\n").split("\n")
        top = next(i for i, l in enumerate(lines) if l.startswith("╭─ Output"))
        bottom = next(i for i, l in enumerate(lines) if i > top and l.startswith("╰"))             # (the status card above it is a box too)
        self.assertIn("up 10:58,  0 users", "\n".join(lines[top:bottom]))                     # the output is inside the box
        meta = "\n".join(lines[bottom + 1:])
        for needle in ("MEASURED", "ROUND TRIP", "TOTAL", "COMING BACK"):
            self.assertIn(needle, meta)                                                        # the numbers are beneath it, not inside
        self.assertNotIn("MEASURED", "\n".join(lines[:bottom]))
        self.assertIn(theme.sgr("╭─ ", "ok"), raw)                                             # green box for a success

    def test_the_box_colour_follows_the_status(self):
        for status, role in (("error", "bad"), ("limit", "warn"), ("ok", "ok")):
            self.assertIn(theme.sgr("╭─ ", role), self.printed({**self.RES, "status": status}), status)

    def test_the_numbers_are_muted_so_they_do_not_look_like_the_job(self):
        raw = self.printed()
        self.assertIn(theme.sgr("MEASURED", "muted"), raw)
        self.assertNotIn(theme.sgr("MEASURED", "label"), raw)

    def test_a_script_still_gets_the_output_alone_on_stdout(self):
        from ct.client import view
        out, err = io.StringIO(), io.StringIO()
        view.print_result(self.RES, out=out, err=err)
        self.assertEqual(out.getvalue().strip(), "up 10:58,  0 users")


class Breakout(unittest.TestCase):
    def test_the_commands_to_copy_are_their_own_breakout_with_a_chip_and_clear_space_above(self):
        raw = shown("Set up", tag="DONE", color=ui.GREEN, run_label="Try it", run=["ct-run classes", "ct-admin status"])
        lines = ANSI.sub("", raw).split("\n")
        at = lines.index(" TRY IT ")
        self.assertEqual(lines[at - 2:at], ["", ""])
        self.assertEqual(lines[at + 1:at + 4], ["", " ct-run classes ", " ct-admin status "])
        self.assertIn(theme.sgr(" TRY IT ", "primary_chip"), raw)


class YesNoPrompt(unittest.TestCase):
    def test_the_question_line_is_always_in_the_prompt_colour_with_the_hint_last(self):
        with mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
            text = ui.confirm_prompt("Remove it?")
        self.assertIn(theme.code("prompt"), text)
        self.assertTrue(ANSI.sub("", text).endswith("Remove it?  [y/N] "))

    def test_long_questions_wrap_at_words(self):
        with mock.patch.dict(os.environ, {"CT_COLOR": "always"}), mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((60, 24))):
            text = ANSI.sub("", ui.confirm_prompt("Remove fan-profile and install the new registry on server.example now?"))
        self.assertTrue(all(len(x) <= 60 for x in text.split("\n")))
        self.assertNotIn("claude\n-tenta", text)

    def test_no_prompt_in_the_package_is_written_by_hand(self):
        for f in (Path(__file__).resolve().parents[1] / "src" / "ct").rglob("*.py"):
            if f.name not in ("ui.py", "theme.py"):
                self.assertNotIn("[y/N]", f.read_text(), f"{f.name}: use ui.confirm_prompt")


class InlineCode(unittest.TestCase):
    """A command written inside a sentence is not lost in the text around it: it gets its own background. (Asked for after a card said 'run `ct-setup install` again' and it read as plain prose.)"""
    CODE = theme.code("code")

    def test_only_the_span_between_backticks_is_in_the_code_colour(self):
        (line,) = cards.inline(["Run `ct-setup install` now"], "muted")
        self.assertEqual(ANSI.sub("", line), "Run `ct-setup install` now")                       # the same words, backticks kept, so colour-off output still shows what is code
        self.assertIn(theme.sgr("`ct-setup install`", "code"), line)
        self.assertIn(theme.sgr("Run ", "muted"), line)                                       # the words around it keep the text colour
        self.assertIn(theme.sgr(" now", "muted"), line)
        self.assertEqual(line.count(self.CODE), 1)

    def test_a_span_that_runs_over_a_line_break_is_in_the_code_colour_on_both_lines(self):
        lines = cards.inline(["Run `ssh -t agent@h 'sudo systemctl", "disable --now x' ` then stop"], None)
        self.assertIn(self.CODE, lines[0])
        self.assertIn(self.CODE, lines[1])
        self.assertEqual(ANSI.sub("", "".join(lines)), "Run `ssh -t agent@h 'sudo systemctldisable --now x' ` then stop")

    def test_text_without_backticks_is_untouched_and_an_unclosed_one_does_not_break_anything(self):
        self.assertEqual(cards.inline(["plain words"], None), ["plain words"])
        (line,) = cards.inline(["a stray ` backtick"], None)
        self.assertEqual(ANSI.sub("", line), "a stray ` backtick")

    def test_cards_style_code_in_notes_facts_and_steps(self):
        lines = cards.card("Stopped", ["Run `ct-setup install` again", ("Why", "Use `git config` here")], tag="ERROR", color="bad", steps=["Then `ct-admin status`"], cols=100)
        text = "\n".join(lines)
        for span in ("`ct-setup install`", "`git config`", "`ct-admin status`"):
            self.assertIn(theme.sgr(span, "code"), text, span)

    def test_every_theme_gives_code_a_background(self):
        for name in ("default", "light"):
            self.assertIn("48;", theme.THEMES[name]["code"], name)
        self.assertIn("code", theme.ROLES)


class CommandsInSentencesAreCodeChips(unittest.TestCase):
    """The standing rule: a command is a code chip wherever it is drawn. A command written in a sentence is found by the card itself, so no message has to remember the backticks."""

    def test_a_command_in_a_sentence_becomes_a_code_span_and_ordinary_words_around_it_do_not(self):
        for text, want in [("Run ct-setup install again once fixed.", "Run `ct-setup install` again once fixed."),
                           ("Put it on it: ssh-copy-id agent@192.0.2.5 (it asks once)", "Put it on it: `ssh-copy-id agent@192.0.2.5` (it asks once)"),
                           ("ct-admin workers lists them.", "`ct-admin workers` lists them."),
                           ("Give it: ct-setup install box.example", "Give it: `ct-setup install box.example`"),
                           ("Run ct-admin check and ct-admin status for it.", "Run `ct-admin check` and `ct-admin status` for it."),
                           ("Fix: ct-setup install --set server.worker_user=abc.", "Fix: `ct-setup install --set server.worker_user=abc`."),
                           ("Replace it: ct-admin apply --force --worker tenta3", "Replace it: `ct-admin apply --force --worker tenta3`"),
                           ("Replace it: ct-admin apply --force --worker <its name>", "Replace it: `ct-admin apply --force --worker <its name>`"),         # a placeholder may hold a space
                           ("Read it: ct-run result <job> and ct-admin status", "Read it: `ct-run result <job>` and `ct-admin status`"),
                           ("Already `ct-run x` here", "Already `ct-run x` here"),
                           ("No command in this sentence.", "No command in this sentence.")]:
            self.assertEqual(cards.code_spans(text), want)

    def test_every_place_a_card_shows_text_turns_them_into_chips(self):
        code = theme.sgr("x", "code").split("x")[0]
        lines = cards.card("T", ["see ct-admin status now", ("Fix", "run ct-setup install again")], tag="ERROR", color=ui.RED, steps=["Then run ct-admin check."], framed=True)
        text = "\n".join(lines)
        for cmd in ("`ct-admin status`", "`ct-setup install`", "`ct-admin check`"):
            self.assertIn(cmd, re.sub(r"\x1b\[[0-9;]*m", "", text))
        self.assertGreaterEqual(text.count(code), 3)


class TheReasonOfAnErrorIsAWhyBlock(unittest.TestCase):
    """The standing rule: an error card that has a reason shows it the same way everywhere: set apart by a blank line, a WHY chip in the card's colour, the text in that colour."""

    def lines(self, framed, **kw):
        return cards.card("It broke", [("Release", "Debian 13"), cards.Why("agent is not in the sudo group")], tag="NOT YET", color=ui.RED, framed=framed, **kw)

    def test_a_blank_line_then_a_red_why_chip_then_the_text_in_red(self):
        for framed in (True, False):
            raw = self.lines(framed)
            plain = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in raw]
            at = next(i for i, ln in enumerate(plain) if "WHY" in ln and "sudo group" in ln)
            self.assertEqual(plain[at - 1].strip("│┃ "), "", "a blank line separates it from the facts above")
            self.assertIn(theme.code("bad_chip"), raw[at])                         # the chip, in the colour of an error
            self.assertIn(theme.code("bad"), raw[at].split("WHY")[1])              # and the text in the same colour
            self.assertNotIn("Why not", " ".join(plain))

    def test_a_long_reason_wraps_under_its_own_text_not_under_the_chip(self):
        raw = cards.card("T", [cards.Why("word " * 40)], tag="ERROR", color=ui.RED, framed=True)
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in raw]
        rows = [ln for ln in plain if "word" in ln]
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(ln.startswith("│       word") or ln.startswith("│  WHY") or ln.startswith("│ WHY") or ln.startswith("│       ") for ln in rows), rows)

    def test_a_yellow_card_has_a_yellow_chip(self):
        raw = cards.card("T", [cards.Why("because")], tag="STOPPED", color=ui.YELLOW, framed=True)
        self.assertTrue(any(theme.code("warn_chip") in ln and "WHY" in ln for ln in raw))

    def test_plain_output_says_why_too(self):
        out = io.StringIO()
        ui.say("It broke", cards.Why("agent is not in the sudo group"), tag="ERROR", color=ui.RED, stream=out)
        self.assertIn("WHY  agent is not in the sudo group", out.getvalue())

    def test_no_error_call_in_the_source_passes_a_why_any_other_way(self):
        """Every emit/say/stop/fail/notice that is handed a variable called why (or an exception's .why) must wrap it in cards.Why, so no error card shows its reason another way."""
        import ast
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        bad = []
        for f in sorted(root.rglob("*.py")):
            tree = ast.parse(f.read_text())
            for call in ast.walk(tree):
                name = call.func.attr if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) else getattr(getattr(call, "func", None), "id", None)
                if not (isinstance(call, ast.Call) and name in ("emit", "say", "stop", "fail", "notice")):
                    continue
                for a in call.args:
                    base = a.func.value if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) else a                     # why, e.why, why.capitalize()
                    holds = [base] if (isinstance(base, ast.Name) and base.id == "why") or (isinstance(base, ast.Attribute) and base.attr == "why") else []
                    wrapped = isinstance(a, ast.Call) and getattr(a.func, "attr", getattr(a.func, "id", "")) == "Why"
                    ok_branch = isinstance(a, ast.IfExp)                                    # `why if ok else Why(why)`: the success text is not an error reason
                    if holds and not wrapped and not ok_branch:
                        bad.append(f"{f.relative_to(root)}:{call.lineno}")
        self.assertEqual(bad, [], "wrap the reason: cards.Why(why)")


class ALiftedCommandKeepsTheSentenceReadable(unittest.TestCase):
    def test_a_command_that_was_the_subject_becomes_the_command_below_not_a_bracket(self):
        found = []
        text = cards.lift("`gh repo create agent/x --private --description 'claude-tenta queue (private)'` said: GraphQL: you do not have the correct permissions to execute CreateRepository", 40, found)
        self.assertTrue(text.startswith("The command below said:"), text)
        self.assertEqual(len(found), 1)
        found = []
        self.assertIn("(command below)", cards.lift("Do this now: su - -c 'apt-get install -y sudo && usermod -aG sudo agent'", 30, found))


if __name__ == "__main__":
    unittest.main()


class StyledValues(unittest.TestCase):
    def test_a_long_explanation_wraps_under_itself_and_nothing_is_cut_off(self):
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((70, 24))), mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
            raw = shown("status", ("Fetch", cards.Styled([("OK", "ok"), ("  last pass 1 s ago, took 0 s (pulling new jobs from GitHub; asleep, so it asks GitHub every 300 s)", "muted")])), tag="STATUS")
        text = ANSI.sub("", raw)
        self.assertTrue(all(len(x) <= 70 for x in text.split("\n")))
        flat = text.replace("│", "").replace("\n", " ")
        for needle in ("pulling new jobs from GitHub", "asleep", "every 300 s"):
            self.assertIn(needle, " ".join(flat.split()))                                          # all of it is there, on more than one line

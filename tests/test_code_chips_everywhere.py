"""The standing rule: a command is a code chip EVERYWHERE it is drawn, whoever wrote the message and wherever it appears (an error, a step, a question, a progress line, a prompt).

Three gates, so no path can skip it:
  1. one detector (cards.code_spans) that reads any command in a sentence, ours or the system's;
  2. every renderer of text goes through it (a source scan fails on any new place that wraps text without it);
  3. every message in the whole source that holds a command is checked to come out as a chip, so a form the detector cannot read is found here, not by a person on a screen."""
import ast
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from ct import cards, theme, ui
from ct.setup import report, tui

SRC = Path(__file__).resolve().parents[1] / "src" / "ct"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Tty(io.StringIO):
    def isatty(self):
        return True


def has_chip(raw, command):
    """True when `command` is drawn inside a code-coloured span."""
    code = theme.code("code")
    return bool(re.search(re.escape(code) + r"[^\x1b]*" + re.escape(command), raw)) or (command in raw and code in raw and "`" + command in ANSI.sub("", raw).replace(" ", " "))


class TheDetector(unittest.TestCase):
    def test_the_commands_seen_in_real_screens_are_all_found(self):
        for text, chip in [("Install sudo and add the account to the sudo group: su -c 'apt-get install sudo && usermod -aG sudo agent'", "`su -c 'apt-get install sudo && usermod -aG sudo agent'`"),
                           ("Put this computer's key on it: ssh-copy-id agent@192.0.2.5 (it asks once)", "`ssh-copy-id agent@192.0.2.5`"),
                           ("Forget the old one with  ssh-keygen -R {}", "`ssh-keygen -R {}`"),
                           ("Check that its SSH server runs (sudo systemctl status ssh).", "(`sudo systemctl status ssh`)"),
                           ("Read why with: journalctl -u {} -n 20 --no-pager", "`journalctl -u {} -n 20 --no-pager`"),
                           ("Use curl -fsSL https://example.org/x | sh to install it.", "`curl -fsSL https://example.org/x | sh`"),
                           ("Run: cat /etc/os-release", "`cat /etc/os-release`"),
                           ("Run ct-admin check and ct-admin status for it.", "`ct-admin check` and `ct-admin status`"),
                           ("Then run: git config --global user.name \"Name\" and retry.", "`git config --global user.name \"Name\"` and retry.")]:
            self.assertIn(chip, cards.code_spans(text), text)

    def test_ordinary_sentences_are_left_alone(self):
        for text in ("The sudo group needs the account. sudo is not installed.", "It may find your files, and it does not cat anything.", "Install sudo and reboot the machine when asked.",
                     "sudo is not installed", "Why not: sudo is not installed", "Note: git is needed.", "Nothing was changed on that computer.", "Claude can read the files you attach."):
            self.assertEqual(cards.code_spans(text), text)

    def test_it_is_idempotent_and_leaves_written_backticks_alone(self):
        for text in ("Run `ct-admin check` now.", "Put the key on it: ssh-copy-id agent@192.0.2.5"):
            once = cards.code_spans(text)
            self.assertEqual(cards.code_spans(once), once)


class EveryRendererDrawsThemAsChips(unittest.TestCase):
    """One call per place text is drawn; each must show the command in the code colour."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CT_COLOR": "always", "CW_COLOR": "always"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cmd = "ssh-copy-id agent@192.0.2.5"
        self.sentence = f"Put the key on it: {self.cmd} (it asks once)"

    def assertChip(self, raw, where):
        code = theme.code("code")
        self.assertIn(code, raw, f"{where}: no code colour at all")
        self.assertRegex(raw, re.escape(code) + r"[^\x1b]*" + re.escape(self.cmd), where)

    def test_a_card_in_its_details_facts_steps_and_notes(self):
        for kind, call in (("detail", lambda: cards.card("T", [self.sentence], tag="ERROR", color=ui.RED)),
                           ("fact", lambda: cards.card("T", [("Fix", self.sentence)], tag="ERROR", color=ui.RED)),
                           ("step", lambda: cards.card("T", [], tag="ERROR", color=ui.RED, steps=[self.sentence])),
                           ("framed", lambda: cards.card("T", [self.sentence], tag="ERROR", color=ui.RED, framed=True))):
            self.assertChip("\n".join(call()), f"card {kind}")

    def test_the_setup_report_lines_notes_warnings_and_errors(self):
        st = report.Styler(styled=True, stream=Tty())
        for call in (lambda: st.line(self.sentence), lambda: st.line("NOTE " + self.sentence), lambda: st.line("ERROR " + self.sentence), lambda: st.warn(self.sentence),
                     lambda: st.error(self.sentence), lambda: st.note("done", self.sentence)):
            self.assertChip(call(), "report")

    def test_the_questions_their_explanation_risk_examples_choices_and_problem(self):
        p = tui.TuiPrompter(out=io.StringIO(), width=100, rows=60, keys=iter(()))
        q = {"key": "x.y", "title": "T", "why": self.sentence, "risk": self.sentence, "choices": [("a", self.sentence)], "required": True}
        raw = "\n".join(p.ask_lines(q, "", [], problem=self.sentence, shown="", view={"items": [], "pos": 0, "total": 1, "can_back": False, "prefill": ""}) if hasattr(p, "ask_lines") else [])
        if not raw:
            raw = "\n".join(p.labelled("RISK", "label", self.sentence) + p.choice_lines(q, "a") + p.inline(cards.wrap_text(self.sentence, 80), "muted"))
        self.assertChip(raw, "question")

    def test_a_yes_no_prompt(self):
        self.assertChip(ui.confirm_prompt(f"Run {self.cmd} now?", Tty()), "confirm_prompt")

    def test_the_framed_box_of_the_wizard(self):
        out = io.StringIO()
        tui.TuiPrompter(out=out, width=100, rows=60, keys=iter(())).box("T", "NOTICE", [self.sentence])
        self.assertChip(out.getvalue(), "box")


class NothingWrapsTextAroundTheDetector(unittest.TestCase):
    def test_every_place_that_wraps_text_for_the_screen_uses_the_one_wrapper_or_says_why_not(self):
        """A new renderer that calls textwrap on its own would skip the chips. It must use cards.wrap_text, or carry `# nochip: <why>` (data shown as it is, a list of commands)."""
        found = []
        for f in sorted(SRC.rglob("*.py")):
            if f.name == "cards.py":
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"textwrap\.(wrap|fill)\(|__import__\(\"textwrap\"\)", line) and not re.search(r"code_spans\(|wrap_text\(|# nochip:", line):
                    found.append(f"{f.relative_to(SRC)}:{n}")
        self.assertEqual(found, [], "use cards.wrap_text (or say `# nochip: why`)")


class EveryMessageInTheSourceThatHoldsACommandComesOutAsAChip(unittest.TestCase):
    """The inventory: all text in the source that reads like a sentence with a command in it. Each must come out of the detector with that command inside a code span."""
    EXEC = r"(?:ct-(?:setup|admin|run|worker|update)|ssh-copy-id|ssh-keygen|sudo systemctl|systemctl|journalctl|apt-get|usermod|su -c)"

    def messages(self):
        for f in sorted(SRC.rglob("*.py")):
            tree = ast.parse(f.read_text())
            docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
            for n in ast.walk(tree):
                if id(n) in docs:
                    continue
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    yield f, n.lineno, n.value
                elif isinstance(n, ast.JoinedStr):
                    yield f, n.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)

    def test_each_command_written_after_a_colon_or_run_is_inside_a_code_span(self):
        missed, seen = [], 0
        for f, line, s in self.messages():
            if len(s.split()) < 4 or s.startswith(("ct-", "env ", "cat ", "sudo ", "ssh ")) or "^" in s or "(?" in s or s.endswith(" "):             # a pattern, not a message; or the front half of a message finished at run time (checked whole, by the renderer tests)
                continue
            for m in re.finditer(r"(?:: |[Rr]un |with  ?|\()(" + self.EXEC + r")\b", re.sub(r"`[^`]*`", "", s)):
                seen += 1
                out = cards.code_spans(s)
                if not re.search(r"`[^`]*" + re.escape(m.group(1)) + r"[^`]*`", out):
                    missed.append(f"{f.relative_to(SRC)}:{line}: {s[:80]!r}")
        self.assertGreater(seen, 20, "the inventory found nothing: the scan is broken")
        self.assertEqual(missed, [], f"{len(missed)} of {seen} commands in messages are not chipped")


if __name__ == "__main__":
    unittest.main()

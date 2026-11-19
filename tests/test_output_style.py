"""Every line the tool shows a person goes through the card style, and this test is what keeps it that way.

Nothing in the package may print on its own: a person at a terminal sees cards, rail rows, tables and chips (cw.ui, cw.cards, cw.setup.report), and a pipe or a log
sees the plain text of the same thing. The few places that must write something else are marked on their line with `# output: <kind>`:

  data         what a script reads (JSON, a job id, a file's own text)
  log          a line for the journal of a service nobody is watching
  passthrough  another program's own terminal output
  a card / plain text for a pipe / the live status line / pre-drawn lines   the renderers themselves

An unmarked print is a failure here, so a new command cannot quietly bring back unformatted output.
"""
import ast
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
from ct.setup import report  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "ct"
RENDERERS = {"ui.py", "cards.py", "theme.py", "brand.py", "tui.py"}         # these ARE the drawing code
STREAMS = {"sys.stdout", "sys.stderr", "self.stream", "stream", "out", "self.out", "err"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")


class Tty(io.StringIO):
    def isatty(self):
        return True


class NothingPrintsOnItsOwn(unittest.TestCase):
    def sites(self):
        for f in sorted(SRC.rglob("*.py")):
            if f.name in RENDERERS:
                continue
            text = f.read_text()
            lines = text.splitlines()
            for n in ast.walk(ast.parse(text)):
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                hit = name == "print" or (name == "write" and isinstance(fn, ast.Attribute) and ast.unparse(fn.value) in STREAMS)
                if hit:
                    span = "\n".join(lines[n.lineno - 1:n.end_lineno])
                    yield f.relative_to(SRC), n.lineno, "# output:" in span

    def test_every_print_and_stream_write_is_marked_with_what_it_is(self):
        loose = [f"{f}:{n}" for f, n, marked in self.sites() if not marked]
        self.assertEqual(loose, [], "these print without going through the card style (use ct.ui.say / ui.table / ui.document, or mark a data or log line `# output: <kind>`)")

    def test_nothing_defaults_to_the_builtin_print(self):
        bad = []
        for f in sorted(SRC.rglob("*.py")):
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, ast.FunctionDef):
                    for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                        if isinstance(d, ast.Name) and d.id == "print":
                            bad.append(f"{f.relative_to(SRC)}:{n.lineno} {n.name}")
        self.assertEqual(bad, [], "a printer that defaults to print() bypasses the style; default to None and use report.sink()")


class TheSetupPrinter(unittest.TestCase):
    def lines(self, *texts):
        out = Tty()
        with mock.patch.dict(os.environ, {"CT_COLOR": "always", "CW_COLOR": "always"}):
            say = report.sink(out)
            for t in texts:
                say(t)
        return out.getvalue().rstrip("\n").split("\n")

    def test_every_line_sits_on_the_rail(self):
        got = self.lines("Checking the machine", "  an indented detail", "NOTE something to read", "ERROR it broke", "OK  all fine", "    $ systemctl start x", "\nAfter a blank")
        plain = [ANSI.sub("", ln) for ln in got]
        self.assertTrue(all(ln.startswith("┃") for ln in plain), plain)

    def test_a_note_and_an_error_say_what_they_are_in_their_own_colours(self):
        raw = "\n".join(self.lines("NOTE read this", "ERROR it broke"))
        self.assertIn(theme.code("warn"), raw)
        self.assertIn(theme.code("bad"), raw)

    def test_a_line_already_styled_passes_through_and_plain_mode_is_untouched(self):
        st = report.Styler(styled=True, stream=Tty())
        with mock.patch.dict(os.environ, {"CT_COLOR": "always", "CW_COLOR": "always"}):
            row = st.note("done", "step", "detail")
            self.assertEqual(st.line(row), row)
        plain = report.Styler(styled=False)
        self.assertEqual(plain.line("NOTE  x"), "NOTE  x")


class Primitives(unittest.TestCase):
    def shown(self, fn, *a, **k):
        out = Tty()
        with mock.patch.dict(os.environ, {"CT_COLOR": "always", "CW_COLOR": "always"}):
            fn(*a, stream=out, **k)
        return ANSI.sub("", out.getvalue())

    def test_a_table_is_a_card_with_capital_column_labels_and_coloured_cells(self):
        text = self.shown(ui.table, "Things", ["name", "state"], [[("a", "ok"), "fine"], ["b", ("bad", "bad")]], tag="LIST")
        self.assertIn("╭─", text)                                                       # every card has the outline
        self.assertIn("NAME", text)
        self.assertIn("STATE", text)

    def test_an_empty_table_says_so_instead(self):
        text = self.shown(ui.table, "Things", ["name"], [], empty="Nothing here.")
        self.assertIn("Nothing here.", text)

    def test_a_document_is_a_framed_card_when_watched_and_the_bare_text_when_piped(self):
        text = self.shown(ui.document, "A file", "line one\nline two", tag="SHOW")
        self.assertIn("╭─", text)
        self.assertIn("line two", text)
        out = io.StringIO()
        ui.document("A file", "line one\nline two", stream=out)
        self.assertEqual(out.getvalue(), "line one\nline two\n")

    def test_a_command_label_is_shown_as_typed_not_in_capitals(self):
        text = self.shown(ui.say, "Help", (cards.Cmd("ct-run classes"), "what it can run"), tag="HELP")
        self.assertIn("ct-run classes", text)
        self.assertNotIn("CT-RUN CLASSES", text)


if __name__ == "__main__":
    unittest.main()

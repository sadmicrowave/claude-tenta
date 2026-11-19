"""The lessons from reviewing real printouts, as rules for EVERY printout in the app (not just the one that showed the problem). Each test is one lesson and scans the whole source, so a new message
cannot skip it. The commands-are-chips, long-commands-below, reason-is-a-WHY and release-is-a-fleet-fact rules have their own files (test_code_chips_everywhere, test_cards, test_admin)."""
import ast
import re
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import cards, ui, words

SRC = Path(__file__).resolve().parents[1] / "src" / "ct"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strings():
    """(file, line, text) of every string constant and f-string in the source, with the pieces of an f-string joined (a {} stands for the value)."""
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text())
        parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for n in ast.walk(tree):
            if id(n) in parts or id(n) in docs:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield f, n.lineno, n.value
            elif isinstance(n, ast.JoinedStr):
                yield f, n.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)


class CountsAgreeWithTheirNouns(unittest.TestCase):
    """Seen on real screens: '1 cores', '1 days old', '1 thing(s)'. A count is written through words.plural."""

    def test_plural_agrees(self):
        self.assertEqual([words.plural(n, "file") for n in (0, 1, 2)], ["0 files", "1 file", "2 files"])
        self.assertEqual(words.plural(1, "class"), "1 class")
        self.assertEqual(words.plural(2, "class"), "2 classes")
        self.assertEqual(words.plural(2, "undeclared output field"), "2 undeclared output fields")
        self.assertEqual(words.plural(2, "child", "children"), "2 children")

    def test_no_message_says_noun_s_in_brackets(self):
        bad = [f"{f.relative_to(SRC)}:{n}: {t[:60]!r}" for f, n, t in strings() if re.search(r"[a-z]\(s\)", t) and "yes(s)" not in t and not t.startswith(("yes(", "\n    yes("))]
        self.assertEqual(bad, [], "use words.plural(n, 'noun')")

    def test_a_counted_noun_that_can_be_one_is_not_written_as_a_fixed_plural(self):
        """{expr} cores / files / days / classes ... in an f-string: the count can be 1."""
        nouns = "files|cores|days|jobs|classes|packages|commits|keys|workers|minutes|hours|results|turns|errors|lines"
        bad = []
        for f in sorted(SRC.rglob("*.py")):
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, ast.JoinedStr):
                    v = n.values
                    for i, x in enumerate(v):
                        if isinstance(x, ast.FormattedValue) and i + 1 < len(v) and isinstance(v[i + 1], ast.Constant) and isinstance(v[i + 1].value, str) and not isinstance(x.value, ast.Call):
                            if re.match(r" (?:(?:job|quiet|independent) )?(?:" + nouns + r")\b", v[i + 1].value):
                                bad.append(f"{f.relative_to(SRC)}:{n.lineno}: {ast.unparse(x.value)[:40]}")
        self.assertEqual(bad, [], "use words.plural(n, 'noun')")


class EveryTimeSaysItsZone(unittest.TestCase):
    """Seen on a real fleet: one worker's card said CST while the others said UTC. A printed time carries its zone."""

    def test_every_time_format_in_the_source_has_a_zone_or_is_a_date(self):
        bad = []
        for f in sorted(SRC.rglob("*.py")):
            lines = f.read_text().split("\n")
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "strftime":
                    fmt = n.args[0] if n.args else None
                    if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str) and "%H" in fmt.value:
                        line = lines[n.lineno - 1]
                        if "%Z" not in fmt.value and "UTC" not in line and not fmt.value.endswith("Z") and "Z" not in line.split("strftime", 1)[1][:40]:
                            bad.append(f"{f.relative_to(SRC)}:{n.lineno}: {fmt.value}")
        self.assertEqual(bad, [], "say the zone (%Z, or UTC when the time is in UTC)")


class OneOutline(unittest.TestCase):
    """Every card is in the same outline. A renderer that draws its own rail instead is a second look."""
    ALLOWED = {"cards.py": "the card renderer itself", "setup/report.py": "the setup's streamed lines (a section that is never closed while it runs)",
               "setup/tui.py": "the wizard's question screens", "updatecheck.py": "the one quiet hint line after a command"}

    def test_no_other_file_draws_its_own_rail(self):
        bad = []
        for f in sorted(SRC.rglob("*.py")):
            rel = str(f.relative_to(SRC))
            if rel in self.ALLOWED:
                continue
            for n, line in enumerate(f.read_text().split("\n"), 1):
                if "┃" in line and not line.strip().startswith("#"):
                    bad.append(f"{rel}:{n}")
        self.assertEqual(bad, [], "draw it with cards.card (or add a reason to ALLOWED)")

    def test_a_card_with_nothing_in_it_is_a_line_not_an_empty_frame(self):
        lines = cards.card("status: tenta2", [], tag="WORKER", color=ui.PRIMARY)
        plain = [ANSI.sub("", ln).strip() for ln in lines if ln.strip()]
        self.assertEqual(len(plain), 1, plain)
        self.assertTrue(plain[0].startswith("── status: tenta2") and plain[0].endswith("WORKER"), plain)

    def test_the_workers_list_is_one_framed_card_per_worker(self):
        from ct.admin import workers
        row = {"name": "tenta2", "os": "ubuntu 24.04", "cpu": "2 cores", "memory": "2 of 3 GB free", "claude": "none", "spend": "0/20 jobs", "state": "jail ON", "release": "f4c9ae706", "load": "0.00",
               "age": "5 min ago", "has_claude": False, "locked": False, "stale": False, "packages": 0, "classes": 3}
        out = [ANSI.sub("", x) for x in workers.cards([row, {**row, "name": "tenta3", "locked": True}], width=80, color=True)]
        self.assertEqual(sum(x.startswith("╭─") for x in out), 2)
        self.assertEqual(sum(x.startswith("╰") for x in out), 2)
        self.assertFalse(any("┃" in x for x in out))
        self.assertTrue(any(x.startswith("╭─ tenta2") and x.rstrip().endswith("WORKER ─╮") for x in out))
        self.assertTrue(any(x.startswith("╭─ tenta3") and x.rstrip().endswith("LOCKED ─╮") for x in out))


class AFleetCommandSaysWhatHappenedOnce(unittest.TestCase):
    """Visiting several workers: a heading line each (no empty frames), a worker that needed nothing does not get two cards, and one summary at the end."""

    def test_the_summary_names_who_was_updated_who_was_current_and_who_was_left_alone(self):
        import io
        from contextlib import redirect_stdout
        from ct.admin import cli
        out = io.StringIO()
        with redirect_stdout(out):
            cli.fleet_apply_summary([("a", "current"), ("b", "installed"), ("c", "left")], "f" * 40)
        text = ANSI.sub("", out.getvalue())
        for needle in ("apply finished: 3 workers", "Updated", "Already current", "Left alone", "fffffff"):
            self.assertIn(needle, text)


class FooterWords(unittest.TestCase):
    """Standing rules on the command footer: it says 'From here you can' (one definition), and the clipboard note does not tell the person to paste (the line above already says it was copied)."""

    def test_the_footer_title_is_defined_once_and_says_you_can(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        hits = [f"{f.relative_to(root)}:{n}" for f in root.rglob("*.py") for n, l in enumerate(f.read_text().splitlines(), 1) if '"From here"' in l and "title=" in l]
        self.assertEqual(hits, [], "the footer's words live in ui.related only")
        self.assertIn('title="From here you can"', (root / "ui.py").read_text())

    def test_nothing_tells_the_person_to_paste_it_at_the_prompt(self):
        root = Path(__file__).resolve().parents[1]
        bad = [str(f.relative_to(root)) for d in ("src", "docs") for f in (root / d).rglob("*") if f.is_file() and f.suffix in (".py", ".md") and ("paste it at the prompt" in f.read_text() or "turns this off)" in f.read_text())]
        self.assertEqual(bad, [])


class NamesAreValuesNeverKeys(unittest.TestCase):
    """Standing rule: text a person can choose (a worker or host name, a command, a path) is never a row label. Labels are a fixed vocabulary of one width; a name is a value, with what is said about
    it on the line below, so a long name wraps in its own space and cannot widen the label column or squeeze the others."""
    LONG_HOST, LONG_SLUG = "a-very-long-worker-hostname-for-the-lab.example", "integration-test-suite-nightly-regression"
    FIXED = {"cli.py": {"c"}, "client/cli.py": {"k.capitalize()", "what"}, "client/view.py": {"word"}}                 # the command names in help, the fixed words of a job's facts and of the status legend

    def test_no_card_row_is_labelled_by_something_a_person_named(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        funs, bad = {"emit", "notice", "card", "say", "Card"}, []
        name = lambda f: f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")               # noqa: E731
        for f in root.rglob("*.py"):
            for call in ast.walk(ast.parse(f.read_text())):
                if not (isinstance(call, ast.Call) and name(call.func) in funs):
                    continue
                safe = {id(x) for inner in ast.walk(call) if isinstance(inner, ast.Call) and name(inner.func) == "named" for x in ast.walk(inner)}
                for a in list(call.args[1:]) + [k.value for k in call.keywords]:
                    for t in ast.walk(a):
                        if (isinstance(t, ast.Tuple) and isinstance(t.ctx, ast.Load) and len(t.elts) == 2 and id(t) not in safe and not isinstance(t.elts[0], (ast.Tuple, ast.Constant)) and not self.fixed_label(t.elts[0])
                                and ast.unparse(t.elts[0]) not in self.FIXED.get(str(f.relative_to(root)), ())):
                            bad.append(f"{f.relative_to(root)}:{t.lineno} {ast.unparse(t.elts[0])}")
        self.assertEqual(bad, [], "a name is a value: use cards.named(...) or a fixed label")

    @staticmethod
    def fixed_label(node):
        """A label that is a fixed word: a Chip of a constant, or a choice between constants ('CLASSES' on the first row, '' on the others)."""
        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "Chip":
            return all(isinstance(a, ast.Constant) for a in node.args)
        if isinstance(node, ast.IfExp):
            return isinstance(node.body, ast.Constant) and isinstance(node.orelse, ast.Constant)
        return False

    def render(self, rows, title="A card"):
        text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", l) for l in cards.card(title, rows, tag="TEST", color="cyan", cols=70))
        widths = {len(l) for l in text.splitlines()}
        self.assertEqual(len(widths), 1, f"the card is not one width:\n{text}")
        return text

    def test_where_keeps_long_worker_names_whole_and_the_label_column_fixed(self):
        from ct import sched
        from ct.client import place
        ch = sched.Choice("a", [("a", "CHOSEN", "free"), (self.LONG_HOST, "left out", "is reserved for sys-status"), ("b", "not chosen", "a is less loaded")])
        text = self.render(place.rows(ch))
        self.assertIn(self.LONG_HOST, text)
        starts = {m.start(2) for m in (re.match(r"^│\s+(CHOSEN|LEFT OUT|NOT CHOSEN)\s+(\S+)", l) for l in text.splitlines()) if m}
        self.assertEqual(len(starts), 1, f"the names do not start in one column:\n{text}")
        green = "\n".join(cards.card("t", place.rows(ch), tag="T", color="cyan", cols=70))
        self.assertIn("CHOSEN", green)
        self.assertIn("\x1b[", green)                                                                 # drawn as a chip (colour is on in the card renderer)

    def test_use_lists_each_workers_commands_under_it_with_no_command_label(self):
        from ct.client import cli, here
        pins = here.Pins(Path("/tmp/x/.tenta.toml"), worker=self.LONG_HOST, commands={self.LONG_SLUG: self.LONG_HOST, "nmap": "tenta3"})
        text = self.render(cli.use_rows(pins))
        for needle in (self.LONG_HOST, self.LONG_SLUG, "JOBS WORKER", "WORKER"):
            self.assertIn(needle, text)
        self.assertNotIn("COMMAND ", text)
        self.assertNotIn("EVERY JOB", text)
        self.assertNotIn("ONLY ", text)

    def test_the_named_helper_puts_the_name_in_the_value_and_the_text_below_it(self):
        text = self.render(cards.named([(self.LONG_HOST, "f4c9ae706  behind"), ("b", "current")]))
        self.assertIn(self.LONG_HOST, text)
        self.assertEqual(text.count("WORKER"), 2)


    def test_a_long_name_in_a_title_does_not_break_the_frame(self):
        text = self.render([("File", "x")], title=f"From this folder, {self.LONG_SLUG} goes to {self.LONG_HOST}")
        self.assertIn(self.LONG_HOST, text)
        self.assertEqual(text.splitlines()[0].count("╭"), 1)

    def test_the_heading_line_of_an_empty_card_keeps_one_width_with_a_long_name(self):
        line = re.sub(r"\x1b\[[0-9;]*m", "", cards.rule(f"{self.LONG_SLUG} on {self.LONG_HOST}", "WORKER", "cyan", cols=70))
        self.assertTrue(all(len(l) <= 72 for l in line.splitlines()), line)


    def test_one_long_entry_in_a_table_cannot_push_the_columns_after_it(self):
        rows = [["a", "x", "ok"], [self.LONG_HOST + "-" + self.LONG_SLUG, "y", "ok"]]
        lines = cards.table_lines(["name", "kind", "state"], rows, 100, color=False)
        cols = {l.rindex("ok") for l in lines[1:]}
        self.assertEqual(len(cols), 1, lines)                                           # 'state' starts in one column however long the first cell is

    def test_the_classes_lists_have_fixed_labels_and_names_as_values(self):
        import io
        from ct.client import view
        info = {"cost": "light", "tier": "auto", "kind": "command", "run": ["x"]}
        out = io.StringIO()
        view.render_fleet_classes({self.LONG_SLUG: {"info": info, "offered_by": [self.LONG_HOST], "dedicated": None}}, out=out)
        text = out.getvalue()
        self.assertIn(self.LONG_SLUG, text)
        self.assertIn(self.LONG_HOST, text)


class NotesAreSetApartFromRows(unittest.TestCase):
    """Standing rule: in every card, of every kind, a note (the muted description) that follows labelled rows has a blank line above it. One rule in the shared renderer, checked for each role."""
    ROLES = ("cyan", "green", "yellow", "red", "primary", "root")

    def test_every_card_kind_has_a_blank_line_between_its_rows_and_a_note(self):
        for color in self.ROLES:
            for framed in (True, False):
                text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", l) for l in cards.card("T", [("KEY", "value"), "A note about the rows above."], tag="TAG", color=color, framed=framed, cols=70))
                lines = text.splitlines()
                i = next(n for n, l in enumerate(lines) if "A note" in l)
                self.assertTrue(lines[i - 1].strip(" │┃") == "", f"{color} framed={framed}: no blank line above the note\n{text}")

    def test_a_note_that_opens_the_card_still_hugs_the_title_and_after_a_why_has_a_blank_line(self):
        framed = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in cards.card("T", ["Opening note.", ("KEY", "v")], tag="TAG", color="cyan", cols=70)]
        i = next(n for n, l in enumerate(framed) if "Opening note" in l)
        self.assertEqual(framed[i - 1].strip("│ "), "")                                                  # the frame's own blank row below the top edge, nothing more
        why = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in cards.card("T", [cards.Why("because"), "after the reason"], tag="TAG", color="red", cols=70)]
        j = next(n for n, l in enumerate(why) if "after the reason" in l)
        self.assertEqual(why[j - 1].strip("│ "), "")

    def test_plain_text_follows_the_same_rule(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            ui.say("T", ("KEY", "value"), "A note about the rows above.", tag="TAG")
        lines = err.getvalue().splitlines()
        i = next(n for n, l in enumerate(lines) if "A note" in l)
        self.assertEqual(lines[i - 1], "")


class NamesNeverInTitles(unittest.TestCase):
    """Standing rule: a card's frame title is a fixed sentence. Anything a person can name (a worker or host, a command or class, a job, a branch, a file or path, an address) is a labelled row in the card
    body, on its own line, never part of the title. A number counted with plural() is fine. The setup streams and the install wizard are separate renderers; the exceptions below are the whole list."""
    FUNS = {"emit", "notice", "card", "say", "Card", "fail", "die", "document", "table", "rule", "ClientError", "stop", "SetupError"}
    SKIP = {"tui.py", "report.py", "install.py", "server.py", "keys.py", "questions.py", "tripwire.py", "uninstall.py", "audit.py", "client.py"}      # streamed setup lines and the wizard
    ALLOWED = {("admin/cli.py", "what"), ("admin/cli.py", "ctx.store"),                                # a fixed verb phrase ('start'); GitHub or the mailbox
               ("admin/cli.py", "a.cmd"), ("admin/cli.py", "name"), ("admin/cli.py", "alias.show(name)"),                                   # the one-line heading that says which worker the output below is about (a heading, not a card)
               ("cli.py", "len(items) - bad"), ("cli.py", "bad"),                                      # counts
               ("clihelp.py", "prog"), ("clihelp.py", "want"),                                         # the help card is titled by the command it explains, one of our own
               ("setup/addworker.py", "q['title']"), ("setup/addworker.py", "q['why']"), ("setup/addworker.py", "n"), ("setup/addworker.py", "value"), ("setup/addworker.py", "desc"),
               ("setup/cli.py", "'Apply' if chose else 'Skip'"), ("setup/cli.py", "mod.title")}      # the wizard's own streamed lines

    def test_no_card_title_carries_a_name(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        name = lambda f: f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")               # noqa: E731
        bad = []
        for f in sorted(root.rglob("*.py")):
            if f.name in self.SKIP and f.parent.name == "setup" or f.name in {"tui.py"}:
                continue
            rel = str(f.relative_to(root))
            for c in ast.walk(ast.parse(f.read_text())):
                if isinstance(c, ast.Call) and name(c.func) in self.FUNS and c.args:
                    for v in ast.walk(c.args[0]):
                        if isinstance(v, ast.FormattedValue) and not (isinstance(v.value, ast.Call) and name(v.value.func) in ("plural", "len")) and (rel, ast.unparse(v.value)) not in self.ALLOWED:
                            bad.append(f"{rel}:{c.lineno} {ast.unparse(c.args[0])[:80]}")
        self.assertEqual(bad, [], "put the name in a labelled row (facts=[(label, value)] for an error), and keep the title a fixed sentence")


class FoundInTheFleetLog(unittest.TestCase):
    """Seen in a pasted log of the real commands: each of these was wrong on screen and passed every test."""

    def test_an_admin_error_shows_its_labelled_rows_like_every_other_error(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from ct.admin import cli as admin_cli
        from ct.client.repo import ClientError
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            admin_cli.fail_from(ClientError("There is no such worker", "why", facts=[("Name", "nope")]))
        self.assertIn("nope", out.getvalue())                                           # the first version dropped every fact in ct-admin

    def test_a_usage_error_has_a_fixed_title_and_what_was_typed_as_rows(self):
        from ct import clihelp
        ap = clihelp.Parser(prog="ct-run", add_help=False)
        sp = ap.add_subparsers(dest="cmd")
        sp.add_parser("where")
        try:
            ap.parse_args(["bogus-command"])
        except clihelp.UsageError as e:
            title, why, steps, example, facts = clihelp.usage_problem(e, "ct-run")
        self.assertEqual(title, "That is not a command")
        self.assertIn(("Command", "bogus-command"), facts)

    def test_a_word_that_ends_in_a_colon_is_prose_not_part_of_a_command(self):
        self.assertEqual(cards.code_spans("ct-run accepts: setup, submit."), "`ct-run` accepts: setup, submit.")


class SyntaxErrorsCarryTheCommandsSyntax(unittest.TestCase):
    """Every command syntax error says in TRY what the right syntax is and where the full help is (not only 'choose one of')."""

    def test_a_wrong_verb_gets_usage_and_the_help_pointer(self):
        from ct import clihelp
        ap = clihelp.Parser(prog="ct-admin", add_help=False)
        sp = ap.add_subparsers(dest="cmd")
        c = sp.add_parser("class", add_help=False)
        c.add_argument("verb", choices=["set", "show"])
        try:
            ap.parse_args(["class", "bogus"])
        except clihelp.UsageError as e:
            title, why, steps, example, facts = clihelp.usage_problem(e, "ct-admin")
        text = " ".join(steps)
        self.assertIn("Choose one of: set, show", text)
        self.assertIn("Usage: ct-admin class", text)
        self.assertIn("ct-admin help class", text)
        self.assertIn(("Verb", "bogus"), facts)                                          # the argument is named by what it is (verb), not 'option'
        self.assertIsNone(example)                                                          # no copied 'did you mean' with placeholders when nothing is close


if __name__ == "__main__":
    unittest.main()

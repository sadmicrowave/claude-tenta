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


if __name__ == "__main__":
    unittest.main()

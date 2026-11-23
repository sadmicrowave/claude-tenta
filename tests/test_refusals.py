"""The refusal catalog (src/ct/refusals.py): every refusal on the job path is defined once, with what it protects, and the modules that refuse reference it (scope: docs/design/REFUSAL-CATALOG.md).
A refusal is never written as a literal string where it is raised: the guard below fails on one in the modules listed in POLICY_MODULES."""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import refusals  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "ct"
POLICY_MODULES = ("jobs.py", "worker.py", "sched.py", "client/place.py", "client/cap.py", "client/here.py", "client/submit.py", "client/view.py", "client/cmdlog.py")            # each later slice adds its modules here
RAISED = {"Reject", "ValueError", "ClientError", "Refused"}


class Catalog(unittest.TestCase):
    def test_every_entry_has_all_its_fields_and_a_valid_kind_and_audience(self):
        self.assertGreaterEqual(len(refusals.CATALOG), 38)
        for code, e in refusals.CATALOG.items():
            self.assertEqual(code, e.code)
            self.assertRegex(code, r"^[a-z]+(\.[a-z_]+)+$", code)
            self.assertIn(e.kind, refusals.KINDS, code)
            self.assertIn(e.who, refusals.WHO, code)
            for field in ("protects", "title", "why", "depends"):
                self.assertTrue(getattr(e, field).strip(), f"{code}: {field} is empty")
            self.assertTrue(e.title.endswith(".") is False and len(e.title) < 90, code)

    def test_every_code_used_in_the_source_exists_and_every_entry_is_used(self):
        called, text = set(), ""
        for f in SRC.rglob("*.py"):
            if f.name == "refusals.py":
                continue
            src = f.read_text()
            called |= set(re.findall(r'refuse\(\s*"([a-z_.]+)"', src))
            text += src
        self.assertEqual(sorted(called - set(refusals.CATALOG)), [], "a code is used that is not in the catalog")
        unused = [c for c in refusals.CATALOG if f'"{c}"' not in text]                        # a code may be named as a literal and raised by variable (a name check returns its code)
        self.assertEqual(unused, [], "an entry is defined that nothing uses")

    def test_a_refusal_is_both_a_reject_and_a_value_error_so_every_existing_catch_still_works(self):
        from ct import jobs
        r = refusals.refuse("job.params.not_object")
        self.assertIsInstance(r, jobs.Reject)
        self.assertIsInstance(r, ValueError)
        self.assertEqual(r.code, "job.params.not_object")
        self.assertEqual(str(r), "params must be an object")

    def test_facts_fill_the_text_and_a_missing_fact_never_breaks_the_refusal(self):
        self.assertIn("'x'", str(refusals.refuse("files.name.path", name="x")))
        self.assertTrue(str(refusals.refuse("files.name.path")))                 # no fact given: still a message, never a KeyError

    def test_an_unknown_code_is_an_error_in_the_code_not_a_silent_message(self):
        with self.assertRaises(KeyError):
            refusals.refuse("no.such.code")


class Guard(unittest.TestCase):
    def test_no_refusal_is_a_literal_string_in_a_policy_module(self):
        bad = []
        for name in POLICY_MODULES:
            tree = ast.parse((SRC / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    fn = node.exc.func
                    called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    if called in RAISED and node.exc.args and isinstance(node.exc.args[0], (ast.Constant, ast.JoinedStr)):
                        bad.append(f"{name}:{node.lineno}")
        self.assertEqual(bad, [], "define it in ct/refusals.py and raise refusals.refuse(code, ...)")


# What these modules still write as a literal is an OPERATIONAL error or a state ("no result yet", a git command failed): the follow-on task moves them to the same catalog method and empties this list.
OPERATIONAL = {"client/cli.py": {"No result yet", "That job returned no patch", "The patch does not fit this folder", "git could not apply the patch", "No profile result yet"},
               "client/offload.py": {"No profile result yet", "Nothing to propose: the profiling run was inconclusive", "registry.yaml has no `classes:` section"},
               "client/repo.py": {"Could not push to the branch", "A git command failed", "This computer is not set up yet", "Could not clone a repository", "A file did not arrive on the branch"}}


class StillLiteral(unittest.TestCase):
    def test_only_the_listed_operational_errors_are_written_as_literals_in_the_other_client_modules(self):
        bad = []
        for name, allowed in OPERATIONAL.items():
            for node in ast.walk(ast.parse((SRC / name).read_text())):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    fn = node.exc.func
                    called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    if called in RAISED and node.exc.args and isinstance(node.exc.args[0], (ast.Constant, ast.JoinedStr)):
                        title = node.exc.args[0].value if isinstance(node.exc.args[0], ast.Constant) else "<f-string>"
                        if title not in allowed:
                            bad.append(f"{name}:{node.lineno} {title!r}")
        self.assertEqual(bad, [], "a rule about what the person may do belongs in ct/refusals.py (refuse.error); an operational error goes in OPERATIONAL until the follow-on task")


if __name__ == "__main__":
    unittest.main()

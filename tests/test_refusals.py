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
POLICY_MODULES = ("jobs.py", "worker.py", "sched.py", "client/place.py", "client/cap.py", "client/here.py", "client/submit.py", "client/view.py", "client/cmdlog.py",
                  "pkgspec.py", "registry.py", "approvals.py", "rediscover.py", "setup/winsetup.py", "setup/winrelease.py", "setup/winlayout.py", "setup/winmailbox.py", "setup/wintask.py",
                  "setup/winuninstall.py", "admin/cli.py", "admin/actionadd.py", "admin/jobs.py", "admin/proposals.py", "admin/release.py", "admin/selfupdate.py", "admin/ssh.py", "admin/workers.py")            # each later slice adds its modules here
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


OPERATIONAL = {"client/cli.py": set(), "client/offload.py": set(), "client/repo.py": set(), "setup/wintools.py": {"script"}}      # (WinToolsError("script", ...) is the installer's own exception type: its fields are what/detail)       # the client's operational errors are all in the catalog: a new literal one fails the guard below


class OperationalErrors(unittest.TestCase):
    """The errors of the client that are not rules but things that went wrong or are not ready: the same catalog method, kind `error`, the code as the last row."""
    CODES = ("result.not_ready", "profile.result.not_ready", "patch.none", "patch.misfit", "patch.apply_failed", "profile.inconclusive", "registry.shape", "repo.git_failed",
             "repo.not_set_up", "repo.clone_failed", "repo.file_missing", "repo.push_failed")

    def test_every_one_is_in_the_catalog_as_an_error_and_builds_a_client_error_with_its_code_last(self):
        from ct.client import refuse
        self.assertIn("error", refusals.KINDS)
        for code in self.CODES:
            self.assertEqual(refusals.CATALOG[code].kind, "error", code)
            e = refuse.error(code, ("Job", "j-1"))
            self.assertEqual(e.facts[-1], ("Code", code), code)
            self.assertTrue(e.title and e.title == refusals.CATALOG[code].title)

    def test_the_client_modules_hold_no_literal_error_any_more(self):
        self.assertEqual({k: v for k, v in OPERATIONAL.items() if k.startswith("client/") and v}, {})        # every client error is in the catalog


class EveryClientEntry(unittest.TestCase):
    def test_every_entry_decided_on_this_computer_builds_a_client_error_with_its_code_last(self):
        from ct.client import refuse
        for code, e in refusals.CATALOG.items():
            if e.who == "client":
                err = refuse.error(code, ("Thing", "x"))
                self.assertEqual(err.facts[-1], ("Code", code), code)
                self.assertEqual(err.title, e.title, code)


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

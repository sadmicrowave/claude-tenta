"""A test class that subclasses another test class re-runs every test of the parent, which costs time and proves nothing new unless its setUp changes the situation. Put the shared setUp and helpers in a `...Fixture` class with no tests, and derive both from it."""
import ast
import glob
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (module, class): the subclass has its own setUp or settings, so the parent's tests run in a different situation. Reviewed by name; add here only with a reason.
OWN_SETUP = {
    ("test_admin_approvals", "SignedOrders"), ("test_admin_approvals", "CleanCommand"), ("test_admin_approvals", "SignedRegistry"),
    ("test_update", "RegistrySync"), ("test_update", "RootLevelActions"), ("test_worker_signed", "PrivilegedJobs"),
}


def inheriting():
    found = []
    for path in sorted(glob.glob(str(HERE / "test_*.py"))):
        tree = ast.parse(Path(path).read_text())
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        for n in classes.values():
            for b in n.bases:
                parent = classes.get(b.id) if isinstance(b, ast.Name) else None
                if parent and any(isinstance(x, ast.FunctionDef) and x.name.startswith("test_") for x in parent.body):
                    found.append((Path(path).stem, n.name, b.id))
    return found


class NoDuplicateRuns(unittest.TestCase):
    def test_no_test_class_inherits_tests_from_another_without_a_reviewed_reason(self):
        bad = [f"{m}.{c} inherits tests from {p}: move the shared setUp into a {p}Fixture without tests" for m, c, p in inheriting() if (m, c) not in OWN_SETUP]
        self.assertEqual(bad, [])

    def test_every_reviewed_exception_still_exists(self):
        self.assertEqual(sorted(OWN_SETUP - {(m, c) for m, c, _ in inheriting()}), [])


if __name__ == "__main__":
    unittest.main()

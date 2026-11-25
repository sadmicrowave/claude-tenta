"""A test class that subclasses another test class re-runs every test of the parent, which costs time and proves nothing new (a subclass setUp that only adds state does not make them new tests). Put the shared setUp and helpers in a `...Fixture` class with no tests, and derive both from it."""
import ast
import glob
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

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
    def test_no_test_class_inherits_tests_from_another(self):
        bad = [f"{m}.{c} inherits tests from {p}: move the shared setUp into a {p}Fixture without tests" for m, c, p in inheriting()]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

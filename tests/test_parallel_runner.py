"""scripts/tparallel.py: runs each module in its own process, reports only failures, honours tests/serial.txt."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import tparallel  # noqa: E402

OK = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n"
BAD = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('boom-marker')\n"


class Parallel(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def make(self, name, body):
        (self.d / f"{name}.py").write_text(body)

    def run_it(self, names=()):
        from contextlib import redirect_stdout
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            code = tparallel.main(sys.executable, self.d, names, jobs=2)
        return code, out.getvalue()

    def test_all_passing_exits_zero_and_prints_only_the_summary(self):
        self.make("test_x", OK)
        self.make("test_y", OK)
        code, out = self.run_it()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip().splitlines()[0].split(",")[0], "2 modules")
        self.assertNotIn("---", out)

    def test_a_failing_module_is_named_with_its_output_and_the_exit_is_one(self):
        self.make("test_x", OK)
        self.make("test_bad", BAD)
        code, out = self.run_it()
        self.assertEqual(code, 1)
        self.assertIn("--- test_bad", out)
        self.assertIn("boom-marker", out)
        self.assertIn("failed: test_bad", out)

    def test_the_largest_file_starts_first_and_named_modules_limit_the_run(self):
        self.make("test_small", OK)
        self.make("test_big", OK + "# " + "x" * 500 + "\n")
        self.assertEqual(tparallel.modules(self.d), ["test_big", "test_small"])
        self.assertEqual(tparallel.modules(self.d, ["test_small"]), ["test_small"])

    def test_serial_txt_names_are_read_and_comments_ignored(self):
        (self.d / "serial.txt").write_text("# why\ntest_docs  # shares gen-docs output\n\n")
        self.assertEqual(tparallel.serial_only(self.d), {"test_docs"})
        self.assertEqual(tparallel.serial_only(self.d / "none"), set())


class GitTemplate(unittest.TestCase):
    """The shared git fixture is built once and each test gets its own working copy of it."""

    def test_each_test_gets_its_own_repositories_pointing_at_themselves(self):
        import support
        a, b = support.Base("setUp"), support.Base("setUp")
        a.setUp()
        b.setUp()
        self.addCleanup(__import__("shutil").rmtree, a.tmp, True)
        self.addCleanup(__import__("shutil").rmtree, b.tmp, True)
        self.assertNotEqual(a.tmp, b.tmp)
        origin = support.sh("git", "config", "remote.origin.url", cwd=a.tmp / "w-queue")
        self.assertEqual(origin, str(a.tmp / "queue.git"))
        (a.tmp / "requester" / "only-a.txt").write_text("a")
        support.sh("git", "add", "-A", cwd=a.tmp / "requester")
        support.sh("git", "commit", "-q", "-m", "a", cwd=a.tmp / "requester")
        support.sh("git", "push", "-q", "origin", "main", cwd=a.tmp / "requester")
        self.assertEqual(support.sh("git", "log", "--oneline", cwd=b.tmp / "requester").count("\n"), 0)
        support.sh("git", "pull", "-q", cwd=a.tmp / "w-queue")
        self.assertTrue((a.tmp / "w-queue" / "only-a.txt").exists())
        self.assertFalse((b.tmp / "queue.git" / "only-a.txt").exists())

    def test_the_template_is_built_once_per_process(self):
        import support
        self.assertIs(support._git_template(), support._git_template())


if __name__ == "__main__":
    unittest.main()

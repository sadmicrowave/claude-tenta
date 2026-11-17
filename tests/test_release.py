import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("release_script", str(ROOT / "scripts" / "release"))
spec = importlib.util.spec_from_loader("release_script", loader)
rel = importlib.util.module_from_spec(spec)
loader.exec_module(rel)


def g(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout.strip()


class Release(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        g(self.root, "init", "-q", "-b", "main")
        g(self.root, "config", "user.email", "t" + "@" + "example.invalid")
        g(self.root, "config", "user.name", "t")
        (self.root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
        (self.root / "src" / "ct").mkdir(parents=True)
        (self.root / "src" / "ct" / "__init__.py").write_text('__version__ = "0.1.0"\n')
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "first")

    def cut(self, v, **kw):
        return rel.release(self.root, v, skip_tests=True, **kw)

    def test_it_bumps_both_version_files_commits_and_tags_with_the_notes(self):
        (self.root / "f.txt").write_text("x")
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "add a thing")
        tag, prev = self.cut("0.2.0")
        self.assertEqual((tag, prev), ("release-0.2.0", None))
        self.assertIn('version = "0.2.0"', (self.root / "pyproject.toml").read_text())
        self.assertIn('__version__ = "0.2.0"', (self.root / "src" / "ct" / "__init__.py").read_text())
        self.assertEqual(g(self.root, "log", "-1", "--format=%s"), "release 0.2.0")
        notes = g(self.root, "tag", "-l", "--format=%(contents)", "release-0.2.0")
        self.assertIn("- add a thing", notes)
        self.assertEqual(g(self.root, "rev-parse", "release-0.2.0^{commit}"), g(self.root, "rev-parse", "HEAD"))

    def test_the_tag_carries_the_whole_notes_including_lines_that_start_with_a_hash(self):
        (self.root / "f.txt").write_text("x")
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "add a thing")
        notes = "The first release.\n\n## Requirements\n- a worker\n\n## Known limits\n- one worker\n"
        rel.release(str(self.root), "0.3.0", notes=notes, skip_tests=True)
        said = g(self.root, "tag", "-l", "--format=%(contents)", "release-0.3.0")
        self.assertIn(notes.strip(), said)                                             # git's default cleanup would have dropped both headings

    def test_a_tag_without_the_notes_is_removed_and_nothing_is_released(self):
        (self.root / "f.txt").write_text("x")
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "add a thing")
        real = rel.git

        def shortened(root, *args, **kw):
            if args[:1] == ("tag",) and "-a" in args:
                args = (*args[:args.index("-m") + 1], "release 0.3.0")                 # a re-tag with a short message
            return real(root, *args, **kw)
        with mock.patch.object(rel, "git", shortened):
            with self.assertRaises(rel.ReleaseError):
                rel.release(str(self.root), "0.3.0", notes="Real notes.\n", skip_tests=True)
        self.assertEqual(g(self.root, "tag", "-l", "release-0.3.0"), "")

    def test_the_notes_are_only_what_changed_since_the_last_release(self):
        self.cut("0.2.0")
        (self.root / "g.txt").write_text("y")
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "second change")
        self.cut("0.3.0")
        notes = g(self.root, "tag", "-l", "--format=%(contents)", "release-0.3.0")
        self.assertIn("second change", notes)
        self.assertNotIn("first", notes)

    def test_it_refuses_a_dirty_tree_a_wrong_branch_a_repeat_and_an_older_version(self):
        (self.root / "dirty.txt").write_text("d")
        with self.assertRaisesRegex(rel.ReleaseError, "uncommitted"):
            self.cut("0.2.0")
        g(self.root, "add", "-A")
        g(self.root, "commit", "-q", "-m", "clean it")
        self.cut("0.2.0")
        with self.assertRaisesRegex(rel.ReleaseError, "already exists"):
            self.cut("0.2.0")
        with self.assertRaisesRegex(rel.ReleaseError, "not newer"):
            self.cut("0.1.5")
        with self.assertRaisesRegex(rel.ReleaseError, "three numbers"):
            self.cut("v2")
        g(self.root, "checkout", "-q", "-b", "topic")
        with self.assertRaisesRegex(rel.ReleaseError, "from main"):
            self.cut("0.3.0")

    def test_it_never_pushes(self):
        self.cut("0.2.0")
        self.assertEqual(g(self.root, "remote"), "")

    def test_the_newest_tag_is_found_by_number_not_by_name(self):
        for v in ("0.9.0", "0.10.0"):
            g(self.root, "tag", "-a", f"release-{v}", "-m", "m")
        self.assertEqual(rel.previous_tag(self.root), "release-0.10.0")


if __name__ == "__main__":
    unittest.main()

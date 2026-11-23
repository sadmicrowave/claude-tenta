"""The vendored ignore templates are third-party data the privacy check does not scan (security spot-check F8), so any edit to them must fail a test: every file is pinned by hash in SHA256SUMS, written by scripts/refresh-gitignore at one upstream commit."""
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct.client import submit  # noqa: E402


class Pinned(unittest.TestCase):
    def copy(self):
        d = Path(tempfile.mkdtemp()) / "ignore_templates"
        shutil.copytree(submit.TEMPLATES, d)
        return d

    def test_the_shipped_templates_match_their_manifest_exactly(self):
        self.assertEqual(submit.templates_problems(), [])
        self.assertRegex((submit.TEMPLATES / "SOURCE").read_text(), r"commit [0-9a-f]{40}")

    def test_an_edited_file_is_found(self):
        d = self.copy()
        (d / "lang" / "Python.gitignore").write_text("*\n")                                    # a template that would hide everything
        problems = submit.templates_problems(d)
        self.assertTrue(any("Python.gitignore" in p and "changed" in p for p in problems), problems)

    def test_a_file_added_or_removed_is_found(self):
        d = self.copy()
        (d / "lang" / "Extra.gitignore").write_text("x\n")
        (d / "lang" / "Go.gitignore").unlink()
        problems = " | ".join(submit.templates_problems(d))
        self.assertIn("Extra.gitignore", problems)
        self.assertIn("Go.gitignore", problems)

    def test_a_missing_manifest_is_a_problem(self):
        d = self.copy()
        (d / "SHA256SUMS").unlink()
        self.assertTrue(submit.templates_problems(d))

    def test_folders_that_are_not_git_repositories_fall_back_to_the_interim_rule_when_the_templates_were_tampered_with(self):
        from unittest import mock
        d = Path(tempfile.mkdtemp())
        (d / "pyproject.toml").write_text("x")
        (d / "__pycache__").mkdir()
        (d / "__pycache__" / "x.pyc").write_text("x")
        with mock.patch.object(submit, "templates_problems", return_value=["lang/Python.gitignore changed"]):
            self.assertIsNone(submit.template_candidates(d))                                    # not trusted: the interim rule decides


if __name__ == "__main__":
    unittest.main()

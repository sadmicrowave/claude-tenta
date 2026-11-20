"""The Windows release fetch (scope: docs/design/WINDOWS-RELEASE-FETCH.md). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the Python around it is run."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ct.setup import winrelease, wintools  # noqa: E402

URL = "https://github.com/example/project.git"
SHA = "a" * 40


class Script(unittest.TestCase):
    s = winrelease.fetch_script(URL, SHA)

    def test_it_fetches_exactly_that_commit_and_refuses_any_other(self):
        self.assertIn(f"fetch -q --depth 1 '{URL}' '{SHA}'", self.s)
        self.assertIn(f"-ne '{SHA}'", self.s)
        self.assertIn("refused, nothing is kept", self.s)

    def test_a_failed_fetch_leaves_the_release_that_works(self):
        self.assertIn("$new = $dest + '.new'", self.s)
        self.assertLess(self.s.index("$new = $dest"), self.s.index("Move-Item $new $dest"))
        self.assertLess(self.s.index("refused, nothing is kept"), self.s.index("Remove-Item -Recurse -Force $dest }"))   # the old one is removed only after the new one is verified

    def test_the_same_commit_again_changes_nothing(self):
        self.assertIn("if ($have -ne '" + SHA + "')", self.s)

    def test_git_comes_from_the_machine_or_the_tools_folder_and_stderr_does_not_stop_it(self):
        self.assertIn("Get-Command git", self.s)
        self.assertIn("git\\cmd\\git.exe", self.s)
        self.assertIn("$ErrorActionPreference = 'Continue'", self.s)

    def test_the_script_ends_with_a_blank_line(self):
        self.assertTrue(self.s.endswith("}\n\n"))

    def test_only_an_https_source_a_full_commit_and_a_plain_folder_get_into_the_script(self):
        for bad in ("http://x.example/p.git", "https://x.example/p.git'; rm", "git@github.com:a/b.git", ""):
            with self.assertRaises(ValueError):
                winrelease.fetch_script(bad, SHA)
        for bad in ("abc", "A" * 40, SHA + "0", "", "a" * 39 + "'"):
            with self.assertRaises(ValueError):
                winrelease.fetch_script(URL, bad)
        for bad in (r"C:\..\x", "C:\\x'y", "relative"):
            with self.assertRaises(ValueError):
                winrelease.fetch_script(URL, SHA, bad)


class Fetch(unittest.TestCase):
    def test_the_result_line_is_read_and_the_script_goes_on_standard_input(self):
        seen = []

        def runner(argv, text):
            seen.append((argv, text))
            return 0, 'noise\nCT-RESULT {"commit": "' + SHA + '", "path": "C:\\\\r", "changed": false}\n'
        got = winrelease.fetch(runner, URL, SHA)
        self.assertEqual((got["commit"], got["changed"]), (SHA, False))
        self.assertEqual(seen[0][0], wintools.POWERSHELL)
        self.assertIn(SHA, seen[0][1])

    def test_an_error_line_keeps_gits_own_words(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            winrelease.fetch(lambda a, t: (1, 'CT-ERROR {"what": "release", "detail": "the fetch failed: not our ref"}'), URL, SHA)
        self.assertEqual((e.exception.what, "not our ref" in e.exception.detail), ("release", True))


if __name__ == "__main__":
    unittest.main()

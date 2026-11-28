"""The Windows tools installer (scope: docs/design/WINDOWS-TOOLS-INSTALLER.md). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the Python around it is run."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from armada.setup import wintools  # noqa: E402


class Pins(unittest.TestCase):
    def test_the_git_pin_is_complete_and_from_the_official_release(self):
        g = wintools.GIT
        self.assertRegex(g["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(g["url"].startswith("https://github.com/git-for-windows/git/releases/download/"))
        self.assertIn(g["version"], g["url"])
        self.assertTrue(g["url"].endswith(g["name"]))
        self.assertGreater(g["size"], 1_000_000)


class Script(unittest.TestCase):
    def setUp(self):
        self.s = wintools.installer_script()

    def test_it_is_built_for_windows_powershell_5_and_stops_on_any_error(self):
        self.assertIn("$ErrorActionPreference = 'Stop'", self.s)
        self.assertNotIn("&&", self.s)                                                  # PowerShell 5.1 has no &&

    def test_the_download_is_checked_against_the_pin_before_anything_is_unpacked(self):
        sha = wintools.GIT["sha256"]
        self.assertLess(self.s.index("Get-FileHash"), self.s.index("tar.exe"))
        self.assertIn(f"-ne '{sha}'", self.s)
        self.assertIn("nothing was unpacked", self.s)
        self.assertLess(self.s.index("Remove-Item $zip -Force; Fail"), self.s.index("tar.exe"))      # the file is deleted on a mismatch

    def test_an_existing_git_and_an_existing_python_are_used_and_nothing_is_installed_for_them(self):
        self.assertIn("Get-Command git", self.s)
        self.assertIn("if (-not $git)", self.s)                                       # the download only happens when no git was found
        self.assertIn("if (-not $py)", self.s)          # only when no Python 3.11+
        self.assertIn("-notlike '*WindowsApps*'", self.s)                             # the Store's python.exe stub is not a Python

    def test_pyyaml_is_installed_by_hash_from_wheels_only_with_no_dependencies_and_only_when_missing(self):
        for needle in ('import yaml', "--require-hashes", "--only-binary=:all:", "--no-deps", f"PyYAML=={wintools.PYYAML['version']}"):
            self.assertIn(needle, self.s)
        for tag, h in wintools.PYYAML["hashes"].items():
            self.assertRegex(h, r"^[0-9a-f]{64}$")
            self.assertIn(f"--hash=sha256:{h}", self.s)
        self.assertLess(self.s.index("import yaml"), self.s.index("--require-hashes"))
        self.assertIn("pip could not install PyYAML", self.s)                              # with pip's own words

    def test_the_script_ends_with_a_blank_line_or_powershell_on_stdin_never_runs_it(self):
        self.assertTrue(wintools.installer_script(wintools.DEFAULT_TOOLS).endswith("}\n\n"))

    def test_python_comes_from_winget_else_from_python_org_checked_by_hash_and_signature(self):
        self.assertNotIn("winget", self.s.replace("no winget route", ""))
        self.assertNotIn("embed", self.s.lower())
        p = wintools.PYTHON
        self.assertRegex(p["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(p["url"].startswith("https://www.python.org/ftp/python/"))
        self.assertTrue(p["url"].endswith(p["name"]))
        self.assertIn(p["sha256"], self.s)
        i = self.s.index("Get-AuthenticodeSignature")
        self.assertLess(self.s.index("-ne '" + p["sha256"]), i)                       # hash first, then signature
        self.assertLess(i, self.s.index("Start-Process"))                              # both before anything is run
        self.assertIn("Python Software Foundation", self.s)

    def test_a_stale_python_on_path_is_skipped_not_fatal(self):
        self.assertRegex(self.s, r"try \{ \$v = \(& \$c\.Source --version")

    def test_a_python_under_a_persons_profile_is_not_reused_but_the_tools_folder_one_is(self):
        self.assertIn("-notlike '*\\Users\\*'", self.s)                                  # the worker's own account could not read it
        self.assertIn("(Join-Path $tools 'python\\python.exe'), 'python', 'py'", self.s)

    def test_it_prints_one_result_line_and_a_failure_names_what_failed(self):
        self.assertIn("CT-RESULT ", self.s)
        self.assertIn("CT-ERROR ", self.s)
        for what in ("'git'", "'python'", "'script'"):
            self.assertIn("Fail " + what, self.s)

    def test_the_tools_folder_cannot_carry_a_quote_or_a_parent_folder_into_the_script(self):
        for bad in ("C:\\a'b", "C:\\a\\..\\b", "C:\\x; rm", "relative\\path", "C:\\a\"b", "$env:X"):
            with self.assertRaises(ValueError, msg=bad):
                wintools.installer_script(bad)
        self.assertIn(r"C:\Tools (x64)\ct", wintools.installer_script(r"C:\Tools (x64)\ct"))
        self.assertIn("%ProgramData%", wintools.installer_script())


class Result(unittest.TestCase):
    def test_a_result_line_is_read_even_with_noise_around_it(self):
        out = 'banner\r\nCT-RESULT {"git":"git version 2.56.1","python":"Python 3.13.5","changed":true}\r\n'
        r = wintools.parse_result(out)
        self.assertEqual((r["git"], r["changed"]), ("git version 2.56.1", True))

    def test_an_error_line_becomes_an_exception_with_the_real_text(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            wintools.parse_result('CT-ERROR {"what":"git","detail":"the download failed (curl exit 6)"}')
        self.assertEqual((e.exception.what, e.exception.detail), ("git", "the download failed (curl exit 6)"))

    def test_no_result_at_all_keeps_what_powershell_said(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            wintools.parse_result("Get-Command : something went wrong")
        self.assertIn("something went wrong", e.exception.detail)

    def test_install_sends_the_script_on_standard_input_with_no_script_file(self):
        seen = {}

        def runner(argv, text):
            seen["argv"], seen["text"] = argv, text
            return 0, 'CT-RESULT {"git":"g","python":"p","changed":false}'
        r = wintools.install(runner)
        self.assertEqual(seen["argv"][-2:], ["-Command", "-"])
        self.assertIn("Bypass", seen["argv"])
        self.assertEqual(seen["text"], wintools.installer_script())
        self.assertFalse(r["changed"])


class StalePythonRegistration(unittest.TestCase):
    def test_a_python_left_registered_for_the_tools_folder_is_undone_before_the_installer_runs(self):
        s = wintools.installer_script()
        self.assertLess(s.index("OurPythonUninstallers $pydir"), s.index("Start-Process -FilePath $exe"))
        self.assertIn("/uninstall", s)
        self.assertNotIn("foreach ($u ", s)

    def test_the_registration_lookup_only_follows_a_key_that_points_at_the_tools_folder(self):
        self.assertIn("-ieq $pydir", wintools.PY_REGISTRATION)
        self.assertIn("if (-not $ours) { return @() }", wintools.PY_REGISTRATION)


if __name__ == "__main__":
    unittest.main()

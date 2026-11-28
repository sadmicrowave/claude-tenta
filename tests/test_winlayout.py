"""The Windows worker layout (scope: docs/design/WINDOWS-WORKER-LAYOUT.md). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the Python around it is run."""
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from armada.setup import winlayout, wintools  # noqa: E402


class Script(unittest.TestCase):
    with warnings.catch_warnings():
        warnings.simplefilter("error")                          # a stray backslash escape in the PowerShell text is a bug, not a warning
        s = winlayout.ensure_script()
        sp = winlayout.ensure_script(probe=True)

    def test_the_modules_compile_with_no_invalid_escape_in_the_powershell_text(self):
        for name in ("winlayout.py", "wintools.py", "winrelease.py"):
            src = (Path(__file__).resolve().parent.parent / "src" / "armada" / "setup" / name).read_text(encoding="utf-8")
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                compile(src, name, "exec")

    def test_the_root_is_closed_to_everyone_but_system_and_administrators_and_the_account_only_passes_through(self):
        self.assertIn("/inheritance:r", self.s)
        self.assertIn("*S-1-5-18:(OI)(CI)F", self.s)
        self.assertIn("*S-1-5-32-544:(OI)(CI)F", self.s)
        self.assertIn('"${u}:(RX)"', self.s)                                             # this folder only, no inheritance flags

    def test_the_account_reads_tools_release_and_config_and_changes_only_state(self):
        self.assertIn("foreach ($d in @('tools', 'release', 'config')) { & icacls.exe (Join-Path $root $d) /grant:r \"${u}:(OI)(CI)RX\"", self.s)
        self.assertIn("foreach ($d in @('state', 'state\\jobs')) { & icacls.exe (Join-Path $root $d) /grant:r \"${u}:(OI)(CI)M\"", self.s)

    def test_the_account_is_standard_never_an_administrator_and_cannot_log_in(self):
        self.assertIn("New-LocalUser", self.s)
        self.assertIn("Remove-LocalGroupMember -SID 'S-1-5-32-544'", self.s)
        for right in ("SeBatchLogonRight", "SeDenyInteractiveLogonRight", "SeDenyNetworkLogonRight", "SeDenyRemoteInteractiveLogonRight"):
            self.assertIn(right, self.s)

    def test_no_password_is_ever_printed_or_kept(self):
        for text in (self.s, self.sp):
            self.assertNotIn("Write-Output $pw", text)
            self.assertNotIn("password =", text.replace("-Password", ""))
            self.assertNotIn("Out-File", text)
        self.assertIn("RandomPassword", self.s)
        self.assertGreater(self.sp.count("RandomPassword"), self.s.count("RandomPassword"))      # the probe sets a throwaway one and then another one over it

    def test_the_probe_runs_as_the_account_in_a_one_time_task_and_removes_it_even_on_failure(self):
        self.assertNotIn("Register-ScheduledTask", self.s)
        self.assertIn("Register-ScheduledTask", self.sp)
        self.assertGreaterEqual(self.sp.count("Unregister-ScheduledTask"), 2)
        self.assertIn("267011", self.sp)                                                  # 'has not run yet' is waited out, not mistaken for a result

    def test_it_ends_with_a_blank_line_and_prints_one_result_line(self):
        self.assertTrue(self.s.endswith("}\n\n"))
        self.assertEqual(self.s.count("CT-RESULT "), 1)

    def test_only_a_plain_folder_and_a_plain_account_name_get_into_the_script(self):
        for bad in ("a b'c", "9abc", "AB", "x" * 21, "a;b", "armada worker"):
            with self.assertRaises(ValueError):
                winlayout.ensure_script(user=bad)
        for bad in (r"C:\..\x", "C:\\x'y", "relative"):
            with self.assertRaises(ValueError):
                winlayout.check_script(root=bad)


class Problems(unittest.TestCase):
    GOOD = {"user": "armworker", "user_exists": True, "enabled": True, "is_admin": False, "dirs": {"tools": True, "release": True}}
    PROBE = {"reads_tools": True, "reads_release": True, "writes_jobs": True, "writes_release": False, "writes_config": False, "writes_tools": False, "is_admin": False}

    def test_a_right_layout_has_no_problems(self):
        self.assertEqual(winlayout.problems(self.GOOD, self.PROBE), [])

    def test_each_wrong_thing_is_named_in_a_sentence(self):
        self.assertIn("does not exist", winlayout.problems({**self.GOOD, "user_exists": False})[0])
        self.assertIn("is disabled", winlayout.problems({**self.GOOD, "enabled": False})[0])
        self.assertIn("must not be", winlayout.problems({**self.GOOD, "is_admin": True})[0])
        self.assertIn("release is missing", winlayout.problems({**self.GOOD, "dirs": {"tools": True, "release": False}})[0])

    def test_a_probe_that_shows_the_account_can_write_where_it_must_not_is_a_problem(self):
        got = winlayout.problems(self.GOOD, {**self.PROBE, "writes_release": True, "is_admin": True})
        self.assertEqual(len(got), 2)
        self.assertIn("writes release", got[0])

    def test_a_probe_with_a_missing_answer_is_a_problem_too(self):
        self.assertEqual(len(winlayout.problems(self.GOOD, {k: v for k, v in self.PROBE.items() if k != "reads_tools"})), 1)


class Calls(unittest.TestCase):
    def test_ensure_sends_the_script_on_standard_input_and_reads_the_result(self):
        seen = []

        def runner(argv, text):
            seen.append((argv, text))
            return 0, 'CT-RESULT {"changed": true, "user": "armworker", "probe": null}'
        got = winlayout.ensure(runner)
        self.assertEqual(got["changed"], True)
        self.assertEqual(seen[0][0], wintools.POWERSHELL)

    def test_a_failure_keeps_the_real_text(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            winlayout.ensure(lambda a, t: (1, 'CT-ERROR {"what": "rights", "detail": "secedit exited with 1"}'))
        self.assertEqual((e.exception.what, "secedit exited with 1" in e.exception.detail), ("rights", True))


if __name__ == "__main__":
    unittest.main()

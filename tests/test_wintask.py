"""The Windows worker start task (scope: docs/design/WINDOWS-WORKER-TASK.md). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the Python around it is run."""
import re
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ct.setup import winlayout, wintask, wintools  # noqa: E402

SETUP = Path(__file__).resolve().parent.parent / "src" / "ct" / "setup"


class Script(unittest.TestCase):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = wintask.ensure_script()

    def test_it_runs_at_startup_as_the_worker_account_whether_or_not_anyone_is_logged_in(self):
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", self.s)
        self.assertIn('-User "$env:COMPUTERNAME\\$u" -Password $pw', self.s)
        self.assertIn("-RunLevel Limited", self.s)

    def test_it_is_restarted_when_it_dies_has_no_time_limit_and_never_runs_twice(self):
        for needle in ("-RestartCount 999", "-RestartInterval (New-TimeSpan -Minutes 1)", "-ExecutionTimeLimit ([TimeSpan]::Zero)", "-MultipleInstances IgnoreNew"):
            self.assertIn(needle, self.s)

    def test_it_runs_the_tools_python_from_the_release_with_its_own_config_and_logs_to_its_state_folder(self):
        for needle in (r"tools\python\python.exe", r"release\src", r"config\ct.toml", r"state\worker.log", "-m ct.cli run", "-WorkingDirectory"):
            self.assertIn(needle, self.s)

    def test_the_worker_finds_the_tools_git_because_the_task_puts_it_on_the_workers_own_path(self):
        self.assertIn(";%PATH%", self.s)

    def test_a_matching_task_is_left_alone_so_the_password_is_not_touched_again(self):
        i = self.s.index("-like \"*definition $hash*\"")
        self.assertLess(i, self.s.index("Set-LocalUser"))
        self.assertIn("changed = $false", self.s)

    def test_no_password_is_printed_or_kept_and_none_is_changed_after_registering(self):
        self.assertNotIn("Write-Output $pw", self.s)
        self.assertNotIn("Out-File", self.s)
        self.assertEqual(self.s.count("Set-LocalUser"), 1)                      # only before registering: a later change would break the stored logon

    def test_the_layouts_probe_refuses_while_this_task_exists(self):
        self.assertIn("claude-tenta-worker", winlayout.ensure_script(probe=True))
        self.assertIn("would invalidate it", winlayout.ensure_script(probe=True))

    def test_every_script_ends_with_a_blank_line_and_prints_one_result_line_per_path(self):
        for fn in (wintask.ensure_script, wintask.start_script, wintask.stop_script, wintask.status_script, wintask.remove_script):
            text = fn()
            self.assertTrue(text.endswith("}\n\n"), fn.__name__)
            self.assertIn("CT-RESULT ", text)

    def test_the_processes_are_read_with_their_owner_from_the_system(self):
        self.assertIn("GetOwner", wintask._PRE)
        self.assertIn("Win32_Process", wintask._PRE)

    def test_only_plain_arguments_names_and_folders_get_into_the_script(self):
        for bad in ("-m x'y", "-m x & calc", "-m x | y", "-c a>b", "", "a%PATH%", "x\\y", "x" * 201):
            with self.assertRaises(ValueError, msg=bad):
                wintask.ensure_script(args=bad)
        for bad in ("a b", "1x", "x;y"):
            with self.assertRaises(ValueError, msg=bad):
                wintask.ensure_script(task=bad)
        wintask.ensure_script(args='-c "import time; time.sleep(3600)"')


class PowerShellPitfalls(unittest.TestCase):
    def test_no_double_quoted_string_in_the_windows_scripts_puts_a_colon_right_after_a_variable_name(self):
        """PowerShell reads `$u:` in a string as a drive-qualified variable and refuses the whole script (found on the real VM): write ${u}: instead."""
        bad = []
        for f in sorted(SETUP.glob("win*.py")):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r'"[^"\n]*\$(?!env:)[A-Za-z_]\w*:', line):
                    bad.append(f"{f.name}:{n}: {line.strip()[:90]}")
        self.assertEqual(bad, [])


class Problems(unittest.TestCase):
    GOOD = {"exists": True, "user": "ctworker", "triggers": ["MSFT_TaskBootTrigger"], "restart_count": 999, "time_limit": "PT0S",
            "processes": [{"name": "python.exe", "pid": 5, "owner": "BOX\\ctworker"}]}

    def test_a_right_task_has_no_problems(self):
        self.assertEqual(wintask.problems(self.GOOD), [])

    def test_each_wrong_thing_is_named(self):
        self.assertIn("not registered", wintask.problems({"exists": False})[0])
        self.assertIn("not ctworker", wintask.problems({**self.GOOD, "user": "someone"})[0])
        self.assertIn("boot trigger", wintask.problems({**self.GOOD, "triggers": []})[0])
        self.assertIn("restart", wintask.problems({**self.GOOD, "restart_count": 0})[0])
        self.assertIn("after PT72H", wintask.problems({**self.GOOD, "time_limit": "PT72H"})[0])

    def test_a_process_running_as_anyone_else_is_a_problem(self):
        got = wintask.problems({**self.GOOD, "processes": [{"name": "python.exe", "pid": 9, "owner": "BOX\\admin"}]})
        self.assertIn("runs as BOX\\admin", got[0])


class Calls(unittest.TestCase):
    def test_start_sends_the_script_on_standard_input(self):
        seen = []

        def runner(argv, text):
            seen.append((argv, text))
            return 0, 'CT-RESULT {"task": "claude-tenta-worker", "started": true}'
        self.assertTrue(wintask.start(runner)["started"])
        self.assertEqual(seen[0][0], wintools.POWERSHELL)

    def test_a_failure_keeps_the_real_text(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            wintask.ensure(lambda a, t: (1, 'CT-ERROR {"what": "python", "detail": "there is no Python at X"}'))
        self.assertEqual((e.exception.what, "no Python" in e.exception.detail), ("python", True))


if __name__ == "__main__":
    unittest.main()

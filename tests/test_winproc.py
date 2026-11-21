"""The Windows job runner (docs/design/WINDOWS-JOB-RUNNER.md). The tests that start real processes run only on Windows (the Windows CI runner and the real Windows VM); the rest run anywhere.
Deliberately independent of `support`, so the Windows CI job can run this file alone."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ct import plat, runner  # noqa: E402

WIN = os.name == "nt"
PY = sys.executable


class Anywhere(unittest.TestCase):
    def test_the_windows_jail_runs_the_command_as_it_is_says_it_is_ready_and_claims_no_isolation(self):
        from ct import jail
        j = jail.WindowsJobObject()
        self.assertEqual((j.ready(), j.problem()), (True, None))
        self.assertEqual(j.wrap(["a", "b"], network=False, wd="w", home="h"), ["a", "b"])
        self.assertIn("no file or network isolation", j.describe())

    def test_a_windows_configuration_gets_the_windows_jail_and_a_linux_one_does_not(self):
        from unittest import mock
        from ct import config, jail
        clean, problems = config.validate({"identity": {"name": "claude-tenta"}, "server": {"host": "h.example", "admin_user": "me"}})
        cfg = config.Config(clean)
        with mock.patch("os.name", "nt"):
            self.assertEqual(jail.from_config(cfg).name, "windows-job")
        self.assertNotEqual(jail.from_config(cfg).name, "windows-job")


    def test_term_and_kill_are_different_requests_on_every_platform(self):
        self.assertNotEqual(plat.TERM, plat.KILL)

    def test_a_windows_job_sees_a_short_system_path_its_own_temp_and_only_its_own_variables(self):
        env = runner.windows_env(r"C:\jobs\one", {"FOO": "1", "PATH": r"C:\tools"})
        self.assertTrue(env["PATH"].startswith(r"C:\tools;"))
        self.assertEqual((env["FOO"], env["HOME"], env["USERPROFILE"]), ("1", r"C:\jobs\one", r"C:\jobs\one"))
        self.assertTrue(env["TEMP"].endswith("tmp") and env["TEMP"] == env["TMP"])
        self.assertEqual(sorted(k for k in env if k not in ("FOO", "PATH")), sorted(["SystemRoot", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG"]))

    def test_the_class_cannot_replace_the_system_path_only_add_in_front_of_it(self):
        env = runner.windows_env("h", {"PATH": "X"})
        self.assertIn("system32", env["PATH"])

    @unittest.skipIf(WIN, "POSIX only")
    def test_asking_for_a_held_process_changes_nothing_on_posix(self):
        self.assertEqual(plat.new_session(hold=True), {"start_new_session": True})


@unittest.skipUnless(WIN, "needs Windows")
class OnWindows(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.wd = self.home / "work"
        self.wd.mkdir()

    def run_it(self, argv, timeout=20, **kw):
        return runner.run_command(argv, timeout, self.wd, home=self.home, **kw)

    def test_an_ok_job_returns_its_output_exit_code_and_metrics(self):
        status, rc, out, m, note = self.run_it([PY, "-c", "print('hello')"])
        self.assertEqual((status, rc), ("ok", 0), (out, note))
        self.assertIn("hello", out)
        for key in ("wall_s", "cpu_s", "peak_rss_mb", "mem_total_mb"):
            self.assertIn(key, m)
        self.assertGreater(m["mem_total_mb"], 0)

    def test_a_failing_job_reports_its_exit_code(self):
        status, rc, out, m, note = self.run_it([PY, "-c", "import sys; sys.exit(3)"])
        self.assertEqual((status, rc), ("error", 3))

    def test_a_timeout_stops_the_whole_tree_not_only_the_top_process(self):
        flag = self.home / "grandchild.pid"
        code = ("import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                f"open(r'{flag}','w').write(str(c.pid)); time.sleep(120)")
        t0 = time.time()
        status, rc, out, m, note = self.run_it([PY, "-c", code], timeout=3)
        self.assertEqual(status, "timeout", (out, note))
        self.assertLess(time.time() - t0, 40)
        gpid = int(flag.read_text())
        time.sleep(1)
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {gpid}"], capture_output=True, text=True).stdout
        self.assertNotIn(str(gpid), alive.split("PID")[-1] if "No tasks" not in alive else "", "the grandchild is still running")

    def test_a_job_over_its_memory_limit_ends_as_limit(self):
        code = "x = bytearray(400 * 1024 * 1024); import time; time.sleep(60)"
        status, rc, out, m, note = self.run_it([PY, "-c", code], timeout=40, limits={"max_rss_mb": 150})
        self.assertEqual(status, "limit", (out, note))
        self.assertIn("memory", note)

    def test_the_job_sees_only_the_environment_it_was_given(self):
        os.environ["CT_SECRET_FOR_TEST"] = "leak"
        try:
            status, rc, out, m, note = self.run_it([PY, "-c", "import os; print('SECRET' if 'CT_SECRET_FOR_TEST' in os.environ else 'clean', os.environ['USERPROFILE'], os.environ['TEMP'])"])
        finally:
            del os.environ["CT_SECRET_FOR_TEST"]
        self.assertEqual(status, "ok", out)
        self.assertIn("clean", out)
        self.assertIn(str(self.home), out)

    def test_a_job_the_worker_lets_go_of_does_not_keep_running(self):
        p = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"], **plat.new_session(hold=True))
        plat.adopt(p)
        plat.finish(p)                                                       # closing the Job Object ends what is in it
        self.assertIsNotNone(p.wait(timeout=10))

    def test_the_file_lock_is_exclusive_across_processes_and_released_when_the_file_closes(self):
        path = self.home / "lock.txt"
        path.write_text("x")
        with open(path, "r+") as mine:
            self.assertTrue(plat.try_lock(mine))
            code = (f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parents[1] / 'src'}'); from ct import plat; "
                    f"f = open(r'{path}', 'r+'); print(plat.try_lock(f))")
            other = subprocess.run([PY, "-c", code], capture_output=True, text=True).stdout.strip()
            self.assertEqual(other, "False")                                        # held here: refused there
        other = subprocess.run([PY, "-c", code], capture_output=True, text=True).stdout.strip()
        self.assertEqual(other, "True")                                              # closed here: free there

    def test_the_command_line_of_a_running_process_is_readable_and_a_dead_one_is_empty(self):
        p = subprocess.Popen([PY, "-c", "import time; time.sleep(30)  # marker-job-id-xyz"], **plat.new_session())
        try:
            time.sleep(1)
            self.assertIn("marker-job-id-xyz", plat.process_cmdline(p.pid))
        finally:
            p.kill()
            p.wait()
        self.assertEqual(plat.process_cmdline(p.pid), "")
        self.assertEqual(plat.process_cmdline(99999999), "")

    def test_cpu_time_of_a_finished_job_is_counted(self):
        before = plat.child_usage().cpu_s
        self.run_it([PY, "-c", "t=__import__('time').time()\nwhile __import__('time').time()-t<1: pass"])
        self.assertGreater(plat.child_usage().cpu_s - before, 0.5)


if __name__ == "__main__":
    unittest.main()

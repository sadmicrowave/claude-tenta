"""The platform seam (src/ct/plat.py): the worker's runtime reaches the operating system only through it, so a port has one place to write. The modules that are Linux by nature are listed by name and are the
whole list of exceptions."""
import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))              # deliberately not `support`: the Windows CI job runs this file alone
from ct import plat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "src" / "ct"
LINUX_BY_NATURE = {"jail.py", "sandboxtest.py", "canarywatch.py", "helper.py", "privileged.py", "update.py", "firewall.py", "plat.py", "theme.py", "mailshell.py", "clonediag.py", "rediscover.py"}
LINUX_FOLDERS = {"setup", "admin", "client"}              # the install (run as root on a server), and the administrator's computer: their own platforms, their own work
BAD_IMPORT = {"fcntl", "pwd", "grp", "resource", "termios", "tty", "select", "signal"}
BAD_OS = {"killpg", "getloadavg", "statvfs", "setsid", "getpgid", "fork", "getuid", "geteuid", "setuid", "chown", "getpgrp"}


def runtime_modules():
    return [f for f in sorted(ROOT.glob("*.py")) if f.name not in LINUX_BY_NATURE]


class Seam(unittest.TestCase):
    def test_no_module_of_the_workers_runtime_reaches_past_the_seam(self):
        bad = []
        for f in runtime_modules():
            for n in ast.walk(ast.parse(f.read_text())):
                if isinstance(n, ast.Import):
                    bad += [f"{f.name}: import {a.name}" for a in n.names if a.name.split(".")[0] in BAD_IMPORT]
                elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in BAD_IMPORT:
                    bad.append(f"{f.name}: from {n.module}")
                elif isinstance(n, ast.Attribute) and n.attr in BAD_OS and isinstance(n.value, ast.Name) and n.value.id == "os":
                    bad.append(f"{f.name}: os.{n.attr}")
                elif isinstance(n, ast.keyword) and n.arg == "start_new_session":
                    bad.append(f"{f.name}: start_new_session (use plat.new_session())")
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/proc/"):
                    bad.append(f"{f.name}: {n.value}")
        self.assertEqual(bad, [], "call ct.plat instead (see docs/design/WINDOWS-SPECIAL-USE.md)")

    def test_every_exception_is_a_module_that_exists(self):
        for name in LINUX_BY_NATURE:
            self.assertTrue((ROOT / name).exists(), name)
        for folder in LINUX_FOLDERS:
            self.assertTrue((ROOT / folder).is_dir(), folder)


class WindowsSaysSoPlainly(unittest.TestCase):
    """Where the Windows port has not been written, the seam stops with a sentence that names the design, never a quiet wrong answer. Simulated here by saying the platform is Windows."""

    def test_each_unwritten_function_raises_with_the_pointer(self):
        calls = [lambda: plat.new_session(), lambda: plat.kill_group(1), lambda: plat.send_signal(1, plat.TERM), lambda: plat.child_usage(), lambda: plat.loadavgs(), lambda: plat.try_lock(object())]
        with mock.patch.object(plat, "IS_WINDOWS", True):
            for call in calls:
                with self.assertRaises(NotImplementedError) as e:
                    call()
                self.assertIn("WINDOWS-SPECIAL-USE", str(e.exception))
            self.assertIsNone(plat.child_limits(1024))                                          # nothing to limit through a fork hook there: the Job Object will do it

    def test_the_readings_that_can_be_asked_politely_return_nothing_not_an_error(self):
        self.assertIsNone(plat.disk_free_gb("/nonexistent-folder-for-the-test"))
        self.assertEqual(plat.process_cmdline(99999999), "")


@unittest.skipIf(os.name == "nt", "the POSIX implementation")
class PosixBehaviourIsUnchanged(unittest.TestCase):
    def test_a_process_group_is_started_and_stopped_as_a_whole(self):
        import subprocess
        import sys
        import time
        p = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"], **plat.new_session())
        time.sleep(0.5)
        plat.kill_group(p.pid)
        p.wait(timeout=10)
        self.assertNotEqual(p.returncode, 0)
        plat.kill_group(p.pid)                                                     # a group that is gone is not an error
        plat.kill_group(2 ** 22 + 12345)                                           # nor is one that never existed

    def test_the_machine_readings_have_the_shape_the_worker_expects(self):
        self.assertEqual(len(plat.loadavgs()), 3)
        self.assertIsInstance(plat.loadavg(), float)
        self.assertIsNone(plat.disk_free_gb("/nonexistent-folder-for-the-test"))
        self.assertGreater(plat.disk_free_gb("/"), 0)
        self.assertEqual(plat.process_cmdline(99999999), "")
        if Path("/proc/self").exists():                                                  # macOS has no /proc: reading a command line there is the Mac worker's own work
            self.assertIn("python", plat.process_cmdline(__import__("os").getpid()).lower())

    def test_the_lock_is_exclusive_and_released_when_the_file_closes(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "lock"
        a, b = open(path, "w"), open(path, "w")
        self.assertTrue(plat.try_lock(a))
        self.assertFalse(plat.try_lock(b))
        a.close()
        self.assertTrue(plat.try_lock(b))
        b.close()

    def test_a_child_limit_is_a_function_on_posix_and_the_usage_counts_finished_children(self):
        self.assertTrue(callable(plat.child_limits(1024 * 1024)))
        u0 = plat.child_usage()
        import subprocess
        import sys
        subprocess.run([sys.executable, "-c", "sum(range(200000))"], check=True)
        u1 = plat.child_usage()
        self.assertGreaterEqual(u1.cpu_s, u0.cpu_s)
        self.assertGreater(u1.maxrss_kb, 0)


if __name__ == "__main__":
    unittest.main()

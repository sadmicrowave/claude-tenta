import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from armada import runner  # noqa: E402
from armada.util import SAFE_PATH  # noqa: E402

PY = sys.executable
NO_NETNS, NETNS = False, ["--user", "--net"]          # flag lists for the legacy isolate() function
from armada import jail as jail_mod  # noqa: E402


class FakeSensors:
    def __init__(self, temp=None, rss=0.0, mem=(8000, 6000)):
        self._temp, self._rss, self._mem = temp, rss, mem

    def temp(self):
        return self._temp

    def meminfo(self):
        return self._mem

    def group_rss_mb(self, pgid):
        return self._rss


def run(code, **kw):
    wd = Path(tempfile.mkdtemp())
    kw.setdefault("home", str(wd))
    kw.setdefault("sensors", FakeSensors())
    kw.setdefault("jail", jail_mod.NoJail())
    kw.setdefault("require_netns", False)
    kw.setdefault("network", True)
    timeout = kw.pop("timeout", 20)
    return runner.run_command([PY, "-c", code], timeout, wd, **kw), wd


class Isolation(unittest.TestCase):
    def test_network_classes_are_untouched_and_others_are_wrapped(self):
        self.assertEqual(runner.isolate(["x"], True, flags=NO_NETNS), ["x"])
        self.assertEqual(runner.isolate(["x", "y"], False, flags=NETNS), ["unshare", "--user", "--net", "--", "x", "y"])

    def test_it_fails_closed(self):
        with self.assertRaises(runner.Refused) as cm:
            runner.isolate(["x"], False, flags=NO_NETNS)
        self.assertIn("will not run", str(cm.exception))
        self.assertEqual(runner.isolate(["x"], False, flags=NO_NETNS, require=False), ["x"])     # the explicit, unsafe opt-out

    def test_a_refused_command_never_runs(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        (status, rc, out, metrics, note), _ = run(f"open({str(marker)!r}, 'w')", network=False, require_netns=True)
        self.assertEqual((status, rc, out, metrics), ("error", None, "", {}))
        self.assertIn("not started", note)
        self.assertFalse(marker.exists())

    def test_class_network_defaults(self):
        self.assertFalse(runner.class_network({}))
        self.assertTrue(runner.class_network({"network": True}))
        self.assertFalse(runner.class_network({"network": False}))

    def test_a_real_namespace_has_only_loopback_when_this_machine_can_build_one(self):
        flags = runner.netns_flags(refresh=True)
        if not flags:
            self.skipTest("this machine cannot create a network namespace")
        (status, rc, out, *_), _ = run("print(open('/proc/net/dev').read())", network=False, jail=jail_mod.Unshare(flags), require_netns=True)
        names = [l.split(":")[0].strip() for l in out.splitlines()[2:] if ":" in l]
        self.assertEqual((status, names), ("ok", ["lo"]))


class Environment(unittest.TestCase):
    def test_path_is_system_dirs_only(self):
        self.assertEqual(runner.build_env("/h")["PATH"], SAFE_PATH)

    def test_a_class_tool_folder_goes_in_front_and_cannot_replace_the_path(self):
        env = runner.build_env("/h", extra_env={"PATH": "/opt/t/bin", "PYTHONPATH": "/opt/t/pip"})
        self.assertEqual((env["PATH"], env["PYTHONPATH"]), ("/opt/t/bin:" + SAFE_PATH, "/opt/t/pip"))
        self.assertEqual(runner.build_env("/h", extra_env={"HOME": "/evil"})["HOME"], "/evil")        # a class may set its own variables

    def test_a_job_sees_nothing_of_the_workers_environment(self):
        os.environ["ARM_TEST_SECRET"] = "leak"
        try:
            (status, rc, out, *_), _ = run("import os, json; print(json.dumps(dict(os.environ)))", extra_env={"X": "1"})
        finally:
            del os.environ["ARM_TEST_SECRET"]
        env = json.loads(out)
        self.assertNotIn("ARM_TEST_SECRET", env)
        self.assertEqual(env["PATH"], SAFE_PATH)
        self.assertEqual(env["X"], "1")

    def test_core_dumps_are_off(self):
        (_, _, out, *_), _ = run("import resource; print(resource.getrlimit(resource.RLIMIT_CORE))")
        self.assertEqual(out.strip(), "(0, 0)")


class Running(unittest.TestCase):
    def test_ok_error_and_output(self):
        (status, rc, out, m, note), _ = run("import sys; print('hi'); print('warn', file=sys.stderr)")
        self.assertEqual((status, rc, note), ("ok", 0, ""))
        self.assertEqual(sorted(out.split()), ["hi", "warn"])
        (status, rc, *_), _ = run("raise SystemExit(3)")
        self.assertEqual((status, rc), ("error", 3))
        self.assertEqual(run("print(1)")[0][1:2], (0,))

    def test_metrics_are_measured(self):
        (_, _, _, m, _), _ = run("x = bytearray(30 * 1024 * 1024); import time; time.sleep(0.3)", sensors=FakeSensors(temp=55, rss=30))
        for key in ("wall_s", "cpu_s", "peak_rss_mb", "disk_mb", "load_end", "mem_total_mb", "start_temp_c"):
            self.assertIn(key, m)
        self.assertEqual(m["mem_total_mb"], 8000)
        self.assertEqual(m["start_temp_c"], 55)
        self.assertGreaterEqual(m["wall_s"], 0.3)

    def test_a_missing_program_is_an_error_not_a_crash(self):
        status, rc, out, *_ = runner.run_command(["/no/such/program"], 5, tempfile.mkdtemp(), home="/tmp", sensors=FakeSensors(),
                                                  jail=jail_mod.NoJail(), require_netns=False)
        self.assertEqual((status, rc), ("error", None))
        self.assertIn("not found", out)

    def test_a_timeout_stops_the_whole_tree(self):
        d = Path(tempfile.mkdtemp())
        marker = d / "child-survived"
        (d / "child.py").write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n")
        code = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(d / 'child.py')!r}])\ntime.sleep(30)\n"
        t0 = time.time()
        (status, rc, *_), _ = run(code, timeout=1)
        self.assertEqual((status, rc), ("timeout", None))
        self.assertLess(time.time() - t0, 15)
        time.sleep(3.5)
        self.assertFalse(marker.exists())                                      # the grandchild was killed with the group

    def test_memory_heat_and_disk_limits_stop_the_job(self):
        (status, _, _, _, note), _ = run("import time; time.sleep(30)", timeout=30, limits={"max_rss_mb": 100}, sensors=FakeSensors(rss=500))
        self.assertEqual(status, "limit")
        self.assertIn("memory 500 MB went over the 100 MB limit", note)
        (status, _, _, _, note), _ = run("import time; time.sleep(30)", timeout=30, limits={"max_temp_c": 70}, sensors=FakeSensors(temp=90))
        self.assertEqual(status, "limit")
        self.assertIn("went over the 70C limit", note)
        wd = Path(tempfile.mkdtemp())
        code = f"import time; open({str(wd / 'big')!r}, 'wb').write(b'x' * 3_000_000); time.sleep(30)"
        status, _, _, _, note = runner.run_command([PY, "-c", code], 30, wd, home=str(wd), limits={"max_disk_mb": 1}, sensors=FakeSensors(temp=40),
                                                    jail=jail_mod.NoJail(), require_netns=False)
        self.assertEqual(status, "limit")
        self.assertIn("MB limit", note)

    def test_a_stop_signal_gives_status_killed(self):
        old = signal.getsignal(signal.SIGTERM)
        runner.install_term_handler()
        timer = threading.Timer(0.7, lambda: os.kill(os.getpid(), signal.SIGTERM))
        try:
            timer.start()
            (status, rc, *_), _ = run("import time; time.sleep(30)", timeout=30)
        finally:
            timer.cancel()
            signal.signal(signal.SIGTERM, old)
        self.assertEqual((status, rc), ("killed", None))

    def test_the_process_group_pid_is_recorded_in_the_marker(self):
        marker = Path(tempfile.mkdtemp()) / "marker"
        marker.write_text(json.dumps({"job": "j"}))
        run("print(1)", marker=marker)
        self.assertEqual(set(json.loads(marker.read_text())), {"job", "child_pgid"})

    def test_the_timeout_is_capped(self):
        # a job cannot ask for more than MAX_TIMEOUT, a one-day watchdog: the real limits come from the class and, for a profile run, from the person's estimate
        self.assertEqual(runner.MAX_TIMEOUT, 86400)


class Groups(unittest.TestCase):
    def test_run_group_returns_output_and_times_out_with_code_124(self):
        r = runner.run_group([PY, "-c", "print('x')"], 10)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "x"))
        r = runner.run_group([PY, "-c", "import time; time.sleep(30)"], 1)
        self.assertEqual(r.returncode, 124)
        self.assertIn("timed out after 1s", r.stderr)


class Sensors(unittest.TestCase):
    def test_temperature_takes_the_hottest_core_and_ignores_other_sensors(self):
        root = Path(tempfile.mkdtemp())
        for name, temps in (("hwmon0", ("coretemp", [41000, 63000])), ("hwmon1", ("nvme", [99000])), ("hwmon2", ("coretemp", [58000]))):
            d = root / name
            d.mkdir()
            (d / "name").write_text(temps[0] + "\n")
            for i, t in enumerate(temps[1], 1):
                (d / f"temp{i}_input").write_text(f"{t}\n")
        (root / "hwmon3").mkdir()                                              # no name file
        self.assertEqual(runner.LinuxSensors(hwmon=root).temp(), 63.0)
        self.assertIsNone(runner.LinuxSensors(hwmon=root / "missing").temp())

    def test_meminfo_and_a_missing_proc(self):
        proc = Path(tempfile.mkdtemp())
        (proc / "meminfo").write_text("MemTotal:        8192000 kB\nMemFree: 1 kB\nMemAvailable:    4096000 kB\n")
        self.assertEqual(runner.LinuxSensors(proc=proc).meminfo(), (8000, 4000))
        self.assertEqual(runner.LinuxSensors(proc=proc / "missing").meminfo(), (0, None))
        self.assertEqual(runner.LinuxSensors(proc=proc / "missing").group_rss_mb(1), 0.0)

    def test_group_memory_sums_the_processes_of_one_group_or_session(self):
        proc = Path(tempfile.mkdtemp())
        sensors = runner.LinuxSensors(proc=proc)
        sensors.page_mb = 1.0                                                   # one page = 1 MB, to keep the numbers readable
        for pid, pgid, sid, pages in ((100, 100, 100, 10), (101, 100, 100, 5), (200, 200, 200, 99), (300, 1, 100, 2)):
            d = proc / str(pid)
            d.mkdir()
            (d / "stat").write_text(f"{pid} (my proc) S 1 {pgid} {sid} 0\n")
            (d / "statm").write_text(f"1000 {pages} 0\n")
        (proc / "self").mkdir()                                                 # not a pid: ignored
        self.assertEqual(sensors.group_rss_mb(100), 17.0)                      # 100, 101 (group 100) and 300 (session 100)

    def test_tree_size_counts_files_and_ignores_symlinks(self):
        d = Path(tempfile.mkdtemp())
        (d / "sub").mkdir()
        (d / "a").write_bytes(b"x" * 1_048_576)
        (d / "sub" / "b").write_bytes(b"x" * 524_288)
        outside = Path(tempfile.mkdtemp()) / "big"
        outside.write_bytes(b"x" * 5_000_000)
        (d / "link").symlink_to(outside)
        self.assertAlmostEqual(runner.tree_mb(d), 1.5, places=2)
        self.assertEqual(runner.tree_mb(d / "missing"), 0)


class WhoIsLoggedIn(unittest.TestCase):
    """Recorded from the three real workers and from the Ubuntu 26.04 machine of the end-to-end run, where `who` prints nothing at all (no utmp file) and only the login manager knows the sessions."""

    def sensors(self, replies):
        from types import SimpleNamespace

        def run(argv, timeout):
            out = replies.get(tuple(argv))
            return SimpleNamespace(returncode=0 if out is not None else 1, stdout=out or "", stderr="")
        return runner.LinuxSensors(run=run)

    def test_ubuntu_26_04_has_no_utmp_so_only_the_login_manager_sees_the_person(self):
        s = self.sensors({("loginctl", "list-sessions", "--no-legend"): "c1 0 root - 264 manager-early - no -\nc2 1002 guest - 720 user - no -\nc3 1002 guest - 726 manager - no -\n",
                          ("loginctl", "show-session", "c1", "-p", "Name", "-p", "Class"): "Name=root\nClass=manager-early\n",
                          ("loginctl", "show-session", "c2", "-p", "Name", "-p", "Class"): "Name=guest\nClass=user\n",
                          ("loginctl", "show-session", "c3", "-p", "Name", "-p", "Class"): "Name=guest\nClass=manager\n",
                          ("who",): ""})
        self.assertEqual(s.logged_in(), ["guest"])

    def test_the_older_column_layout_and_a_console_session_work_and_the_manager_session_never_counts(self):
        s = self.sensors({("loginctl", "list-sessions", "--no-legend"): "   1 1000 agent seat0 tty1 active yes -\n1108 1000 agent -     -    active no  -\n",
                          ("loginctl", "show-session", "1", "-p", "Name", "-p", "Class"): "Name=agent\nRemote=no\nClass=user\nState=active\n",
                          ("loginctl", "show-session", "1108", "-p", "Name", "-p", "Class"): "Name=agent\nClass=manager\n",
                          ("who",): "agent    tty1         2026-11-18 00:38\n"})
        self.assertEqual(s.logged_in(), ["agent"])                                       # once, though two sources name them

    def test_a_computer_without_loginctl_falls_back_to_who_and_one_that_answers_nothing_is_empty(self):
        self.assertEqual(self.sensors({("who",): "alice    pts/0   2026-11-19 18:44 (192.0.2.7)\nbob      :0      2026-11-19 09:00\n"}).logged_in(), ["alice", "bob"])
        self.assertEqual(self.sensors({}).logged_in(), [])


if __name__ == "__main__":
    unittest.main()

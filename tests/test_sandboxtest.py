import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import config, render, sandboxtest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UNIT = """[Unit]
ProtectSystem=nothing-because-this-is-the-Unit-section

[Service]
User=worker
Type=oneshot
KillMode=process
Environment=CW_STATE=/var/lib/x/state
# ProtectHome=yes is only a comment
ProtectSystem=strict
ReadWritePaths=/var/lib/x /home/worker
RestrictAddressFamilies=AF_UNIX AF_INET
ExecStart=/usr/bin/python3 -m cw.cli step
TimeoutStartSec=10min
"""


def cfg():
    return config.load(None, env={"CW_CONFIG": ""})


class Properties(unittest.TestCase):
    def test_only_sandbox_and_environment_settings_are_carried_over(self):
        self.assertEqual(sandboxtest.unit_properties(UNIT), ["User=worker", "Environment=CW_STATE=/var/lib/x/state", "ProtectSystem=strict",
                                                             "ReadWritePaths=/var/lib/x /home/worker", "RestrictAddressFamilies=AF_UNIX AF_INET"])

    def test_the_real_rendered_units_yield_the_sandbox_that_will_be_tested(self):
        text = render.unit_files(cfg(), ROOT / "templates")["claude-ballast-step.service"]
        props = sandboxtest.unit_properties(text)
        for needed in ("User=cwworker", "ProtectSystem=strict", "Environment=PYTHONPATH=/opt/claude-ballast/lib"):
            self.assertIn(needed, props)
        self.assertFalse([p for p in props if p.startswith(("ExecStart", "Type", "KillMode", "TimeoutStartSec"))])


class Checks(unittest.TestCase):
    def test_loopback_only(self):
        head = "Inter-|   Receive\n face |bytes\n"
        self.assertTrue(sandboxtest.only_loopback(head + "    lo: 0 0 0\n"))
        self.assertFalse(sandboxtest.only_loopback(head + "    lo: 0 0 0\n  eth0: 5 5 5\n"))

    def test_the_plan_covers_what_a_job_needs_and_skips_what_the_machine_lacks(self):
        names = [n for n, *_ in sandboxtest.checks("/home/w", "/var/lib/x/state", "/var/lib/x/queue", helper="/usr/local/sbin/h", have=lambda p: True)]
        for needed in ("the worker starts", "a no-network jail can be built", "state and home folders are writable", "a private /tmp works",
                       "GitHub can be reached over SSH", "sudo works for the thermal helper", "the sandbox is in force (the root file system is read-only)"):
            self.assertIn(needed, names)
        skipped = {n: r for n, a, r in sandboxtest.checks("/h", "/s", "/q", have=lambda p: False) if a is None}
        self.assertEqual(set(skipped), {"GitHub can be reached over SSH", "sudo works for the thermal helper", "temperature sensors can be read"})

    def test_the_helper_check_expects_the_usage_exit_not_success(self):
        plan = {n: (a, e) for n, a, e in sandboxtest.checks("/h", "/s", "/q", helper="/usr/local/sbin/h", have=lambda p: True)}
        argv, ok = plan["sudo works for the thermal helper"]
        self.assertEqual(argv[:2], ["/usr/bin/sudo", "-n"])
        self.assertTrue(ok(2, "usage: helper idle|load"))
        self.assertFalse(ok(1, "sudo: a password is required"))

    def test_the_commands_name_the_package_module_not_a_script_path(self):
        argv = [a for n, a, e in sandboxtest.checks("/h", "/s", "/q", have=lambda p: False) if n == "the worker starts"][0]
        self.assertEqual(argv[1:], ["-m", "cw.cli", "status"])


class Running(unittest.TestCase):
    def plan(self):
        return [("a", ["x"], lambda rc, o: rc == 0), ("b", ["y"], lambda rc, o: rc == 0), ("c", None, "no sensors")]

    def test_pass_fail_and_skip(self):
        res = sandboxtest.run_all(["ProtectSystem=strict"], runner=lambda p, a: (0, "fine") if a == ["x"] else (1, "boom: read-only file system"), plan=self.plan())
        self.assertEqual([(n, s) for n, s, _ in res], [("a", "ok"), ("b", "fail"), ("c", "skip")])
        self.assertIn("read-only file system", res[1][2])

    def test_a_runner_that_cannot_start_is_a_failure(self):
        res = sandboxtest.run_all([], runner=lambda p, a: (_ for _ in ()).throw(OSError("no systemd-run")), plan=[("a", ["x"], lambda rc, o: True)])
        self.assertEqual(res[0][1], "fail")

    def test_test_unit_returns_the_exit_code_and_prints_a_readable_list(self):
        lines = []
        loopback = "Inter-|   Receive\n face |bytes\n    lo: 0 0 0\n"
        runner = lambda props, argv: (0, loopback if argv[0].endswith(("unshare", "bwrap")) else "")   # noqa: E731
        code = sandboxtest.test_unit(UNIT, cfg(), runner=runner, have=lambda p: False, out=lines.append)
        self.assertEqual(code, 0)
        self.assertTrue(any(l.startswith("  OK    the worker starts") for l in lines))
        self.assertTrue(any(l.startswith("  SKIP  GitHub can be reached over SSH") for l in lines))
        self.assertEqual(lines[-1].strip(), "The sandbox lets jobs work.")
        lines.clear()
        code = sandboxtest.test_unit(UNIT, cfg(), runner=lambda p, a: (1, "denied"), have=lambda p: False, out=lines.append)
        self.assertEqual(code, 1)
        self.assertTrue(any(l.startswith("  FAIL") for l in lines))
        self.assertIn("The unit file should not be kept", lines[-1])

    def test_a_unit_without_a_sandbox_has_nothing_to_test(self):
        lines = []
        self.assertEqual(sandboxtest.test_unit("[Service]\nUser=x\n", cfg(), runner=lambda p, a: self.fail("no"), out=lines.append), 0)
        self.assertIn("no sandbox settings", lines[0])

    def test_the_test_unit_carries_the_unit_settings_into_systemd_run(self):
        seen = []
        orig = sandboxtest.subprocess.run
        sandboxtest.subprocess.run = lambda cmd, **kw: (seen.append(cmd), type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1]
        try:
            sandboxtest.run_in_sandbox(["ProtectSystem=strict", "User=worker"], ["/bin/true"])
        finally:
            sandboxtest.subprocess.run = orig
        cmd = seen[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("ProtectSystem=strict", cmd)
        self.assertEqual(cmd[-2:], ["--", "/bin/true"])


if __name__ == "__main__":
    unittest.main()

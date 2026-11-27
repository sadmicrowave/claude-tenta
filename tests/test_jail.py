"""The jails: the exact bubblewrap command line, what is and is not visible inside, and failing closed. The real-machine tests run only where bwrap works."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import support  # noqa: F401  (points HOME at a throwaway folder)

from ct import jail

OK_PROBE = subprocess.CompletedProcess([], 0, "Inter-|   Receive\n face |bytes\n    lo: 0 0 0\n", "")
PLAIN_FILE = os.stat_result((0o100755,) + (0,) * 9)
SETUID_FILE = os.stat_result((0o104755,) + (0,) * 9)


def fake_run(version="bubblewrap 0.11.1", probe=OK_PROBE):
    def run(argv, **kw):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, version + "\n", "")
        return probe
    return run


def bwrap(path="/usr/bin/bwrap", **kw):
    return jail.Bwrap(path=path, hide=kw.pop("hide", ["/etc/ct-test"]), isdir=kw.pop("isdir", lambda p: True), **kw)


def pairs(a, flag):
    return [a[i + 1:i + 3] for i, x in enumerate(a) if x == flag]


class Arguments(unittest.TestCase):
    def build(self, **over):
        kw = dict(network=False, wd="/var/lib/ct/work/j1", home="/home/w", env={"PATH": "/usr/bin:/bin"})
        kw.update(over)
        return bwrap().args(["/bin/echo", "hi"], **kw)

    def test_the_command_runs_last_after_a_double_dash(self):
        a = self.build()
        self.assertEqual(a[-3:], ["--", "/bin/echo", "hi"])
        self.assertEqual(a[0], "/usr/bin/bwrap")

    def test_every_job_gets_the_safety_flags(self):
        a = self.build()
        for flag in ("--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--cap-drop") + 1], "ALL")
        self.assertEqual(a[a.index("--hostname") + 1], "localhost")

    def test_the_network_is_cut_unless_the_class_needs_it(self):
        self.assertIn("--unshare-net", self.build(network=False))
        net = self.build(network=True)
        self.assertNotIn("--unshare-net", net)
        self.assertIn(["/run/systemd/resolve", "/run/systemd/resolve"], pairs(net, "--ro-bind-try"))        # name resolution works
        self.assertNotIn(["/run/systemd/resolve", "/run/systemd/resolve"], pairs(self.build(network=False), "--ro-bind-try"))

    def test_only_system_folders_are_visible_and_read_only(self):
        binds = pairs(self.build(), "--ro-bind-try")
        for d in jail.SYSTEM_DIRS:
            self.assertIn([d, d], binds)
        for hidden in ("/var", "/opt", "/root", "/run", "/home", "/srv", "/mnt"):
            self.assertNotIn([hidden, hidden], binds)
        self.assertEqual(pairs(self.build(), "--bind"), [["/var/lib/ct/work/j1", "/var/lib/ct/work/j1"]])        # the one writable folder

    def test_the_home_folder_and_the_configuration_are_hidden_and_the_machine_is_not_named(self):
        a = self.build()
        tmpfs = [x for i, x in enumerate(a) if i and a[i - 1] == "--tmpfs"]
        self.assertIn("/home/w", tmpfs)
        self.assertIn("/etc/ct-test", tmpfs)
        masked = pairs(a, "--ro-bind-try")
        self.assertIn(["/dev/null", "/etc/hostname"], masked)
        self.assertIn(["/dev/null", "/etc/machine-id"], masked)

    def test_the_configuration_folder_is_overlaid_after_etc_is_bound(self):
        a = self.build()
        self.assertGreater(a.index("/etc/ct-test"), a.index("/etc"))

    def test_a_hidden_folder_that_does_not_exist_is_skipped(self):
        a = bwrap(isdir=lambda p: False).args(["x"], network=False, wd="/w", home="/h")
        self.assertNotIn("/etc/ct-test", a)

    def test_a_hidden_folder_outside_the_system_folders_needs_no_overlay(self):
        a = bwrap(hide=["/srv/elsewhere"]).args(["x"], network=False, wd="/w", home="/h")
        self.assertNotIn("/srv/elsewhere", a)

    def test_nothing_under_the_home_is_shown_to_a_job(self):
        self.assertNotIn(["/home/w/.local", "/home/w/.local"], pairs(self.build(), "--ro-bind-try"))
        self.assertNotIn("/home/w/.ssh", " ".join(self.build()))

    def test_a_throwaway_tools_folder_on_the_search_path_is_bound_read_only(self):
        tools = tempfile.mkdtemp(prefix="ct-tools-")
        a = self.build(env={"PATH": f"{tools}/bin:/usr/bin", "PYTHONPATH": tools, "NODE_PATH": "/relative/ignored"})
        self.assertEqual(pairs(a, "--ro-bind"), [[tools, tools]])

    def test_the_working_folder_and_home_in_the_search_path_are_not_bound_twice(self):
        wd = tempfile.mkdtemp(prefix="ct-wd-")
        self.assertEqual(jail.extra_ro_paths({"PATH": f"{wd}:/home/w/.local/bin:/usr/bin"}, "/home/w", wd), [])

    def test_there_is_no_second_session(self):
        self.assertNotIn("--new-session", self.build())          # the worker already gives each job its own session with no terminal


class Readiness(unittest.TestCase):
    def test_a_good_bubblewrap_is_ready_and_probed_once(self):
        calls = []
        base = fake_run()
        j = bwrap(run=lambda argv, **kw: calls.append(argv) or base(argv, **kw))
        with unittest.mock.patch("os.stat", return_value=PLAIN_FILE):
            self.assertTrue(j.ready())
            self.assertTrue(j.ready())
        self.assertEqual(len([c for c in calls if "--version" not in c]), 1)

    def test_a_setuid_bubblewrap_is_refused(self):
        j = bwrap(run=fake_run())
        with unittest.mock.patch("os.stat", return_value=SETUID_FILE):
            self.assertIn("setuid", j.problem())
            self.assertFalse(j.ready())

    def test_an_old_bubblewrap_is_refused(self):
        j = bwrap(run=fake_run(version="bubblewrap 0.3.1"))
        with unittest.mock.patch("os.stat", return_value=PLAIN_FILE):
            self.assertIn("too old", j.problem())

    def test_a_missing_bubblewrap_says_how_to_install_it(self):
        j = jail.Bwrap(path=None, which=lambda *a, **k: None)
        self.assertIn("apt-get install bubblewrap", j.problem())

    def test_a_jail_that_cannot_be_created_explains_why(self):
        bad = subprocess.CompletedProcess([], 1, "", "bwrap: No permissions to create a new namespace\n")
        j = bwrap(run=fake_run(probe=bad))
        with unittest.mock.patch("os.stat", return_value=PLAIN_FILE):
            self.assertIn("No permissions to create a new namespace", j.problem())

    def test_a_jail_that_still_has_a_network_interface_is_not_ready(self):
        leaky = subprocess.CompletedProcess([], 0, "h\nh\n    lo: 0\n  eth0: 1\n", "")
        j = bwrap(run=fake_run(probe=leaky))
        with unittest.mock.patch("os.stat", return_value=PLAIN_FILE):
            self.assertIn("network interface", j.problem())

    def test_version_parsing(self):
        self.assertEqual(jail.parse_version("bubblewrap 0.11.1"), (0, 11, 1))
        self.assertEqual(jail.parse_version("bubblewrap 0.9"), (0, 9, 0))
        self.assertIsNone(jail.parse_version("nothing"))


class FailingClosed(unittest.TestCase):
    def wrap(self, j, **kw):
        return j.wrap(["x"], network=kw.pop("network", False), wd="/w", home="/h", **kw)

    def test_nothing_runs_when_the_jail_is_not_ready_not_even_a_network_job(self):
        for j in (jail.NoJail(), jail.Bwrap(path=None, which=lambda *a, **k: None)):
            for network in (False, True):
                with self.assertRaises(jail.Refused) as e:
                    self.wrap(j, network=network)
                self.assertIn("no job will run", str(e.exception))

    def test_the_explicit_unsafe_opt_out_runs_it_unwrapped(self):
        self.assertEqual(self.wrap(jail.NoJail(), require=False), ["x"])

    def test_the_older_method_keeps_its_behaviour(self):
        u = jail.Unshare(["--user", "--net"])
        self.assertEqual(u.wrap(["x"], network=False, wd="/w", home="/h"), ["unshare", "--user", "--net", "--", "x"])
        self.assertEqual(u.wrap(["x"], network=True, wd="/w", home="/h"), ["x"])
        with self.assertRaises(jail.Refused):
            jail.Unshare(False).wrap(["x"], network=False, wd="/w", home="/h")

    def test_from_config_picks_the_method_and_hides_the_configuration(self):
        from ct import config
        cfg = config.Config(config.validate({})[0])
        j = jail.from_config(cfg)
        self.assertEqual((j.name, j.hide), ("bwrap", (cfg.paths.config,)))
        cfg2 = config.Config(config.validate({"isolation": {"method": "unshare"}})[0])
        self.assertEqual(jail.from_config(cfg2).name, "unshare")


@unittest.skipUnless(shutil.which("bwrap") and jail.Bwrap().ready(), "bubblewrap does not work on this machine")
class RealJail(unittest.TestCase):
    """Runs real jailed commands. Skipped where bubblewrap or unprivileged namespaces are unavailable (for example macOS)."""

    def run_in_jail(self, script, network=False):
        wd = Path(tempfile.mkdtemp(prefix="ct-real-"))
        home = Path(tempfile.mkdtemp(prefix="ct-home-"))
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_test").write_text("not really a key")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        argv = jail.Bwrap(hide=["/etc/ssh"]).wrap([sys.executable, "-c", script], network=network, wd=wd, home=home, env=env)
        r = subprocess.run(argv, capture_output=True, text=True, cwd=str(wd), env=env, timeout=60)
        return r, wd, home

    def test_only_loopback_is_visible(self):
        r, *_ = self.run_in_jail("print(open('/proc/net/dev').read())")
        names = [line.split(":")[0].strip() for line in r.stdout.splitlines()[2:] if ":" in line]
        self.assertEqual((r.returncode, names), (0, ["lo"]), r.stderr)

    def test_the_home_folder_is_empty_inside_and_untouched_outside(self):
        r, wd, home = self.run_in_jail("import os; print(os.path.exists(os.path.expanduser('~/.ssh')), os.listdir(os.path.expanduser('~')))")
        self.assertEqual(r.stdout.strip(), "False []", r.stderr)
        self.assertTrue((home / ".ssh" / "id_test").exists())

    def test_the_jobs_own_folder_is_writable_and_the_system_is_not(self):
        r, wd, _ = self.run_in_jail("open('made', 'w').write('x')\ntry:\n    open('/usr/denied', 'w')\nexcept OSError as e:\n    print('refused', e.errno)")
        self.assertEqual((wd / "made").read_text(), "x")
        self.assertIn("refused 30", r.stdout)               # EROFS

    def test_no_capabilities_and_no_new_privileges(self):
        r, *_ = self.run_in_jail("print([l.strip() for l in open('/proc/self/status') if l.startswith(('CapEff', 'NoNewPrivs'))])")
        self.assertIn("CapEff:\\t0000000000000000", r.stdout)
        self.assertIn("NoNewPrivs:\\t1", r.stdout)

    def test_a_job_cannot_gain_capabilities_in_a_new_namespace(self):
        script = "import subprocess; r = subprocess.run(['unshare','--user','--net','--map-current-user','true'], capture_output=True); print('rc', r.returncode)"
        r, *_ = self.run_in_jail(script)
        if "rc 0" in r.stdout:
            self.skipTest("this machine leaves user namespaces unrestricted (kernel.apparmor_restrict_unprivileged_userns = 0), so nesting is possible")
        self.assertIn("rc 1", r.stdout)

    def test_the_machine_name_is_not_visible(self):
        r, *_ = self.run_in_jail("import socket\ntry:\n    named = open('/etc/hostname').read().strip() != ''\nexcept OSError:\n    named = False          # masked: reading it is refused\nprint(socket.gethostname(), named)")
        self.assertEqual(r.stdout.strip(), "localhost False", r.stderr)

    def test_the_hidden_configuration_folder_is_empty_inside(self):
        r, *_ = self.run_in_jail("import os; print(os.listdir('/etc/ssh'))")
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)

    def test_other_processes_are_not_visible(self):
        r, *_ = self.run_in_jail("import os; print(len([p for p in os.listdir('/proc') if p.isdigit()]))")
        self.assertLess(int(r.stdout.strip()), 10)

    def test_a_network_class_keeps_the_network(self):
        r, *_ = self.run_in_jail("print(open('/proc/net/dev').read())", network=True)
        names = [line.split(":")[0].strip() for line in r.stdout.splitlines()[2:] if ":" in line]
        self.assertGreater(len(names), 1)


if __name__ == "__main__":
    unittest.main()


class UnitSandboxAndJail(unittest.TestCase):
    def test_the_shared_unit_sandbox_leaves_out_what_breaks_the_jail(self):
        from ct import render
        for prop in ("ProtectHostname", "ProtectKernelLogs", "RestrictNamespaces", "NoNewPrivileges", "ProtectKernelTunables", "PrivateTmp"):
            self.assertNotIn(prop, render.SANDBOX.split("# Sandbox.", 1)[1], prop)        # each was found, or known, to stop bubblewrap from building a jail

    def test_the_default_hardening_modules_include_job_isolation_first(self):
        from ct import config
        self.assertEqual(config.SCHEMA["hardening"]["modules"][0][0], "job-isolation")
        self.assertEqual(config.SCHEMA["isolation"]["method"][0], "bwrap")

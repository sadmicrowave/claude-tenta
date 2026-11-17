"""ct-setup uninstall: removes exactly what the installer put there, previews first, asks once, and leaves the protections alone."""
import contextlib
import io
import subprocess
import tempfile
import unittest

import support  # noqa: F401
from test_setup import FakeHost, cfg_of

from ct import config
from ct.setup import cli as setup_cli
from ct.setup.host import Result
from ct.setup.server import SetupError
from ct.setup.uninstall import Uninstall

from ct import render  # noqa: E402
UNITS = render.UNITS          # the real list: an uninstall that forgets a unit is exactly what this guards against


def installed_host(name="claude-tenta"):
    h = FakeHost()
    for u in UNITS:
        h.put(f"/etc/systemd/system/{name}-{u}", "[Unit]\n")
    for p in (f"/etc/sudoers.d/{name}", f"/etc/sysctl.d/60-{name}-ipv6.conf", f"/etc/sysctl.d/61-{name}-userns.conf", f"/etc/audit/rules.d/60-{name}.rules",
              "/usr/local/bin/ct-worker", "/usr/local/sbin/ct-update"):
        h.put(p, "x\n")
    for d in (f"/opt/{name}", f"/etc/{name}", f"/var/lib/{name}"):
        h.put(d + "/marker", "x")
    return h


class Removal(unittest.TestCase):
    def setUp(self):
        self.said, self.h, self.cfg = [], installed_host(), cfg_of()

    def un(self, **kw):
        kw.setdefault("dry_run", False)
        kw.setdefault("confirm", lambda p: True)
        return Uninstall(self.h, self.cfg, say=self.said.append, **kw)

    def test_a_preview_lists_every_step_and_changes_nothing(self):
        r = self.un(dry_run=True).run_all()
        self.assertEqual(r[-1][1], "planned")
        self.assertEqual([c for c in self.h.calls if c[0] in ("systemctl", "rm", "pkill", "userdel", "augenrules")], [])
        self.assertTrue(self.h.exists("/etc/sudoers.d/claude-tenta"))
        text = "\n".join(self.said)
        for needle in ("systemctl disable --now claude-tenta-fetch.timer", "rm -rf /opt/claude-tenta", "rm -rf /var/lib/claude-tenta", "What stays:"):
            self.assertIn(needle, text)

    def test_applying_removes_the_units_files_commands_and_folders(self):
        self.un().run_all()
        for p in ("/etc/sudoers.d/claude-tenta", "/etc/sysctl.d/60-claude-tenta-ipv6.conf", "/etc/sysctl.d/61-claude-tenta-userns.conf",
                  "/etc/audit/rules.d/60-claude-tenta.rules", "/usr/local/bin/ct-worker", "/usr/local/sbin/ct-update",
                  "/etc/systemd/system/claude-tenta-fetch.timer", "/etc/systemd/system/claude-tenta-update.service"):
            self.assertFalse(self.h.exists(p), p)
        for d in ("/opt/claude-tenta", "/etc/claude-tenta", "/var/lib/claude-tenta"):
            self.assertTrue(self.h.ran(f"rm -rf {d}"), d)
        for later in ("systemctl daemon-reload", "augenrules --load"):
            self.assertTrue(self.h.ran(later), later)

    def test_timers_stop_before_their_files_go_and_daemon_reload_follows(self):
        self.un().run_all()
        calls = [" ".join(c) for c in self.h.calls]
        self.assertLess(calls.index("systemctl disable --now claude-tenta-step.timer"), calls.index("systemctl daemon-reload"))

    def test_keep_data_leaves_the_state_folder(self):
        self.un(keep_data=True).run_all()
        self.assertFalse(self.h.ran("rm -rf /var/lib/claude-tenta"))
        self.assertTrue(self.h.ran("rm -rf /opt/claude-tenta"))
        self.assertTrue(any("(--keep-data)" in line for line in self.said))

    def test_the_worker_account_is_removed_only_when_asked(self):
        self.un().run_all()
        self.assertEqual(self.h.ran("userdel"), [])
        self.assertTrue(any("worker account ctworker" in line for line in self.said))
        self.h.calls.clear()
        self.un(remove_account=True).run_all()
        self.assertTrue(self.h.ran("userdel -r ctworker"))

    def test_the_protections_are_never_touched(self):
        self.un(remove_account=True).run_all()
        flat = " ".join(" ".join(c) for c in self.h.calls)
        for protected in ("ufw", "sshd", "unattended", "apt-get", "sysctl -w", "ssh.conf", "10-claude-tenta.conf"):
            self.assertNotIn(protected, flat)
        self.assertTrue(any("key-only SSH" in line for line in self.said))

    def test_declining_removes_nothing(self):
        r = self.un(confirm=lambda p: False).run_all()
        self.assertEqual(r[-1][1], "declined")
        self.assertEqual(self.h.ran("rm -rf"), [])
        self.assertTrue(self.h.exists("/etc/sudoers.d/claude-tenta"))

    def test_nothing_installed_is_said_plainly(self):
        r = Uninstall(FakeHost(), self.cfg, say=self.said.append, dry_run=False, confirm=lambda p: True).run_all()
        self.assertEqual(r[-1][1], "nothing")
        self.assertTrue(any("nothing to remove" in line for line in self.said))

    def test_only_what_is_there_is_listed(self):
        h = FakeHost()
        h.put("/etc/systemd/system/claude-tenta-fetch.timer", "x")
        h.put("/opt/claude-tenta/x", "x")
        Uninstall(h, self.cfg, say=self.said.append, dry_run=True).run_all()
        text = "\n".join(self.said)
        self.assertIn("claude-tenta-fetch.timer", text)
        self.assertNotIn("claude-tenta-step.timer", text)
        self.assertNotIn("/var/lib/claude-tenta", text)

    def test_a_different_name_and_folders_are_respected(self):
        cfg = cfg_of(identity={"name": "homect"}, paths={"code": "/srv/hc-code", "state_root": "/srv/hc-state", "config": "/srv/hc-conf"})
        h = installed_host("homect")
        for d in ("/srv/hc-code", "/srv/hc-state", "/srv/hc-conf"):
            h.put(d + "/m", "x")
        Uninstall(h, cfg, say=self.said.append, dry_run=False, confirm=lambda p: True).run_all()
        for d in ("/srv/hc-code", "/srv/hc-state", "/srv/hc-conf"):
            self.assertTrue(h.ran(f"rm -rf {d}"), d)
        self.assertFalse(h.exists("/etc/sudoers.d/homect"))
        self.assertFalse(h.ran("rm -rf /opt/claude-tenta"))

    def test_a_removal_that_fails_stops_and_says_which(self):
        self.h.replies["rm -rf /opt/claude-tenta"] = Result(1, "Device or resource busy")
        with self.assertRaises(SetupError) as e:
            self.un().run_all()
        self.assertIn("Device or resource busy", e.exception.why)

    def test_stopping_something_that_is_not_running_is_not_a_failure(self):
        self.h.replies["systemctl stop"] = Result(5, "Unit not loaded")
        self.h.replies["pkill"] = Result(1, "")
        self.assertEqual(self.un().run_all()[-1][1], "removed")


class FakeSsh:
    host, user = "worker.example", "admin"

    def __init__(self, rc=0):
        self.captured, self.interactive_calls, self.sent, self.rc = [], [], [], rc

    def capture(self, remote):
        self.captured.append(remote)
        if "mktemp" in remote:
            return subprocess.CompletedProcess(remote, 0, "/tmp/ct-install.AbC123\n", "")
        return subprocess.CompletedProcess(remote, 0, "STEP LIST\n", "")

    def send(self, remote, data):
        self.sent.append((remote, data))
        return subprocess.CompletedProcess(remote, 0, b"", b"")

    def interactive(self, remote):
        self.interactive_calls.append(remote)
        return self.rc


class FromYourComputer(unittest.TestCase):
    def setUp(self):
        import os
        from pathlib import Path
        self.cfgdir = Path(os.environ["HOME"]) / ".config" / "claude-tenta"
        self.cfgdir.mkdir(parents=True, exist_ok=True)
        (self.cfgdir / "ct.toml").write_text('[server]\nhost = "worker.example"\nadmin_user = "admin"\n[github]\nowner = "someone"\n')
        self.ssh, self.answers = FakeSsh(), []
        self.tmp_src = tempfile.mkdtemp()

    def tearDown(self):
        (self.cfgdir / "ct.toml").unlink()

    def run_cli(self, *args, ssh=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = setup_cli.main(["uninstall", "--source", self.tmp_src, *args], host=FakeHost(), ask=lambda p: self.answers.pop(0), ssh_factory=lambda h, u, p: ssh or self.ssh)
        return code, err.getvalue()

    def test_without_apply_it_previews_on_the_server_with_no_sudo(self):
        code, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.interactive_calls, [])
        preview = [c for c in self.ssh.captured if "uninstall --here" in c][0]
        self.assertNotIn("sudo", preview)
        self.assertNotIn("--apply", preview)
        self.assertIn("PYTHONPATH=/tmp/ct-install.AbC123/src", preview)              # the removal code comes from this checkout, not from the install being removed
        self.assertIn("CT_CONFIG=/etc/claude-tenta/ct.toml", preview)
        self.assertEqual(self.ssh.sent[0][0], "tar -xf - -C /tmp/ct-install.AbC123")
        self.assertIn("rm -rf /tmp/ct-install.AbC123", self.ssh.captured)
        self.assertIn("ct-setup uninstall --apply", err)

    def test_apply_asks_once_then_runs_the_removal_under_sudo_on_a_terminal(self):
        self.answers = ["y"]
        import sys
        from unittest import mock
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            code, err = self.run_cli("--apply", "--keep-data")
        self.assertEqual(code, 0)
        call = self.ssh.interactive_calls[0]
        self.assertTrue(call.startswith("sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/ct-install.AbC123/src CT_CONFIG=/etc/claude-tenta/ct.toml python3 -m ct.setup uninstall --here"))
        self.assertIn("rm -rf /tmp/ct-install.AbC123", self.ssh.captured)
        for piece in ("--keep-data", "--apply", "--yes"):
            self.assertIn(piece, call)
        self.assertIn("ct-setup install", err)

    def test_answering_no_removes_nothing(self):
        self.answers = ["n"]
        import sys
        from unittest import mock
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            code, _ = self.run_cli("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.ssh.interactive_calls, [])

    def test_nobody_to_ask_means_no_removal_unless_yes_is_given(self):
        code, _ = self.run_cli("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.ssh.interactive_calls, [])
        code, _ = self.run_cli("--apply", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.ssh.interactive_calls), 1)

    def test_a_failed_removal_on_the_server_is_reported(self):
        code, err = self.run_cli("--apply", "--yes", ssh=FakeSsh(rc=4))
        self.assertEqual(code, 1)
        self.assertIn("The removal stopped", err)

    def test_a_connection_failure_is_explained_not_a_traceback(self):
        from ct.client.repo import ClientError

        class Down(FakeSsh):
            def capture(self, remote):
                raise ClientError("Could not connect to worker.example", "Permission denied (publickey)", ["Check that SSH key login works from this computer."], example="ssh admin" + "@" + "worker.example true")
        code, err = self.run_cli(ssh=Down())
        self.assertEqual(code, 2)
        self.assertIn("Could not connect to worker.example", err)
        self.assertIn("Permission denied (publickey)", err)
        self.assertNotIn("Traceback", err)

    def test_help_lists_uninstall(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup_cli.main(["help"])
        self.assertIn("uninstall", out.getvalue())


if __name__ == "__main__":
    unittest.main()

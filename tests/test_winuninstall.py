"""Removing a Windows worker (src/armada/setup/winremove.py, winuninstall.py, and the route in cli.py; scope: docs/design/WINDOWS-UNINSTALL.md). The PowerShell cannot run here, so its text is checked for the decisions
that matter; the order, what is written on THIS computer, and the failure messages run through a runner that answers each script by what it contains."""
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

import support  # noqa: F401
from armada import config
from armada.admin import workers as workers_mod
from armada.setup import cli, winremove, winuninstall, wintools
from armada.setup.server import SetupError

ANSWERS = [("Unregister-ScheduledTask", {"task": "armada-worker", "removed": True}), ("sshd -t", {"user": "win-mail", "removed": True, "mailbox_kept": True}),
           ("$keepData", {"python": "uninstalled (exit 0)", "rights": 2, "profile": True, "account": True, "folders": ["tools", "release", "config", "mail"], "kept": []})]


class Script(unittest.TestCase):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = winremove.remove_script()

    def test_python_goes_through_its_own_uninstaller_so_no_registration_is_left_to_fail_a_later_install(self):
        self.assertIn("/uninstall", self.s)
        self.assertLess(self.s.index("/uninstall"), self.s.index("Remove-Item -LiteralPath $item.FullName"))

    def test_python_is_found_through_its_pythoncore_key_and_also_when_its_folder_is_already_gone(self):
        self.assertIn("OurPythonUninstallers $py", self.s)
        self.assertIn(r"HKCU:\SOFTWARE\Python\PythonCore", self.s)
        self.assertLess(self.s.index("OurPythonUninstallers $py"), self.s.index("Test-Path -LiteralPath $py") + 400)
        self.assertNotIn("InstallLocation", self.s)                                          # the bundle's entry has none: matching on it left the registration behind on a real computer

    def test_no_loop_variable_overwrites_the_account_name(self):
        self.assertNotIn("foreach ($u ", self.s)

    def test_it_refuses_while_the_worker_task_exists_before_changing_anything(self):
        self.assertLess(self.s.index("Get-ScheduledTask"), self.s.index("Start-Process"))
        self.assertIn("remove it first", self.s)

    def test_only_the_rights_the_setup_set_are_touched_and_only_entries_that_are_gone(self):
        for right in winremove.RIGHTS:
            self.assertIn(right, self.s)
        self.assertEqual(len(winremove.RIGHTS), 4)
        self.assertIn("Translate([Security.Principal.NTAccount])", self.s)

    def test_keep_data_leaves_the_state_folders_and_the_default_does_not(self):
        self.assertIn("$keepData = $false", self.s)
        self.assertIn("$keepData = $true", winremove.remove_script(keep_data=True))
        self.assertIn("'state', 'state-mail'", self.s)

    def test_nothing_outside_the_workers_folder_or_account_is_named(self):
        for bad in ("OpenSSH-Server", "Remove-WindowsCapability", "Uninstall-WindowsFeature", "Remove-LocalUser -Name $env"):
            self.assertNotIn(bad, self.s)

    def test_a_profile_that_is_still_loaded_is_retried_then_reported_and_does_not_stop_the_removal(self):
        self.assertIn("Start-Sleep -Seconds 3", self.s)
        self.assertIn("profile_left", self.s)
        self.assertLess(self.s.index("Remove-CimInstance"), self.s.index("Remove-LocalUser"))
        self.assertIn('$_.LocalPath -like "*\\$u"', self.s)               # found by its folder, so a second run still finds it after the account is gone

    def test_the_script_ends_with_a_blank_line_and_prints_one_result_line(self):
        self.assertTrue(self.s.endswith("\n\n"))
        self.assertEqual(self.s.count("CT-RESULT"), 1)

    def test_a_bad_folder_or_account_name_is_refused_before_a_script_is_made(self):
        with self.assertRaises(ValueError):
            winremove.remove_script(root=r"C:\x'; calc; '")
        with self.assertRaises(ValueError):
            winremove.remove_script(user="Bad Name")


def fixture(tmp, home=None):
    conf = tmp / "cfg" / "win.toml"
    conf.parent.mkdir(parents=True)
    home = home or tmp / "armada" / "workers" / "win"
    (home / "mailbox").mkdir(parents=True)
    (home / "mailbox" / "key").write_text("k")
    from armada.setup import winfiles
    conf.write_text(winfiles.windows_config("win", "192.0.2.50", "me", client_home=str(home)))
    wfile = tmp / "cfg" / "workers.toml"
    workers_mod.save_list(wfile, {"win": str(conf), "other": str(tmp / "cfg" / "other.toml")})
    return config.load(conf), wfile, home


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg, self.wfile, self.home = fixture(self.tmp)
        self.sent = []

    def runner(self, fail_on=None):
        def run(argv, text):
            if "'CT-RESULT ' + '{}'" in text:
                return 0, "CT-RESULT {}"
            self.sent.append(text)
            for marker, answer in ANSWERS:
                if marker in text:
                    if fail_on == marker:
                        return 1, 'CT-ERROR {"what": "boom", "detail": "the real words"}'
                    return 0, "CT-RESULT " + json.dumps(answer)
            raise AssertionError("no answer for: " + text[:80])
        return run

    def go(self, **kw):
        kw.setdefault("dry_run", False)
        return winuninstall.WindowsUninstall(self.cfg, self.wfile, run=kw.pop("run", None) or self.runner(), **kw)

    def test_the_folder_comes_from_the_workers_own_configuration(self):
        self.assertEqual(winuninstall.windows_root(self.cfg), r"C:\ProgramData\armada")

    def test_a_preview_sends_nothing_and_changes_nothing(self):
        r = self.go(dry_run=True)
        self.assertEqual(r.run_all()[-1][1], "planned")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.home.exists() and Path(self.cfg.source).exists())
        self.assertIn(("Remove account", "armworker"), r.rows())
        self.assertIn(("Remove task", "armada-worker"), r.rows())

    def test_the_steps_run_in_order_task_then_mailbox_then_the_worker(self):
        self.go(confirm=lambda p: True).run_all()
        order = [next(m for m, _ in ANSWERS if m in t) for t in self.sent]
        self.assertEqual(order, [m for m, _ in ANSWERS])

    def test_this_computer_forgets_the_worker_and_only_that_worker(self):
        self.go(confirm=lambda p: True).run_all()
        self.assertEqual(list(workers_mod.load_list(self.wfile)), ["other"])
        self.assertFalse(Path(self.cfg.source).exists())
        self.assertFalse(self.home.exists())

    def test_keep_data_keeps_the_clients_folder_and_tells_the_windows_script_to_keep_state(self):
        self.go(keep_data=True, confirm=lambda p: True).run_all()
        self.assertTrue(self.home.exists())
        self.assertIn("$keepData = $true", self.sent[-1])

    def test_no_answer_means_nothing_is_removed(self):
        r = self.go(confirm=lambda p: False)
        self.assertEqual(r.run_all()[-1][1], "declined")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.home.exists())

    def test_a_folder_that_is_not_this_workers_own_is_never_deleted(self):
        tmp = Path(tempfile.mkdtemp())
        shared = tmp / "shared-home"
        cfg, wfile, _ = fixture(tmp, home=shared)
        (shared / "x").write_text("x")
        winuninstall.WindowsUninstall(cfg, wfile, run=self.runner(), dry_run=False, confirm=lambda p: True).run_all()
        self.assertTrue((shared / "x").exists())

    def test_a_profile_left_in_use_keeps_this_computers_side_so_the_same_command_can_finish(self):
        answers = [(m, dict(a, profile_left="C:\\Users\\armworker") if m == "$keepData" else a) for m, a in ANSWERS]

        def run(argv, text):
            if "'CT-RESULT ' + '{}'" in text:
                return 0, "CT-RESULT {}"
            for m, a in answers:
                if m in text:
                    return 0, "CT-RESULT " + json.dumps(a)
        rep = self.go(run=run, confirm=lambda p: True).run_all()
        self.assertEqual(rep[-1][1], "partial")
        self.assertIn("win", workers_mod.load_list(self.wfile))
        self.assertTrue(Path(self.cfg.source).exists() and self.home.exists())

    def test_a_failing_step_names_the_step_and_keeps_the_real_error_and_nothing_here_is_forgotten(self):
        with self.assertRaises(SetupError) as cm:
            self.go(run=self.runner(fail_on="$keepData"), confirm=lambda p: True).run_all()
        self.assertEqual(cm.exception.title, "A step of the Windows removal failed")
        self.assertIn("the real words", cm.exception.why)
        self.assertIn(("Step", "worker"), cm.exception.facts)
        self.assertIn("other", workers_mod.load_list(self.wfile))
        self.assertIn("win", workers_mod.load_list(self.wfile))

    def test_an_unreachable_computer_is_named_before_anything_is_removed(self):
        def run(argv, text):
            return 1, "ssh: connect to host 192.0.2.50 port 22: Operation timed out"
        with self.assertRaises(SetupError) as cm:
            self.go(run=run, confirm=lambda p: True).run_all()
        self.assertEqual(cm.exception.title, "The Windows computer cannot be reached over SSH")
        self.assertTrue(self.home.exists())


class Route(unittest.TestCase):
    def test_remove_account_is_refused_for_a_windows_worker(self):
        tmp = Path(tempfile.mkdtemp())
        cfg, wfile, home = fixture(tmp)
        a = SimpleNamespace(remove_account=True, yes=True, apply=True, keep_data=False, client=False)
        self.assertEqual(cli.windows_uninstall_command(a, cfg, None, lambda m: None), 2)
        self.assertTrue(home.exists())

    def test_the_uninstall_command_takes_the_windows_route_and_never_the_linux_one(self):
        import os
        tmp = Path(tempfile.mkdtemp())
        cfg, wfile, home = fixture(tmp)
        a = SimpleNamespace(here=False, remove_account=False, yes=False, apply=False, keep_data=False, source=None, client=False)
        def linux_route(*x):
            raise AssertionError("the Linux route was used for a Windows worker")
        old = os.environ.get("ARM_CONFIG")
        os.environ["ARM_CONFIG"] = cfg.source
        try:
            rc = cli.uninstall_command(a, None, None, linux_route, lambda m: None)
        finally:
            os.environ.pop("ARM_CONFIG") if old is None else os.environ.__setitem__("ARM_CONFIG", old)
        self.assertEqual(rc, 0)
        self.assertTrue(home.exists())

    def test_the_windows_check_in_the_route_matches_the_installers_own(self):
        from armada.setup import addworker
        tmp = Path(tempfile.mkdtemp())
        cfg, _, _ = fixture(tmp)
        import tomllib
        self.assertTrue(addworker.is_windows_worker(tomllib.loads(Path(cfg.source).read_text())))
        self.assertEqual(str(cfg.paths.code)[1:3], ":/")


if __name__ == "__main__":
    unittest.main()

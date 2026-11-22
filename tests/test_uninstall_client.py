"""Taking the ct commands off your own computer (src/ct/setup/uninstall_client.py and the --client route in cli.py; scope: docs/design/WINDOWS-UNINSTALL.md), against a throwaway home folder."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import support  # noqa: F401
from ct import config
from ct.admin import workers as workers_mod
from ct.setup import cli
from ct.setup.client import COMMANDS
from ct.setup.uninstall_client import MARKER, ClientUninstall


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.scripts = self.tmp / "venv" / "bin"
        self.scripts.mkdir(parents=True)
        (self.home / ".local" / "bin").mkdir(parents=True)
        for c in COMMANDS:
            (self.scripts / c).write_text("#!/bin/sh\n")
            (self.home / ".local" / "bin" / c).symlink_to(self.scripts / c)
        self.rc = self.home / ".zshrc"
        self.rc.write_text(f"alias a=b\n{MARKER}\nexport PATH=\"{self.home}/.local/bin:$PATH\"\nalias c=d\n")
        os.chmod(self.rc, 0o600)
        self.conf = self.tmp / "cfg" / "ct.toml"
        self.conf.parent.mkdir()
        self.clients = self.home / ".ct"
        (self.clients / "approval").mkdir(parents=True)
        (self.clients / "approval" / "key").write_text("k")
        self.conf.write_text(f'[identity]\nname = "claude-tenta"\n[server]\nhost = "mini.example"\nadmin_user = "me"\n[github]\nowner = "someone"\n[paths]\nclient_home = "{self.clients}"\n')
        self.wconf = self.tmp / "cfg" / "win.toml"
        self.wconf.write_text("x")
        workers_mod.save_list(self.tmp / "cfg" / "workers.toml", {"win": str(self.wconf)})
        self.cfg = config.load(self.conf)

    def go(self, **kw):
        kw.setdefault("dry_run", False)
        return ClientUninstall(self.cfg, home=self.home, scripts_dir=self.scripts, **kw)


class Remove(Home):
    def test_a_preview_changes_nothing(self):
        r = self.go(dry_run=True)
        self.assertEqual(r.run_all()[-1][1], "planned")
        self.assertTrue(all((self.home / ".local" / "bin" / c).is_symlink() for c in COMMANDS))
        self.assertIn(MARKER, self.rc.read_text())
        self.assertTrue(self.conf.exists() and self.clients.exists())
        self.assertEqual([k for k, _ in r.rows()].count("Remove link"), 4)

    def test_it_removes_the_links_the_two_lines_the_configuration_and_the_keys(self):
        self.go(confirm=lambda p: True).run_all()
        self.assertFalse(any((self.home / ".local" / "bin" / c).exists() or (self.home / ".local" / "bin" / c).is_symlink() for c in COMMANDS))
        self.assertEqual(self.rc.read_text(), "alias a=b\nalias c=d\n")
        self.assertEqual(self.rc.stat().st_mode & 0o777, 0o600)
        for p in (self.conf, self.wconf, self.tmp / "cfg" / "workers.toml", self.clients):
            self.assertFalse(p.exists(), p)

    def test_keep_data_keeps_the_configuration_and_the_keys_but_still_removes_the_commands(self):
        self.go(keep_data=True, confirm=lambda p: True).run_all()
        self.assertFalse((self.home / ".local" / "bin" / "ct-run").is_symlink())
        self.assertTrue(self.conf.exists() and self.wconf.exists() and (self.clients / "approval" / "key").exists())

    def test_a_link_or_file_that_is_something_else_is_never_removed(self):
        other = self.tmp / "other-ct-run"
        other.write_text("x")
        link = self.home / ".local" / "bin" / "ct-run"
        link.unlink()
        link.symlink_to(other)
        (self.home / ".local" / "bin" / "ct-help").unlink()
        (self.home / ".local" / "bin" / "ct-help").write_text("my own script")
        self.go(confirm=lambda p: True).run_all()
        self.assertTrue(link.is_symlink() and other.exists())
        self.assertEqual((self.home / ".local" / "bin" / "ct-help").read_text(), "my own script")

    def test_a_startup_file_whose_line_after_the_marker_is_not_ours_is_left_alone(self):
        self.rc.write_text(f"{MARKER}\nexport SOMETHING_ELSE=1\n")
        self.go(confirm=lambda p: True).run_all()
        self.assertIn("SOMETHING_ELSE", self.rc.read_text())

    def test_no_answer_means_nothing_is_removed(self):
        self.assertEqual(self.go(confirm=lambda p: False).run_all()[-1][1], "declined")
        self.assertTrue(self.conf.exists() and (self.home / ".local" / "bin" / "ct-run").is_symlink())

    def test_running_it_twice_is_safe(self):
        self.go(confirm=lambda p: True).run_all()
        self.assertEqual(self.go(confirm=lambda p: True).run_all()[-1][1], "removed")

    def test_the_users_home_folder_is_never_treated_as_the_client_folder(self):
        cfg = config.load(self.conf)
        r = ClientUninstall(cfg, home=self.clients, scripts_dir=self.scripts, dry_run=False, confirm=lambda p: True)
        self.assertNotIn(self.clients, r.data_paths())


class Route(Home):
    def test_the_command_lists_the_workers_still_set_up_and_changes_nothing_without_apply(self):
        old = os.environ.get("CT_CONFIG")
        os.environ["CT_CONFIG"] = str(self.conf)
        try:
            a = SimpleNamespace(worker=None, here=False, remove_account=False, yes=False, apply=False, keep_data=False, client=False)
            rc = cli.client_uninstall_command(a, None, lambda m: None, scripts_dir=self.scripts, home=self.home)
        finally:
            os.environ.pop("CT_CONFIG") if old is None else os.environ.__setitem__("CT_CONFIG", old)
        self.assertEqual(rc, 0)
        self.assertTrue(self.conf.exists())

    def test_client_cannot_be_combined_with_worker(self):
        a = SimpleNamespace(worker="win", here=False, remove_account=False, yes=True, apply=True, keep_data=False)
        self.assertEqual(cli.client_uninstall_command(a, None, lambda m: None), 2)


if __name__ == "__main__":
    unittest.main()

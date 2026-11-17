import os
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct.setup.client import COMMANDS, ClientSetup
from ct.setup.host import Host


class OnPath(unittest.TestCase):
    """The ct-* commands must work from any new terminal without typing where they live."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "me"
        self.home.mkdir()
        self.scripts = self.tmp / "venv" / "bin"
        self.scripts.mkdir(parents=True)
        for c in COMMANDS:
            (self.scripts / c).write_text("#!/bin/sh\n")
            (self.scripts / c).chmod(0o755)
        self.said, self.cards = [], []

    def setup_obj(self, path=None, shell="/bin/zsh", answer=True, dry_run=False, scripts="default", card=True):
        env = {"HOME": str(self.home), "PATH": path if path is not None else "/usr/bin:/bin", "SHELL": shell}
        host = Host(root="/", env=env)
        confirm = (lambda info: self.cards.append(info) or answer) if card else None
        return ClientSetup(host, self.home / ".ct", say=self.said.append, dry_run=dry_run, scripts_dir=self.scripts if scripts == "default" else scripts, confirm_card=confirm)

    def links(self):
        return {c: os.path.realpath(self.home / ".local" / "bin" / c) for c in COMMANDS if os.path.lexists(self.home / ".local" / "bin" / c)}

    def test_the_commands_are_linked_into_the_users_local_bin_and_a_second_run_changes_nothing(self):
        s = self.setup_obj(path=f"{self.home}/.local/bin:/usr/bin")
        s.commands_on_path()
        self.assertEqual(self.links(), {c: os.path.realpath(self.scripts / c) for c in COMMANDS})
        before = dict(self.links())
        again = self.setup_obj(path=f"{self.home}/.local/bin:/usr/bin")
        again.commands_on_path()
        self.assertEqual(self.links(), before)
        self.assertEqual([r for r in again.report if r[0] == "commands on your PATH"][-1][1], "ok")

    def test_a_file_that_is_already_there_is_never_overwritten(self):
        (self.home / ".local" / "bin").mkdir(parents=True)
        mine = self.home / ".local" / "bin" / "ct-help"
        mine.write_text("my own tool\n")
        s = self.setup_obj(path=f"{self.home}/.local/bin:/usr/bin")
        s.commands_on_path()
        self.assertEqual(mine.read_text(), "my own tool\n")
        row = [r for r in s.report if r[0] == "commands on your PATH"][-1]
        self.assertEqual(row[1], "partial")
        self.assertIn("rm ", row[2])
        self.assertIn("ct-run", self.links())                                   # the rest were still linked

    def test_a_folder_already_on_the_path_needs_no_change_to_the_shell(self):
        s = self.setup_obj(path=f"{self.home}/.local/bin:/usr/bin")
        s.commands_on_path()
        self.assertEqual(self.cards, [])
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertTrue(any("new terminal tab" in line for line in self.said))

    def test_with_permission_one_marked_line_is_added_to_the_shell_startup_file_once(self):
        rc = self.home / ".zshrc"
        rc.write_text("alias ll='ls -l'")
        rc.chmod(0o600)
        s = self.setup_obj()
        s.commands_on_path()
        text = rc.read_text()
        self.assertTrue(text.startswith("alias ll='ls -l'\n"))                  # what was there is kept, and given its newline
        self.assertIn("# added by ct-setup: the ct-* commands", text)
        self.assertIn(f'export PATH="{self.home}/.local/bin:$PATH"', text)
        self.assertEqual(rc.stat().st_mode & 0o777, 0o600)                      # the file's own mode is kept
        self.assertEqual(len(self.cards), 1)
        self.assertIn(".zshrc", self.cards[0]["risk"])
        self.setup_obj().commands_on_path()
        self.assertEqual(rc.read_text().count("added by ct-setup"), 1)

    def test_without_permission_the_startup_file_is_untouched_and_the_line_is_shown(self):
        for kw in ({"answer": False}, {"card": False}):
            s = self.setup_obj(**kw)
            s.commands_on_path()
            self.assertFalse((self.home / ".zshrc").exists(), kw)
        self.assertTrue(any(f'export PATH="{self.home}/.local/bin:$PATH"' in line for line in self.said))

    def test_an_unknown_shell_is_told_what_to_add_and_nothing_is_edited(self):
        s = self.setup_obj(shell="/usr/bin/tcsh")
        s.commands_on_path()
        self.assertEqual(self.cards, [])
        self.assertTrue(any("startup file" in line for line in self.said))

    def test_a_preview_changes_nothing(self):
        s = self.setup_obj(dry_run=True)
        s.commands_on_path()
        self.assertEqual(self.links(), {})
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertEqual(self.cards, [])

    def test_commands_that_already_resolve_from_the_path_are_left_alone(self):
        s = self.setup_obj(path=f"{self.scripts}:/usr/bin")
        s.commands_on_path()
        self.assertEqual(self.links(), {})
        self.assertEqual([r for r in s.report if r[0] == "commands on your PATH"][-1][1], "ok")

    def test_a_python_without_the_commands_next_to_it_skips_the_step(self):
        s = self.setup_obj(scripts=None)
        s.commands_on_path()
        self.assertEqual([r for r in s.report if r[0] == "commands on your PATH"][-1][1], "skipped")


class ApprovalKey(OnPath):
    """The key that signs approvals on this computer."""

    def make(self, **kw):
        s = self.setup_obj(**kw)
        s.home = self.home / ".ct"                                   # where the client keeps its files
        return s

    def test_a_key_is_made_once_with_no_passphrase_when_nobody_is_there_to_ask(self):
        s = self.make()
        s.approval_key()
        key = self.home / ".ct" / "approval" / "key"
        self.assertTrue(key.exists() and key.with_name("key.pub").exists())
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / ".ct" / "approval").stat().st_mode & 0o777, 0o700)
        first = key.read_bytes()
        again = self.make()
        again.approval_key()
        self.assertEqual(key.read_bytes(), first)                    # an existing key is never replaced
        self.assertEqual([r for r in again.report if r[0] == "approval key"][-1][1], "ok")

    def test_on_a_terminal_ssh_keygen_is_run_on_it_so_a_passphrase_can_be_typed(self):
        calls = []

        def fake(argv):
            calls.append(argv)
            Path(argv[argv.index("-f") + 1]).write_text("k")
            Path(argv[argv.index("-f") + 1] + ".pub").write_text("ssh-ed25519 AAAA ct-approval")
            return 0, ""
        s = self.make()
        s.ask, s.interactive = (lambda q: ""), fake
        s.approval_key()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-N", calls[0])                             # no passphrase is forced: ssh-keygen asks
        self.assertTrue(any("passphrase" in line for line in self.said))

    def test_a_preview_makes_nothing_and_a_failure_is_said_not_fatal(self):
        s = self.make(dry_run=True)
        s.approval_key()
        self.assertFalse((self.home / ".ct" / "approval").exists())
        s = self.make()
        s.ask, s.interactive = (lambda q: ""), (lambda argv: (1, ""))
        s.approval_key()
        self.assertEqual([r for r in s.report if r[0] == "approval key"][-1][1], "failed")


if __name__ == "__main__":
    unittest.main()

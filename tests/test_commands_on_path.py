import os
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from cw.setup.client import COMMANDS, ClientSetup
from cw.setup.host import Host


class OnPath(unittest.TestCase):
    """The cw-* commands must work from any new terminal without typing where they live."""

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
        return ClientSetup(host, self.home / ".cw", say=self.said.append, dry_run=dry_run, scripts_dir=self.scripts if scripts == "default" else scripts, confirm_card=confirm)

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
        mine = self.home / ".local" / "bin" / "cw-help"
        mine.write_text("my own tool\n")
        s = self.setup_obj(path=f"{self.home}/.local/bin:/usr/bin")
        s.commands_on_path()
        self.assertEqual(mine.read_text(), "my own tool\n")
        row = [r for r in s.report if r[0] == "commands on your PATH"][-1]
        self.assertEqual(row[1], "partial")
        self.assertIn("rm ", row[2])
        self.assertIn("cw-run", self.links())                                   # the rest were still linked

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
        self.assertIn("# added by cw-setup: the cw-* commands", text)
        self.assertIn(f'export PATH="{self.home}/.local/bin:$PATH"', text)
        self.assertEqual(rc.stat().st_mode & 0o777, 0o600)                      # the file's own mode is kept
        self.assertEqual(len(self.cards), 1)
        self.assertIn(".zshrc", self.cards[0]["risk"])
        self.setup_obj().commands_on_path()
        self.assertEqual(rc.read_text().count("added by cw-setup"), 1)

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


if __name__ == "__main__":
    unittest.main()

"""The mailbox account can run git on the worker's two repositories and nothing else: results are read-only, and nothing a client sends can reach a shell."""
import os
import unittest
from unittest import mock

import support  # noqa: F401

from ct import mailshell

Q, R = "/var/lib/x-mail/queue.git", "/var/lib/x-mail/results.git"


class Decide(unittest.TestCase):
    def d(self, command):
        return mailshell.decide(command, Q, R)

    def test_git_reads_both_repositories_and_writes_only_the_queue(self):
        self.assertEqual(self.d(f"git-upload-pack '{Q}'"), ["git-upload-pack", Q])
        self.assertEqual(self.d(f"git-upload-pack '{R}'"), ["git-upload-pack", R])
        self.assertEqual(self.d(f"git-receive-pack '{Q}'"), ["git-receive-pack", Q])

    def test_the_results_repository_cannot_be_written(self):
        self.assertIsNone(self.d(f"git-receive-pack '{R}'"))

    def test_nothing_else_runs(self):
        for bad in ("", None, "ls", "sh", "bash -c id", f"git-upload-pack '{Q}'; id", f"git-upload-pack '{Q}' --foo", f"git-upload-pack '{Q}' '{R}'", f"git-upload-archive '{Q}'",
                    "git-upload-pack '/etc'", f"git-upload-pack '{Q}/../results.git/../../../etc'", "git-upload-pack '/var/lib/x-mail/queue.git.evil'",
                    f"git-upload-pack '{Q}' $(id)", f"git-upload-pack `id`", "git-upload-pack 'unterminated"):
            self.assertIsNone(self.d(bad), bad)

    def test_a_path_that_normalises_to_an_allowed_repository_is_that_repository(self):
        self.assertEqual(self.d("git-upload-pack '/var/lib/x-mail/./queue.git'"), ["git-upload-pack", Q])


class Main(unittest.TestCase):
    def test_a_refused_command_exits_with_a_message_and_runs_nothing(self):
        cfg = mock.Mock()
        cfg.path.return_value = "/var/lib/x-mail"
        with mock.patch("ct.config.load", return_value=cfg), mock.patch.object(os, "execv") as ex, mock.patch("sys.stderr"):
            self.assertEqual(mailshell.main(environ={"SSH_ORIGINAL_COMMAND": "id"}), 1)
            ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()

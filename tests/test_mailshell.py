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


class TheInstalledWrapper(unittest.TestCase):
    def test_the_command_the_client_sent_survives_the_wrapper_s_clean_environment(self):
        """Found on a real machine: `env -i` dropped SSH_ORIGINAL_COMMAND, so every clone was refused. Run the real wrapper and see the command arrive."""
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from ct import config, render
        from test_setup import cfg_of
        cfg = cfg_of(alerts={"kind": "none"}, transport={"kind": "direct"})
        v = render.values_for(cfg)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "ct.toml").write_text('[server]\nhost = "h"\nadmin_user = "me"\n[github]\nowner = "o"\n[transport]\nkind = "direct"\n')
        text = render.mail_shell(cfg).replace(v["lib"], str(Path(mailshell.__file__).parents[1])).replace(v["config"], str(tmp)).replace("/usr/bin/python3", sys.executable)
        script = tmp / "shell"
        script.write_text(text)
        mailbox = config.load(str(tmp / "ct.toml")).path("mailbox")
        run = lambda command: subprocess.run(["sh", str(script)], env={**({"SSH_ORIGINAL_COMMAND": command} if command else {}), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertNotIn("can only fetch", run(f"git-upload-pack '{mailbox}/queue.git'").stderr)        # an allowed command reaches the shell (git then fails: no such repository here)
        self.assertIn("can only fetch", run("id").stderr)
        r = run(None)
        self.assertIn("can only fetch", r.stderr)        # a login with no command at all is refused, not an error about an unset variable
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()

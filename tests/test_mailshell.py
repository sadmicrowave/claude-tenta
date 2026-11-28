"""The mailbox account can run git on the worker's two repositories and nothing else: results are read-only, and nothing a client sends can reach a shell."""
import os
import unittest
from unittest import mock

import support  # noqa: F401

from armada import mailshell

Q, R = "/var/lib/x-mail/queue.git", "/var/lib/x-mail/results.git"


WQ, WR = "C:/ProgramData/armada/state-mail/queue.git", "C:/ProgramData/armada/state-mail/results.git"


class DecideOnWindows(unittest.TestCase):
    def d(self, command):
        return mailshell.decide(command, WQ, WR)

    def test_the_url_path_with_a_slash_before_the_drive_letter_is_the_configured_folder(self):
        self.assertEqual(self.d("git-receive-pack '/C:/ProgramData/armada/state-mail/queue.git'"), ["git-receive-pack", WQ])
        self.assertEqual(self.d("git-upload-pack '/C:/ProgramData/armada/state-mail/results.git'"), ["git-upload-pack", WR])

    def test_case_and_slash_style_do_not_matter_as_on_the_file_system_but_git_gets_the_configured_path(self):
        self.assertEqual(self.d("git-upload-pack 'c:\\programdata\\Armada\\state-mail\\QUEUE.git'"), ["git-upload-pack", WQ])

    def test_the_results_are_still_read_only_and_nothing_else_runs(self):
        self.assertIsNone(self.d("git-receive-pack '/C:/ProgramData/armada/state-mail/results.git'"))
        self.assertIsNone(self.d("cmd /c dir"))
        self.assertIsNone(self.d("git-upload-pack '/C:/Windows/System32'"))

    def test_a_path_that_climbs_out_of_the_mailbox_is_refused(self):
        self.assertIsNone(self.d("git-receive-pack '/C:/ProgramData/armada/state-mail/../state/queue.git'"))
        self.assertIsNone(self.d("git-upload-pack '/D:/ProgramData/armada/state-mail/queue.git'"))


class MailboxFromTheLauncher(unittest.TestCase):
    def test_a_launcher_that_names_the_mailbox_needs_no_configuration_file(self):
        with mock.patch("sys.stderr"):
            rc = mailshell.main(environ={"ARM_MAILBOX": "C:/ProgramData/x/state-mail", "SSH_ORIGINAL_COMMAND": "cmd /c dir"})
        self.assertEqual(rc, 1)                                              # refused as any other command, without ever loading armada.toml (which would have raised)


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
        with mock.patch("armada.config.load", return_value=cfg), mock.patch.object(os, "execv") as ex, mock.patch("sys.stderr"):
            self.assertEqual(mailshell.main(environ={"SSH_ORIGINAL_COMMAND": "id"}), 1)
            ex.assert_not_called()


class GitIsToldTheRepositoryIsMeant(unittest.TestCase):
    def test_git_is_given_exactly_the_repository_it_serves_as_trusted_for_this_one_command(self):
        """Found on a real machine: the results repository belongs to the worker, so git refused to serve it to the mailbox account ('dubious ownership')."""
        cfg = mock.Mock()
        cfg.path.return_value = "/var/lib/x-mail"
        with mock.patch("armada.config.load", return_value=cfg), mock.patch.object(os, "execve") as ex, mock.patch.object(os, "execv", side_effect=AssertionError("the old exec, with the whole environment")):
            mailshell.main(environ={"SSH_ORIGINAL_COMMAND": f"git-upload-pack '{R}'"})
        (program, argv, env), _ = ex.call_args
        self.assertEqual(argv, ["git-upload-pack", R])
        self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("1", "safe.directory", R))
        self.assertEqual(sorted(env), ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "PATH"])        # nothing else of the environment goes on

    def test_a_real_git_serves_a_repository_owned_by_another_account_when_told_so(self):
        """The behaviour itself, with the real git: refused without the setting, served with it (skipped where git has no ownership check, or the test runs as the owner's own account)."""
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "--bare", d + "/r.git"], check=True)
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": d + "/r.git"}
        r = subprocess.run(["git", "upload-pack", "--advertise-refs", d + "/r.git"], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)


class TheInstalledWrapper(unittest.TestCase):
    def test_the_command_the_client_sent_survives_the_wrapper_s_clean_environment(self):
        """Found on a real machine: `env -i` dropped SSH_ORIGINAL_COMMAND, so every clone was refused. Run the real wrapper and see the command arrive."""
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        from armada import config, render
        from test_setup import cfg_of
        cfg = cfg_of(alerts={"kind": "none"}, transport={"kind": "direct"})
        v = render.values_for(cfg)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "armada.toml").write_text('[server]\nhost = "h"\nadmin_user = "me"\n[github]\nowner = "o"\n[transport]\nkind = "direct"\n')
        text = render.mail_shell(cfg).replace(v["lib"], str(Path(mailshell.__file__).parents[1])).replace(v["config"], str(tmp)).replace("/usr/bin/python3", sys.executable)
        script = tmp / "shell"
        script.write_text(text)
        mailbox = config.load(str(tmp / "armada.toml")).path("mailbox")
        run = lambda command: subprocess.run(["sh", str(script)], env={**({"SSH_ORIGINAL_COMMAND": command} if command else {}), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertNotIn("can only fetch", run(f"git-upload-pack '{mailbox}/queue.git'").stderr)        # an allowed command reaches the shell (git then fails: no such repository here)
        self.assertIn("can only fetch", run("id").stderr)
        r = run(None)
        self.assertIn("can only fetch", r.stderr)        # a login with no command at all is refused, not an error about an unset variable
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()

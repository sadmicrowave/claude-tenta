"""The transport decides where the two repositories live; GitHub's answers are exactly what the code used to build by hand."""
import unittest
from pathlib import Path

import support  # noqa: F401

from ct import config, transport


def cfg_with(**over):
    nested = {"server": {"admin_user": "me", "host": "worker.example"}, "github": {"owner": "someone"}, **over}
    clean, problems = config.validate(nested)
    assert not problems, problems
    return config.Config(clean)


class Words(unittest.TestCase):
    def test_every_transport_says_what_you_gain_and_what_you_give_up(self):
        from ct.setup import questions
        for kind, t in transport.TRANSPORTS.items():
            self.assertGreater(len(t.gain), 30, kind)
            self.assertGreater(len(t.cost), 30, kind)
            self.assertLess(len(t.gain) + len(t.cost), 360, kind)                      # short: a line of gain and a line of cost
        offered = dict(questions.CHOICES["transport.kind"])
        self.assertEqual(set(offered), set(transport.TRANSPORTS))                        # the wizard offers every transport, with the same words
        for kind, text in offered.items():
            self.assertIn(transport.TRANSPORTS[kind].gain, text)
            self.assertIn(transport.TRANSPORTS[kind].cost, text)

    def test_the_github_questions_are_not_asked_for_a_direct_install(self):
        from ct.setup import questions
        by = questions.BY_KEY
        self.assertTrue(by["github.owner"]["when"]({"transport.kind": "github"}))
        self.assertFalse(by["github.owner"]["when"]({"transport.kind": "direct"}))
        self.assertTrue(by["github.owner"]["when"]({}))                                  # the default is GitHub


class Direct(unittest.TestCase):
    def test_the_client_uses_its_own_mailbox_key_and_never_asks_a_question(self):
        cfg = cfg_with(transport={"kind": "direct"})
        cmd = transport.for_config(cfg).client_ssh_command(cfg)
        self.assertIn("mailbox/key", cmd)
        for option in ("IdentitiesOnly=yes", "BatchMode=yes"):
            self.assertIn(option, cmd)
        self.assertIsNone(transport.for_config(cfg_with()).client_ssh_command(cfg_with()))   # GitHub uses the person's own ssh

    def test_the_repositories_are_reached_with_that_ssh_command(self):
        from unittest import mock
        from ct.client.repo import ClientRepos
        cfg = cfg_with(transport={"kind": "direct"})
        repos = ClientRepos.from_config(cfg)
        self.assertEqual((repos.where, repos.queue_url), ("the worker", cfg.queue_url))
        with mock.patch("ct.client.repo.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "", ""
            repos.git("/tmp/x", "fetch")
        self.assertIn("mailbox/key", run.call_args.kwargs["env"]["GIT_SSH_COMMAND"])
        github = ClientRepos.from_config(cfg_with())
        with mock.patch("ct.client.repo.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "", ""
            github.git("/tmp/x", "fetch")
        self.assertIsNone(run.call_args.kwargs["env"])


class DirectAddresses(unittest.TestCase):
    def direct(self, **server):
        return cfg_with(transport={"kind": "direct"}, server={"admin_user": "me", "host": "worker.example", **server})

    def test_the_mailbox_is_on_the_worker_and_your_computer_reaches_it_as_the_mail_account(self):
        cfg = self.direct()
        t = transport.for_config(cfg)
        self.assertEqual((t.kind, t.uses_deploy_keys, t.mail_user(cfg)), ("direct", False, "armada-mail"))
        self.assertEqual(cfg.queue_url, "ssh://armada-mail" + "@" + "worker.example/var/lib/armada-mail/queue.git")
        self.assertEqual(cfg.results_url, "ssh://armada-mail" + "@" + "worker.example/var/lib/armada-mail/results.git")

    def test_a_windows_worker_has_a_slash_before_the_drive_letter_in_its_address(self):
        cfg = cfg_with(transport={"kind": "direct"}, server={"admin_user": "me", "host": "worker.example"},
                       paths={"code": "C:/ProgramData/armada/release", "state_root": "C:/ProgramData/armada/state", "config": "C:/ProgramData/armada/config", "worker_home": "C:/ProgramData/armada/state"})
        self.assertEqual(cfg.queue_url, "ssh://armada-mail" + "@" + "worker.example/C:/ProgramData/armada/state-mail/queue.git")
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "C:/ProgramData/armada/state-mail/queue.git")      # the worker itself uses its own plain path

    def test_a_non_standard_ssh_port_is_in_the_address(self):
        self.assertIn("worker.example:2222/", self.direct(ssh_port=2222).queue_url)

    def test_the_worker_uses_plain_paths_and_needs_no_ssh_configuration_at_all(self):
        cfg = self.direct()
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "/var/lib/armada-mail/queue.git")
        self.assertEqual(t.worker_ssh_config(cfg), "")


class GitHub(unittest.TestCase):
    def test_the_default_transport_is_github_with_the_addresses_the_code_always_used(self):
        cfg = cfg_with()
        t = transport.for_config(cfg)
        self.assertEqual((t.kind, cfg.transport.kind), ("github", "github"))
        self.assertEqual(cfg.queue_url, "git@github.com:someone/armada-queue.git")
        self.assertEqual(cfg.results_url, "git@github.com:someone/armada-results.git")
        self.assertEqual(t.client_url(cfg, "results"), cfg.results_url)

    def test_the_workers_clone_uses_one_alias_and_so_one_deploy_key_per_repository(self):
        cfg = cfg_with()
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "git@github-armada-queue:someone/armada-queue.git")
        text = t.worker_ssh_config(cfg)
        for which in ("queue", "results"):
            self.assertIn(f"Host github-armada-{which}", text)
            self.assertIn(f"id_{which}", text)
        self.assertNotIn("ControlMaster", text)                                  # a background ssh master holds a pass's output pipes open
        self.assertTrue(t.uses_deploy_keys)

    def test_a_custom_name_and_repository_names_are_followed(self):
        cfg = cfg_with(identity={"name": "tenta-two"}, github={"owner": "someone", "queue_repo": "jobs-in"})
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "git@github-tenta-two-queue:someone/jobs-in.git")
        self.assertEqual(cfg.results_url, "git@github.com:someone/tenta-two-results.git")

    def test_an_unknown_transport_is_a_configuration_problem_not_a_crash(self):
        clean, problems = config.validate({"server": {"admin_user": "me", "host": "h.example"}, "github": {"owner": "someone"}, "transport": {"kind": "carrier-pigeon"}})
        self.assertTrue(any("transport" in p and "github" in p for p in problems), problems)


class CopyingHistory(unittest.TestCase):
    def test_both_the_origins_branches_and_the_clones_own_arrive_in_the_mailbox_as_branches(self):
        """With real git: the jobs branch (only ever a remote branch in a clone) and main both end up as refs/heads in the empty bare repository (`push --mirror` would have lost jobs)."""
        import subprocess
        import tempfile
        d = Path(tempfile.mkdtemp())
        git = lambda *a, cwd=None: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout   # noqa: E731
        git("init", "-q", "--bare", "-b", "main", str(d / "origin.git"))
        seed = d / "seed"
        git("clone", "-q", str(d / "origin.git"), str(seed))
        (seed / "a").write_text("1")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "one", cwd=seed)
        git("branch", "-M", "main", cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        git("checkout", "-q", "-b", "jobs", cwd=seed)
        (seed / "job").write_text("j")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "job", cwd=seed)
        git("push", "-q", "origin", "jobs", cwd=seed)
        git("clone", "-q", str(d / "origin.git"), str(d / "clone"))                      # a clone: main is local, jobs is only origin/jobs
        git("init", "-q", "--bare", "-b", "main", str(d / "mailbox.git"))
        for argv in transport.copy_history_commands(str(d / "clone"), str(d / "mailbox.git")):
            r = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        refs = git("for-each-ref", "--format=%(refname)", cwd=d / "mailbox.git").split()
        self.assertEqual(sorted(refs), ["refs/heads/jobs", "refs/heads/main"])


if __name__ == "__main__":
    unittest.main()

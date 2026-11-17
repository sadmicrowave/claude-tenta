"""The transport decides where the two repositories live; GitHub's answers are exactly what the code used to build by hand."""
import unittest

import support  # noqa: F401

from ct import config, transport


def cfg_with(**over):
    nested = {"server": {"admin_user": "me", "host": "worker.example"}, "github": {"owner": "someone"}, **over}
    clean, problems = config.validate(nested)
    assert not problems, problems
    return config.Config(clean)


class Direct(unittest.TestCase):
    def direct(self, **server):
        return cfg_with(transport={"kind": "direct"}, server={"admin_user": "me", "host": "worker.example", **server})

    def test_the_mailbox_is_on_the_worker_and_your_computer_reaches_it_as_the_mail_account(self):
        cfg = self.direct()
        t = transport.for_config(cfg)
        self.assertEqual((t.kind, t.uses_deploy_keys, t.mail_user(cfg)), ("direct", False, "claude-tenta-mail"))
        self.assertEqual(cfg.queue_url, "ssh://claude-tenta-mail" + "@" + "worker.example/var/lib/claude-tenta-mail/queue.git")
        self.assertEqual(cfg.results_url, "ssh://claude-tenta-mail" + "@" + "worker.example/var/lib/claude-tenta-mail/results.git")

    def test_a_non_standard_ssh_port_is_in_the_address(self):
        self.assertIn("worker.example:2222/", self.direct(ssh_port=2222).queue_url)

    def test_the_worker_uses_plain_paths_and_needs_no_ssh_configuration_at_all(self):
        cfg = self.direct()
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "/var/lib/claude-tenta-mail/queue.git")
        self.assertEqual(t.worker_ssh_config(cfg), "")


class GitHub(unittest.TestCase):
    def test_the_default_transport_is_github_with_the_addresses_the_code_always_used(self):
        cfg = cfg_with()
        t = transport.for_config(cfg)
        self.assertEqual((t.kind, cfg.transport.kind), ("github", "github"))
        self.assertEqual(cfg.queue_url, "git@github.com:someone/claude-tenta-queue.git")
        self.assertEqual(cfg.results_url, "git@github.com:someone/claude-tenta-results.git")
        self.assertEqual(t.client_url(cfg, "results"), cfg.results_url)

    def test_the_workers_clone_uses_one_alias_and_so_one_deploy_key_per_repository(self):
        cfg = cfg_with()
        t = transport.for_config(cfg)
        self.assertEqual(t.worker_url(cfg, "queue"), "git@github-claude-tenta-queue:someone/claude-tenta-queue.git")
        text = t.worker_ssh_config(cfg)
        for which in ("queue", "results"):
            self.assertIn(f"Host github-claude-tenta-{which}", text)
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


if __name__ == "__main__":
    unittest.main()

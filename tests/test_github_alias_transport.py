"""A GitHub worker fetches through its own ssh alias (git@github-NAME-queue:owner/repo.git), never through the name github.com. What it publishes as the connection it listens on must still be github:
seen on a real second worker, whose published state said direct, so this computer refused to send it a job (placement.transport.mismatch)."""
import unittest

import support  # noqa: F401
from ct import config, transport, workerstate


def cfg(kind="github"):
    clean, problems = config.validate({"identity": {"name": "wkb"}, "github": {"owner": "someone"}, "transport": {"kind": kind}, **({"server": {"host": "h.example", "admin_user": "me"}})}, require_alert_url=False)
    assert not problems, problems
    return config.Config(clean)


class AliasIsGithub(unittest.TestCase):
    def test_the_workers_own_alias_url_is_published_as_github(self):
        c = cfg()
        url = transport.for_config(c).worker_url(c, "queue")
        self.assertNotIn("github.com", url)                                              # the premise: the worker's remote never names github.com
        facts = workerstate.transport_facts("github", transport.canonical_origin(c, url))
        self.assertEqual(facts, {"transport": "github"})

    def test_a_direct_workers_local_path_stays_direct_and_a_plain_github_url_is_left_alone(self):
        c = cfg("direct")
        self.assertEqual(workerstate.transport_facts("direct", transport.canonical_origin(c, "/var/lib/x-mail/queue.git")), {"transport": "direct"})
        self.assertEqual(transport.canonical_origin(c, "git@github.com:someone/q.git"), "git@github.com:someone/q.git")
        self.assertEqual(transport.canonical_origin(c, ""), "")


if __name__ == "__main__":
    unittest.main()

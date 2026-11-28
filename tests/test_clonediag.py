import subprocess
import tempfile
import unittest

import support  # noqa: F401

ADMIN = "agent" + "@" + "worker.example"
from armada import clonediag
from armada.client.repo import ClientError, ClientRepos


class Classify(unittest.TestCase):
    DUBIOUS = "fatal: detected dubious ownership in repository at '/var/lib/arm2-mail/results.git'\nTo add an exception for this directory, call:\n\n\tgit config --global --add safe.directory /var/lib/arm2-mail/results.git\n"

    def test_dubious_ownership_names_its_own_fix_not_the_general_list(self):
        cause, meaning, steps = clonediag.classify(self.DUBIOUS, admin=ADMIN)
        self.assertEqual(cause, "dubious-ownership")
        self.assertIn("another account", meaning)
        joined = " ".join(steps)
        self.assertIn(f"ssh -t {ADMIN} 'sudo git config --system --add safe.directory /var/lib/arm2-mail/results.git'", joined)
        self.assertNotIn("Is the worker on", joined)                                  # the general list would have sent the person to re-run an installer that cannot fix this
        self.assertNotIn("run arm-setup install again", joined)

    def test_each_known_cause_has_its_own_steps(self):
        cases = {"key-refused": "agent@h: Permission denied (publickey).", "host-key": "Host key verification failed.", "unreachable": "ssh: connect to host h port 22: Connection timed out",
                 "no-mailbox": "fatal: '/x/queue.git' does not appear to be a git repository", "shell-refused": "This account can only fetch from the queue and results repositories"}
        seen = set()
        for want, text in cases.items():
            cause, meaning, steps = clonediag.classify(text, admin="agent"+"@"+"h", host="h")
            self.assertEqual(cause, want, text)
            self.assertTrue(meaning and steps, want)
            seen.add(tuple(steps))
        self.assertEqual(len(seen), len(cases))                                      # no two causes share one list

    def test_the_mailbox_causes_are_not_offered_for_a_github_install(self):
        cause, meaning, steps = clonediag.classify("fatal: repository 'git@github.com:o/q.git' does not exist", kind="github")
        self.assertEqual(cause, "no-repository")
        self.assertIn("ssh -T git@github.com", steps[0])
        self.assertNotIn("mailbox", meaning)
        self.assertIsNone(clonediag.classify(self.DUBIOUS, kind="github")[0])                  # dubious ownership is the mailbox's problem only

    def test_something_unrecognised_gets_the_general_list_by_returning_nothing(self):
        self.assertEqual(clonediag.classify("fatal: something new"), (None, "", []))


class InTheClone(unittest.TestCase):
    def test_a_failed_clone_carries_the_real_error_the_cause_and_the_matching_steps(self):
        d = tempfile.mkdtemp()
        repos = ClientRepos(d + "/q", d + "/r", "ssh://x-mail" + "@" + "h.example" + d + "/nope.git", d + "/nope2.git", admin_target=ADMIN, kind="direct",
                            ssh_command="false")                                    # `false` stands in for ssh: it fails, and the error text is what the classifier reads
        repos.queue_url = d + "/nope.git"                                           # a path that is not a repository: git's own message
        with self.assertRaises(ClientError) as e:
            repos.clone_all(d)
        self.assertIn("does not exist", e.exception.why)                            # git's own words are kept
        self.assertIn("mailbox repository is not there", e.exception.why)           # and the cause is named in plain words
        self.assertTrue(any("arm-setup install again" in s for s in e.exception.steps))


if __name__ == "__main__":
    unittest.main()

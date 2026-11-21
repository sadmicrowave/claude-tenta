"""Setting a Windows computer up as a worker (src/ct/setup/winsetup.py). The steps on the Windows computer are the modules' own (each tested with its own file); here the order, what is written on THIS computer, the failure
messages and the first job are tested, with a runner that answers each step's script by what it contains."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import support  # noqa: F401
from ct.setup import winsetup, wintools
from ct.setup.server import SetupError

TOOLS = {"git": "git version 2.56.1.windows.1", "python": "Python 3.13.17", "changed": False}
ANSWERS = [("requirements.txt", TOOLS), ("claude-tenta worker (standard account)", {"user": "ctworker", "changed": True}), ("robocopy", {"commit": "a" * 40, "changed": True}), ("claude-tenta mailbox account", {"user": "claude-tenta-mail", "changed": True}),
           ("$cfgB64", {"changed": True}), ("started = $true", {"started": True}), ("Register-ScheduledTask -TaskName $tn -Action $action -Trigger", {"changed": True})]


class FakeRepos:
    def __init__(self, result=None):
        self.pushed, self.result, self.cloned, self.jobs_branch = [], result, False, "jobs"

    def clone_all(self, home):
        self.cloned = True
        return [("cloned", "queue"), ("cloned", "results")]

    def push_files(self, branch, files, msg, **kw):
        self.pushed.append((branch, files, msg))

    def pull_results(self, max_age=0):
        return None

    def read_result(self, jid):
        return self.result(jid) if callable(self.result) else self.result


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent = []
        self.repos = FakeRepos(result=lambda jid: {"id": jid, "status": "ok"})

    def runner(self, fail_on=None):
        def run(argv, text):
            self.sent.append(text)
            for marker, answer in ANSWERS:
                if marker in text:
                    if fail_on == marker:
                        return 1, 'CT-ERROR {"what": "boom", "detail": "the real words"}'
                    return 0, "CT-RESULT " + json.dumps(answer)
            raise AssertionError("no answer for a script that starts: " + text[:80])
        return run

    def installer(self, fail_on=None, release=None, **kw):
        rel = release if release is not None else {"checked": 1, "tag": "release-win-0.1.0", "commit": "b" * 40}
        return winsetup.WindowsInstaller(self.tmp / "conf" / "win.toml", self.tmp / "home", "win", "192.0.2.50", "me", {}, run=self.runner(fail_on), find_release=lambda url, platform=None: rel,
                                         repos_factory=lambda cfg: self.repos, sleep=lambda s: None, wait_s=10, **kw)


class Whole(Fixture):
    def test_every_step_runs_in_order_and_the_report_says_each_one(self):
        report = self.installer().run_all()
        self.assertEqual([r[0] for r in report], ["your configuration", "mailbox key", "tools", "accounts and folders", "release", "mailbox", "worker files", "worker task", "your copies of the mailbox", "first job"])
        self.assertTrue(all(r[1] != "failed" for r in report))
        order = [next(m for m, _ in ANSWERS if m in t) for t in self.sent]
        self.assertEqual(order, ["requirements.txt", "claude-tenta worker (standard account)", "robocopy", "claude-tenta mailbox account", "$cfgB64", "Register-ScheduledTask -TaskName $tn -Action $action -Trigger", "started = $true"])

    def test_the_computers_own_configuration_is_written_once_with_its_client_folder_and_kept_after(self):
        inst = self.installer()
        inst.run_all()
        text = (self.tmp / "conf" / "win.toml").read_text()
        self.assertIn(f'client_home = "{self.tmp / "home"}"', text)
        self.assertIn('host = "192.0.2.50"', text)
        (self.tmp / "conf" / "win.toml").write_text(text + "# my edit\n")
        self.installer().run_all()
        self.assertIn("# my edit", (self.tmp / "conf" / "win.toml").read_text())

    def test_the_mailbox_key_is_made_once_and_only_its_public_half_goes_to_the_windows_computer(self):
        self.installer().run_all()
        key = self.tmp / "home" / "mailbox" / "key"
        self.assertTrue(key.exists())
        before = key.read_text()
        self.installer().run_all()
        self.assertEqual(key.read_text(), before)
        sent = "\n".join(self.sent)
        self.assertIn(Path(str(key) + ".pub").read_text().split()[1], sent)
        self.assertNotIn("mailbox\n", sent)                                                  # and neither is the key's comment
        self.assertNotIn(before.splitlines()[1], sent)                                        # the private half never leaves this computer

    def test_the_first_job_is_the_packs_test_job_pushed_to_the_jobs_branch_like_ct_run_does(self):
        self.installer().run_all()
        (branch, files, msg), = self.repos.pushed
        self.assertEqual(branch, "jobs")
        (path, body), = files.items()
        self.assertRegex(path, r"^jobs/win-status-[a-z0-9]{6}\.json$")
        self.assertEqual(json.loads(body)["class"], "win-status")

    def test_the_release_comes_from_the_latest_windows_tag_commit(self):
        self.installer().run_all()
        self.assertIn("b" * 40, next(t for t in self.sent if "robocopy" in t))


class Failures(Fixture):
    def test_each_step_that_fails_stops_with_the_real_text_and_says_to_run_it_again(self):
        for marker in ("requirements.txt", "claude-tenta worker (standard account)", "robocopy", "claude-tenta mailbox account", "$cfgB64"):
            with self.assertRaises(SetupError, msg=marker) as e:
                self.installer(fail_on=marker).run_all()
            self.assertIn("the real words", e.exception.why)
            self.assertIn("Run the same command again", " ".join(e.exception.steps))

    def test_no_published_windows_release_stops_before_anything_is_changed_on_the_windows_computer(self):
        with self.assertRaises(SetupError) as e:
            self.installer(release={"checked": 1}).run_all()
        self.assertIn("No Windows release has been published", e.exception.title)
        self.assertEqual(self.sent, [])

    def test_a_release_that_cannot_be_looked_up_says_why(self):
        with self.assertRaises(SetupError) as e:
            self.installer(release={"checked": 1, "error": "could not reach the release source"}).run_all()
        self.assertIn("could not reach", e.exception.why)
        self.assertEqual(self.sent, [])

    def test_a_first_job_that_does_not_come_back_is_not_called_a_success(self):
        self.repos.result = lambda jid: {"id": jid, "status": "error"}
        with self.assertRaises(SetupError) as e:
            self.installer().run_all()
        self.assertIn("first job did not come back", e.exception.title)
        self.assertIn("worker.log", " ".join(e.exception.steps))

    def test_no_result_at_all_within_the_wait_is_a_failure_too(self):
        self.repos.result = None
        with self.assertRaises(SetupError) as e:
            self.installer().run_all()
        self.assertIn("no result within", e.exception.why)


if __name__ == "__main__":
    unittest.main()

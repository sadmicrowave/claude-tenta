"""Finding F5: what a worker accepts onto its disk is decided BEFORE a push is written, not only when the job file is read.
The old behaviour (a push of any size lands in the queue repository, because the worker's ceiling is checked afterwards) is pinned by the real-git test below, which fails without the cap."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

import support  # noqa: F401

from ct import intake, mailshell

Usage = namedtuple("Usage", "total used free")
GB = 1e9


class Decision(unittest.TestCase):
    def test_below_the_reserve_a_push_is_refused(self):
        why, facts = intake.push_decision(free_gb=1.9, total_gb=20, owner_mb=None, mem_mb=8000)
        self.assertEqual(why, "disk")
        self.assertEqual(facts, {"free": "1.9", "reserve": "2"})
        why, facts = intake.push_decision(free_gb=9.0, total_gb=100, owner_mb=None, mem_mb=8000)       # 10% of 100 GB
        self.assertEqual((why, facts["reserve"]), ("disk", "10"))

    def test_above_the_reserve_the_cap_is_the_ceiling_plus_slack(self):
        why, facts = intake.push_decision(free_gb=100, total_gb=500, owner_mb=None, mem_mb=64000)
        self.assertIsNone(why)
        self.assertEqual(facts["ceiling"], 1024)
        self.assertEqual(facts["cap_bytes"], (1024 + intake.PUSH_SLACK_MB) * 1_000_000)

    def test_the_cap_shrinks_with_free_disk_and_memory_and_the_owner_wins(self):
        self.assertEqual(intake.push_decision(8, 20, None, 64000)[1]["ceiling"], 1024)                  # a quarter of 8 GB is 2000: 1024 is the top
        self.assertEqual(intake.push_decision(3, 20, None, 64000)[1]["ceiling"], 750)               # a quarter of 3 GB
        self.assertEqual(intake.push_decision(100, 500, None, 800)[1]["ceiling"], 200)               # a quarter of 800 MB of memory
        self.assertEqual(intake.push_decision(100, 500, 50, 800)[1]["ceiling"], 50)                  # hardware.max_job_mb

    def test_the_worker_and_the_mailbox_use_the_same_numbers(self):
        for free, mem in ((0.5, None), (5, 2000), (400, 64000)):
            self.assertEqual(intake.ceiling_mb(None, free, mem), int(max(1, min(1024.0, free * 250, *( [mem / 4] if mem is not None else [])))))


class ReceiveGuard(unittest.TestCase):
    def run_receive(self, usage, env=None, git_ok=True):
        err = io.StringIO()
        ran = mock.Mock(stdout=mock.Mock(fileno=lambda: 5), wait=lambda: 0 if git_ok else 1)
        with mock.patch("sys.stderr", err), mock.patch("ct.mailshell.subprocess.Popen", return_value=ran) as run, mock.patch("ct.mailshell.os.read", return_value=b""), mock.patch("ct.mailshell.os.write"), mock.patch("ct.mailshell.mem_available_mb", return_value=64000.0):
            rc = mailshell._receive(["git-receive-pack", "/x/queue.git"], env or {"CT_MAILBOX": "/x"}, "/x", usage=lambda p: usage)
        return rc, err.getvalue(), run

    def test_a_full_disk_refuses_before_git_runs_and_says_the_command(self):
        rc, said, run = self.run_receive(Usage(20 * GB, 19 * GB, 1 * GB))
        self.assertEqual(rc, 1)
        run.assert_not_called()
        self.assertIn("1.0 GB is free on this worker and 2 GB is kept in reserve", said)
        self.assertIn("Nothing was sent", said)

    def test_a_roomy_disk_runs_git_with_the_cap_set(self):
        rc, said, run = self.run_receive(Usage(500 * GB, 100 * GB, 400 * GB))
        self.assertEqual(rc, 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual((env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"], env["GIT_CONFIG_COUNT"]), ("receive.maxInputSize", str((1024 + intake.PUSH_SLACK_MB) * 1_000_000), "2"))
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "safe.directory")                                   # the ownership trust is kept

    def test_git_saying_too_big_adds_the_plain_words_even_when_split_between_reads_and_exit_zero(self):
        err = io.StringIO()
        ran = mock.Mock(stdout=mock.Mock(fileno=lambda: 5), wait=lambda: 0)
        with mock.patch("sys.stderr", err), mock.patch("ct.mailshell.subprocess.Popen", return_value=ran), mock.patch("ct.mailshell.mem_available_mb", return_value=64000.0), \
                mock.patch("ct.mailshell.os.read", side_effect=[b"fatal: pack exceeds maxi", b"mum allowed size", b""]), mock.patch("ct.mailshell.os.write") as wrote:
            mailshell._receive(["git-receive-pack", "/x/queue.git"], {"CT_MAILBOX": "/x"}, "/x", usage=lambda p: Usage(500 * GB, 100 * GB, 400 * GB))
        self.assertIn("this push is over the 1024 MB", err.getvalue())
        self.assertEqual(b"".join(c.args[1] for c in wrote.call_args_list), b"fatal: pack exceeds maximum allowed size")      # git's own words pass through unchanged

    def test_an_unreadable_disk_does_not_block(self):
        with mock.patch("ct.mailshell.subprocess.Popen", return_value=mock.Mock(stdout=mock.Mock(fileno=lambda: 5), wait=lambda: 0)) as run, mock.patch("ct.mailshell.os.read", return_value=b""):
            self.assertEqual(mailshell._receive(["git-receive-pack", "/x/queue.git"], {"CT_MAILBOX": "/x"}, "/x", usage=mock.Mock(side_effect=OSError)), 0)
        self.assertNotIn("GIT_CONFIG_KEY_1", run.call_args.kwargs["env"])

    def test_the_owner_ceiling_comes_from_the_launcher_on_windows_and_the_configuration_elsewhere(self):
        self.assertEqual(mailshell.owner_ceiling({"CT_MAX_JOB_MB": "300", "CT_MAILBOX": "C:/x"}), 300)
        self.assertIsNone(mailshell.owner_ceiling({"CT_MAILBOX": "C:/x"}))                          # a launcher that says nothing: automatic, the configuration is never opened
        self.assertIsNone(mailshell.owner_ceiling({"CT_MAX_JOB_MB": "not a number"}))


@unittest.skipUnless(os.path.exists("/usr/bin/git-receive-pack") and shutil.which("git"), "needs git's receive-pack in /usr/bin")
class RealGitPush(unittest.TestCase):
    """The old behaviour, for real: a push over the mailbox shell. Without the cap the whole pack is accepted whatever the worker's ceiling is."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.box = self.tmp / "box"
        (self.box).mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.box / "queue.git")], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.box / "results.git")], check=True)
        self.client = self.tmp / "c"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.client)], check=True)
        ssh = self.tmp / "fakessh"
        shell = self.tmp / "shell.py"                                                                  # the mailbox shell, on a disk that has room (this computer's own disk must not decide the test)
        shell.write_text("import shutil, sys\nfrom collections import namedtuple\nU = namedtuple('U', 'total used free')\nshutil.disk_usage = lambda p: U(500e9, 100e9, 400e9)\nfrom ct import mailshell\nsys.exit(mailshell.main())\n")
        ssh.write_text(f'#!/bin/sh\nshift\nSSH_ORIGINAL_COMMAND="$*" CT_MAILBOX="{self.box}" CT_MAX_JOB_MB="${{FAKE_CEILING_MB:-}}" PYTHONPATH="{Path(mailshell.__file__).parents[1]}" exec "{sys.executable}" "{shell}"\n')
        ssh.chmod(0o755)
        self.env = {**os.environ, "GIT_SSH_COMMAND": str(ssh), "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"}

    def push(self, mb, ceiling):
        (self.client / "job.bin").write_bytes(os.urandom(mb * 1_000_000))
        for cmd in (["add", "."], ["commit", "-qm", "j"]):
            subprocess.run(["git", "-C", str(self.client), *cmd], check=True, env=self.env)
        env = {**self.env, "FAKE_CEILING_MB": str(ceiling)}
        return subprocess.run(["git", "-C", str(self.client), "push", f"ssh://mail@worker{self.box}/queue.git", "main"], capture_output=True, text=True, env=env)

    def test_a_pack_over_the_ceiling_is_stopped_and_nothing_stays_on_the_disk(self):
        r = self.push(mb=40, ceiling=5)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("this push is over the 5 MB this worker accepts right now", r.stderr)
        size = sum(f.stat().st_size for f in (self.box / "queue.git").rglob("*") if f.is_file())
        self.assertLess(size, 1_000_000, "the rejected pack must not stay in the repository")

    def test_a_pack_under_the_ceiling_still_arrives(self):
        r = self.push(mb=1, ceiling=5)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

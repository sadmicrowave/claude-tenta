import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import gitio, util  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd}: {r.stderr}"
    return r.stdout.strip()


class Repos(unittest.TestCase):
    """A bare 'GitHub' repo for the queue and for the results, a requester's clone of the queue, and the worker's two clones."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        for name in ("queue", "results"):
            sh("git", "init", "-q", "--bare", "-b", "main", str(t / f"{name}.git"))
        sh("git", "clone", "-q", str(t / "queue.git"), str(t / "requester"))
        (t / "requester" / "README.md").write_text("queue\n")
        sh("git", "add", "-A", cwd=t / "requester")
        sh("git", "commit", "-q", "-m", "init", cwd=t / "requester")
        sh("git", "push", "-q", "origin", "main", cwd=t / "requester")
        sh("git", "clone", "-q", str(t / "queue.git"), str(t / "w-queue"))
        sh("git", "clone", "-q", str(t / "results.git"), str(t / "w-results"))        # empty: no branch yet
        self.state = t / "state"
        (self.state / "inbox").mkdir(parents=True)
        self.logged = []
        self.io = gitio.GitIO(t / "w-queue", t / "w-results", self.state, log=self.logged.append, lock_wait=1)

    def push_job(self, jid, branch="jobs", folder="jobs", body=None, message="job"):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", branch, "origin/" + branch if self.remote_branch(branch) else "origin/main", cwd=req)
        (req / folder).mkdir(exist_ok=True)
        (req / folder / f"{jid}.json").write_text(body or json.dumps({"id": jid}))
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", message, cwd=req)
        sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=req)

    def remote_branch(self, branch):
        return subprocess.run(["git", "-C", str(self.tmp / "queue.git"), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], capture_output=True).returncode == 0

    def results_log(self):
        return sh("git", "log", "--topo-order", "--format=%an|%s", "main", cwd=self.tmp / "results.git").splitlines()      # --topo-order: newest first even when commits share a second


class Jobs(Repos):
    def test_sync_then_listing_reads_jobs_from_the_jobs_branch(self):
        self.push_job("job-0001")
        self.assertEqual(self.io.job_listing(), {})                                   # nothing fetched yet
        self.assertTrue(self.io.sync())
        self.assertEqual(self.io.job_listing(), {"job-0001": ("origin/jobs", "jobs/job-0001.json")})
        self.assertEqual(json.loads(self.io.job_bytes("job-0001")), {"id": "job-0001"})
        self.assertIsNone(self.io.job_bytes("job-9999"))

    def test_the_jobs_branch_wins_over_main_and_main_still_works_for_older_clients(self):
        self.push_job("job-0001", body='{"from": "jobs"}')
        self.push_job("job-0001", branch="main", body='{"from": "main"}')
        self.push_job("job-0002", branch="main", body='{"from": "main"}')
        self.io.sync()
        listing = self.io.job_listing()
        self.assertEqual(listing["job-0001"][0], "origin/jobs")
        self.assertEqual(listing["job-0002"][0], "origin/main")
        self.assertEqual(json.loads(self.io.job_bytes("job-0001")), {"from": "jobs"})

    def test_a_job_the_worker_has_already_seen_is_read_from_its_inbox_copy(self):
        self.push_job("job-0003", body='{"v": 1}')
        self.io.sync()
        (self.state / "inbox" / "job-0003.json").write_bytes(b'{"v": 1}')
        self.push_job("job-0003", body='{"v": 2}')                                      # the requester edits it afterwards
        self.io.sync()
        self.assertEqual(self.io.job_bytes("job-0003"), b'{"v": 1}')                    # first seen wins: what was approved is what runs

    def test_only_json_files_in_the_jobs_folder_are_jobs_and_deleted_branches_are_pruned(self):
        self.push_job("job-0004")
        req = self.tmp / "requester"
        (req / "jobs" / "notes.txt").write_text("x")
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "notes", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.io.sync()
        self.assertEqual(list(self.io.job_listing()), ["job-0004"])
        sh("git", "push", "-q", "origin", "--delete", "jobs", cwd=req)
        self.io.sync()
        self.assertEqual(self.io.job_listing(), {})                                     # the branch is gone from the worker's clone too

    def test_sync_with_an_empty_results_repo_is_fine(self):
        self.assertTrue(self.io.sync())
        self.assertEqual(self.logged, [])

    def test_sync_skips_when_the_lock_is_busy(self):
        with self.io.lock(wait=0) as held:
            self.assertTrue(held.got)
            self.assertFalse(self.io.sync())
        self.assertIn("lock busy", self.logged[0])


class Publishing(Repos):
    res = {"id": "job-0001", "status": "ok", "output": "hi"}

    def test_a_result_is_committed_pushed_and_authored_as_the_configured_worker(self):
        self.assertTrue(self.io.publish(self.res))
        self.assertEqual(self.results_log(), ["cw-worker|result job-0001: ok"])
        shown = sh("git", "show", "main:results/job-0001.json", cwd=self.tmp / "results.git")
        self.assertEqual(json.loads(shown)["output"], "hi")

    def test_two_results_make_two_commits_and_other_files_can_be_published(self):
        self.io.publish(self.res)
        self.io.publish({"id": "job-0002", "status": "error"})
        self.assertTrue(self.io.publish_file("registry.json", "{}\n", "registry: installed"))
        self.assertEqual(len(self.results_log()), 3)

    def test_a_push_that_loses_a_race_pulls_and_retries(self):
        self.io.publish(self.res)
        other = self.tmp / "other"
        sh("git", "clone", "-q", str(self.tmp / "results.git"), str(other))
        (other / "README.md").write_text("someone else\n")
        sh("git", "add", "-A", cwd=other)
        sh("git", "commit", "-q", "-m", "other writer", cwd=other)
        sh("git", "push", "-q", "origin", "main", cwd=other)                              # the remote moves under the worker
        self.assertTrue(self.io.publish({"id": "job-0003", "status": "ok"}))
        self.assertEqual(self.results_log()[0], "cw-worker|result job-0003: ok")
        self.assertIn("other writer", " ".join(self.results_log()))

    def test_a_failing_remote_reports_false_and_logs(self):
        sh("git", "remote", "set-url", "origin", str(self.tmp / "nowhere.git"), cwd=self.tmp / "w-results")
        self.assertFalse(self.io.publish(self.res))
        self.assertTrue(any("push failed" in m for m in self.logged))

    def test_a_busy_lock_means_try_later(self):
        with self.io.lock(wait=0):
            self.assertFalse(self.io.publish(self.res))
        self.assertTrue(any("lock busy" in m for m in self.logged))
        self.assertEqual(self.io.git(self.tmp / "results.git", "rev-list", "--count", "--all").stdout.strip(), "0")


class Pruning(Repos):
    def publish_old_and_new(self):
        old = util.iso(util.now() - timedelta(days=30))
        new = util.iso(util.now() - timedelta(days=1))
        self.io.publish({"id": "job-old1", "status": "ok", "finished": old})
        self.io.publish({"id": "job-new1", "status": "ok", "finished": new})
        self.io.publish({"id": "job-nodate", "status": "ok"})
        self.io.publish_file("registry.json", "{}\n", "registry")

    def test_nothing_happens_below_the_threshold(self):
        self.publish_old_and_new()
        self.assertIn("no prune needed", self.io.prune_results(days=14, threshold=200))
        self.assertEqual(len(self.results_log()), 4)

    def test_history_is_squashed_old_results_dropped_and_other_files_kept(self):
        self.publish_old_and_new()
        msg = self.io.prune_results(days=14, force=True)
        self.assertIn("4 commits -> 1, dropped 1 results older than 14 days", msg)
        self.assertEqual(len(self.results_log()), 1)
        files = sh("git", "ls-tree", "-r", "--name-only", "main", cwd=self.tmp / "results.git").splitlines()
        self.assertEqual(sorted(files), ["registry.json", "results/job-new1.json", "results/job-nodate.json"])   # undated is kept
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.tmp / "w-results"), sh("git", "rev-parse", "main", cwd=self.tmp / "results.git"))
        self.assertFalse((self.state / "prune.index").exists())

    def test_a_remote_that_moved_during_the_prune_is_left_alone(self):
        self.publish_old_and_new()
        original = self.io.git

        def sneaky(repo, *args, **kw):
            if args[:1] == ("ls-remote",) and "refs/heads/main" in args:
                r = original(repo, *args, **kw)
                other = self.tmp / "other"                                                 # another writer pushes right after we looked
                sh("git", "clone", "-q", str(self.tmp / "results.git"), str(other))
                (other / "x.txt").write_text("x")
                sh("git", "add", "-A", cwd=other)
                sh("git", "commit", "-q", "-m", "sneaky", cwd=other)
                sh("git", "push", "-q", "origin", "main", cwd=other)
                return r
            return original(repo, *args, **kw)
        self.io.git = sneaky
        msg = self.io.prune_results(days=14, force=True)
        self.assertIn("nothing was changed", msg)
        self.assertEqual(len(self.results_log()), 5)                                        # history intact, plus the sneaky commit


class Environment(unittest.TestCase):
    def test_git_never_prompts_or_waits_forever(self):
        env = gitio.harden_git_env({})
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        mine = gitio.harden_git_env({"GIT_SSH_COMMAND": "custom"})
        self.assertEqual(mine["GIT_SSH_COMMAND"], "custom")                                  # an explicit choice is respected

    def test_the_lock_is_exclusive_and_bounded(self):
        path = Path(tempfile.mkdtemp()) / "x.lock"
        with gitio.FileLock(path, wait=0) as a:
            self.assertTrue(a.got)
            t0 = time.time()
            with gitio.FileLock(path, wait=0.5) as b:
                self.assertFalse(b.got)
            self.assertGreaterEqual(time.time() - t0, 0.4)
        with gitio.FileLock(path, wait=0) as c:
            self.assertTrue(c.got)                                                            # free again once released


if __name__ == "__main__":
    unittest.main()

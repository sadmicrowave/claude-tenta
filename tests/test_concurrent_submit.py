"""Several ct-run submits at once against the same queue (a script in a loop, two sessions, a load test): every job must arrive, or the send must fail loudly. Never "sent" and gone.

Found by the first real load test: 8 simultaneous submits left 2 jobs reported as sent that were not in the queue, and 2 more failed with a git lock message. Every process built its commit in
the same temporary index file."""
import json
import threading
import unittest

from support import RepoFixture  # noqa: F401  (support sets HOME to a throwaway folder)

from ct.client import submit as sub


class ManyAtOnce(RepoFixture):
    def burst(self, n):
        errors, ids = [], [f"sys-status-{i:06d}" for i in range(n)]
        start = threading.Barrier(n)

        def one(jid):
            start.wait()
            try:
                sub.push_job(self.repos, jid, {"class": "sys-status", "params": {}})
            except Exception as e:                      # a refusal is allowed (it is loud); a silent loss is not
                errors.append((jid, str(e)[:100]))
        threads = [threading.Thread(target=one, args=(j,)) for j in ids]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.repos.fetch_queue()
        names = self.repos.git(self.repos.queue, "ls-tree", "-r", "--name-only", f"origin/{self.repos.jobs_branch}", check=False).stdout.split()
        return ids, errors, {n.split("/")[-1][:-5] for n in names}

    def test_every_job_that_was_reported_sent_is_in_the_queue_and_nothing_fails_on_a_lock(self):
        for _ in range(3):
            ids, errors, present = self.burst(8)
            lost = [j for j in ids if j not in present and j not in {e[0] for e in errors}]
            self.assertEqual(lost, [], "reported as sent, but not in the queue")
            self.assertEqual([e for e in errors if "remove the file manually" in e[1] or "lock" in e[1].lower()], [], errors)

    def test_a_job_pushed_by_someone_else_between_reading_the_branch_and_committing_is_not_dropped(self):
        """The root cause, made deterministic: the branch name was read twice (once for the file tree, once for the commit's parent). If it moved in between, the new commit had the NEWER parent
        and the OLDER tree: a valid fast-forward that silently deleted what had just been added. Found on a Linux runner, where the race was lost about one run in ten."""
        from ct.client.repo import ClientRepos
        other = ClientRepos(self.tmp / "other-queue", self.tmp / "other-results", str(self.tmp / "queue.git"), str(self.tmp / "results.git"), sleep=lambda s: None)
        other.clone_all(self.tmp / "other-home")
        sub.push_job(self.repos, "sys-status-000001", {"class": "sys-status", "params": {}})                 # so the branch exists and has a first job
        real, fired = self.repos._plumb, []

        def plumb(env, *args, **kw):
            out = real(env, *args, **kw)
            if args and args[0] == "read-tree" and not fired:
                fired.append(1)                                                                                # right after the tree is read: someone else pushes, and this clone fetches it
                sub.push_job(other, "sys-status-000002", {"class": "sys-status", "params": {}})
                self.repos.fetch_queue()
            return out
        self.repos._plumb = plumb
        sub.push_job(self.repos, "sys-status-000003", {"class": "sys-status", "params": {}})
        self.repos._plumb = real
        self.repos.fetch_queue()
        names = self.repos.git(self.repos.queue, "ls-tree", "-r", "--name-only", f"origin/{self.repos.jobs_branch}", check=False).stdout.split()
        self.assertEqual(sorted(n.split("/")[-1][:-5] for n in names), ["sys-status-000001", "sys-status-000002", "sys-status-000003"])

    def test_with_no_contention_all_arrive(self):
        ids, errors, present = self.burst(1)
        self.assertEqual((errors, present), ([], set(ids)))


if __name__ == "__main__":
    unittest.main()


class ReadingTheJobsBranchCostsOneProcessNotOnePerJob(RepoFixture):
    """The profile of one real send: 5.3 of its 8.5 s were the host cap reading every job file on the queue branch with its own `git show` (90 processes for about 45 jobs). The cost grew with every job
    sent, until the branch was pruned."""

    def test_all_the_files_are_read_in_one_git_process_and_are_the_same_text_as_one_by_one(self):
        for i in range(12):
            sub.push_job(self.repos, f"sys-status-{i:06d}", {"class": "sys-status", "params": {}})
        self.repos.fetch_queue()
        names = self.repos.git(self.repos.queue, "ls-tree", "-r", "--name-only", f"origin/{self.repos.jobs_branch}", check=False).stdout.split()
        ref = f"origin/{self.repos.jobs_branch}"
        one_by_one = {p: self.repos.show_file(ref, p) for p in names}
        import subprocess as sp
        from unittest import mock
        real, runs = sp.run, []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: runs.append(a[0]) or real(*a, **k)):
            batch = self.repos.show_files(ref, names)
        self.assertEqual(batch, one_by_one)
        self.assertEqual(len(runs), 1, "one git process for every file")

    def test_a_missing_path_is_none_and_the_files_after_it_are_still_read(self):
        sub.push_job(self.repos, "sys-status-000001", {"class": "sys-status", "params": {}})
        self.repos.fetch_queue()
        ref = f"origin/{self.repos.jobs_branch}"
        got = self.repos.show_files(ref, ["jobs/none.json", "jobs/sys-status-000001.json"])
        self.assertIsNone(got["jobs/none.json"])
        self.assertIn("sys-status-000001", got["jobs/sys-status-000001.json"])

    def test_non_ascii_text_is_read_whole(self):
        sub.push_job(self.repos, "sys-status-000002", {"class": "sys-status", "params": {"note": "café — ok"}})
        self.repos.fetch_queue()
        ref = f"origin/{self.repos.jobs_branch}"
        self.assertEqual(self.repos.show_files(ref, ["jobs/sys-status-000002.json"])["jobs/sys-status-000002.json"], self.repos.show_file(ref, "jobs/sys-status-000002.json"))


class NoRepeatedNetworkTrips(RepoFixture):
    """Profile of one real send: about 11 fetches and pulls, several of them a repeat of one made a second earlier. A look that was just made may be reused by the parts that only need a recent picture
    (the host cap, the pruning); the lock check before sending is never reused."""

    def fetches(self, fn):
        import subprocess as sp
        from unittest import mock
        real, runs = sp.run, []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: runs.append(a[0]) or real(*a, **k)):
            fn()
        return [r for r in runs if "fetch" in r]

    def test_a_recent_look_is_reused_when_the_caller_allows_it_and_never_otherwise(self):
        self.repos.fetch_queue()
        self.repos.pull_results()
        self.assertEqual(len(self.fetches(lambda: self.repos.fetch_queue(max_age=30))), 0)
        self.assertEqual(len(self.fetches(lambda: self.repos.pull_results(max_age=30))), 0)
        self.assertEqual(len(self.fetches(lambda: self.repos.fetch_queue())), 1)                 # no max_age: always real
        self.assertEqual(len(self.fetches(lambda: self.repos.pull_results())), 1)

    def test_a_reused_pull_still_reports_the_lock(self):
        (self.repos.results / "security.json").write_text('{"locked": true}')
        first = self.repos.pull_results()
        self.assertEqual(first, self.repos.pull_results(max_age=30))

    def test_the_old_look_is_not_reused(self):
        self.repos.fetch_queue()
        self.repos._seen["queue"] -= 100
        self.assertEqual(len(self.fetches(lambda: self.repos.fetch_queue(max_age=30))), 1)

    def test_the_cap_check_reuses_a_look_made_moments_ago(self):
        from ct.client import cap
        self.repos.fetch_queue()
        self.repos.pull_results()
        self.assertEqual(len(self.fetches(lambda: cap.in_flight(self.repos))), 0)


class PruningIsNotDoneOnEverySend(unittest.TestCase):
    def test_it_is_due_once_an_hour(self):
        import tempfile
        from ct.client import place
        home = tempfile.mkdtemp()
        self.assertTrue(place.prune_due(home, now=1000))
        place.prune_done(home, now=1000)
        self.assertFalse(place.prune_due(home, now=1000 + 600))
        self.assertTrue(place.prune_due(home, now=1000 + 4000))

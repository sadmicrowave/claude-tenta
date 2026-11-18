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

    def test_with_no_contention_all_arrive(self):
        ids, errors, present = self.burst(1)
        self.assertEqual((errors, present), ([], set(ids)))


if __name__ == "__main__":
    unittest.main()

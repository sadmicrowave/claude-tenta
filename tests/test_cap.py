import json
import unittest
from datetime import datetime, timezone

import support  # noqa: F401
from ct.client import cap
from ct.client.repo import ClientError

NOW = 1_800_000_000.0


def stamp(age_s):
    return datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat().replace("+00:00", "Z")


class Repos:
    """A client repository with a jobs branch of {id: age seconds} and results {id: status}."""
    jobs_branch, queue = "jobs", "q"

    def __init__(self, jobs, results=None, fail=False):
        self.jobs, self.results, self.fail = jobs, results or {}, fail

    def fetch_queue(self):
        if self.fail:
            raise OSError("no route")

    def pull_results(self):
        self.fetch_queue()

    def git(self, repo, *a, check=True):
        class R:
            stdout = "\n".join(f"jobs/{j}.json" for j in self.jobs)
        return R()

    def show_file(self, ref, path):
        jid = path.split("/")[-1][:-5]
        return json.dumps({"created": stamp(self.jobs[jid])})

    def read_result(self, jid):
        return {"status": self.results[jid]} if jid in self.results else None


class Cap(unittest.TestCase):
    def test_queued_running_and_unseen_jobs_count_but_finished_and_awaiting_approval_do_not(self):
        r = Repos({"a": 60, "b": 60, "c": 60, "d": 60, "e": 60}, {"a": "ok", "b": "running", "c": "queued", "d": "awaiting_approval"})
        self.assertEqual(sorted(cap.in_flight(r, NOW)), ["b", "c", "e"])

    def test_a_job_that_never_came_back_stops_counting_after_a_day(self):
        self.assertEqual(cap.in_flight(Repos({"old": 25 * 3600, "new": 10}), NOW), ["new"])

    def test_sending_past_the_cap_across_all_workers_is_refused_with_what_is_running_and_how_to_raise_it(self):
        fleet = [("main", Repos({"a": 1, "b": 1})), ("studio", Repos({"c": 1}))]
        self.assertEqual(cap.check(fleet, 4, 1, NOW), 3)               # 3 + 1 is the cap: allowed
        with self.assertRaises(ClientError) as e:
            cap.check(fleet, 3, 1, NOW)
        self.assertIn("3 in flight", e.exception.title)
        self.assertIn("main: 2", e.exception.why)
        self.assertIn("max_in_flight", " ".join(e.exception.steps))

    def test_a_batch_counts_as_all_its_jobs(self):
        with self.assertRaises(ClientError):
            cap.check([("main", Repos({"a": 1}))], 3, 3, NOW)

    def test_zero_is_no_cap_and_reads_nothing(self):
        self.assertEqual(cap.check([("main", Repos({"a": 1}, fail=True))], 0, 100, NOW), 0)

    def test_an_unreachable_worker_does_not_block_sending_it_is_counted_from_what_was_last_downloaded(self):
        self.assertEqual(cap.check([("main", Repos({"a": 1}, fail=True))], 5, 1, NOW), 1)


if __name__ == "__main__":
    unittest.main()

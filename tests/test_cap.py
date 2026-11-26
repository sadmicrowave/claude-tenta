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

    def __init__(self, jobs, results=None, fail=False, classes=None):
        self.jobs, self.results, self.fail, self.classes = jobs, results or {}, fail, classes or {}

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
        return json.dumps({"created": stamp(self.jobs[jid]), **({"class": self.classes[jid]} if jid in self.classes else {})})

    def show_files(self, ref, paths):                           # the real one reads them in one git process (tested in test_concurrent_submit); a fake only has to give the same answer
        return {p: self.show_file(ref, p) for p in paths}

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
        self.assertIn("3", str(e.exception.facts))                       # how many are in flight is a labelled row, not part of the title
        self.assertIn("Too many jobs", e.exception.title)
        self.assertEqual(e.exception.facts[-1], ("Code", "placement.capacity.in_flight"))
        self.assertIn("main: 2", e.exception.why)
        self.assertIn("max_in_flight", " ".join(e.exception.steps))

    def test_a_batch_counts_as_all_its_jobs(self):
        with self.assertRaises(ClientError):
            cap.check([("main", Repos({"a": 1}))], 3, 3, NOW)

    def test_zero_is_no_cap_and_reads_nothing(self):
        self.assertEqual(cap.check([("main", Repos({"a": 1}, fail=True))], 0, 100, NOW), 0)

    def test_an_unreachable_worker_does_not_block_sending_it_is_counted_from_what_was_last_downloaded(self):
        self.assertEqual(cap.check([("main", Repos({"a": 1}, fail=True))], 5, 1, NOW), 1)


class Share(unittest.TestCase):
    """limits.max_per_worker and limits.max_per_class: a job over its share is told why (it is queued by the caller), never refused here."""
    FLEET = [("main", Repos({"a": 1, "b": 1, "c": 1}, classes={"a": "big", "b": "big", "c": "small"})), ("studio", Repos({"d": 1}, classes={"d": "big"}))]

    def test_no_limit_reads_nothing(self):
        self.assertIsNone(cap.share_problem([("main", Repos({"a": 1}, fail=True))], "main", "big", 0, 0, NOW))

    def test_a_worker_at_its_limit_is_named_with_the_count_and_the_setting(self):
        why = cap.share_problem(self.FLEET, "main", "other", 3, 0, NOW)
        self.assertIn("main already has 3 in flight", why)
        self.assertIn("limits.max_per_worker", why)
        self.assertIsNone(cap.share_problem(self.FLEET, "studio", "other", 3, 0, NOW))

    def test_a_command_at_its_limit_counts_across_every_worker(self):
        why = cap.share_problem(self.FLEET, "studio", "big", 0, 3, NOW)
        self.assertIn("3 of big", why)
        self.assertIn("limits.max_per_class", why)
        self.assertIsNone(cap.share_problem(self.FLEET, "studio", "small", 0, 3, NOW))      # another command is not held back by the big batch

    def test_jobs_of_the_same_batch_not_yet_visible_count(self):
        self.assertIsNotNone(cap.share_problem(self.FLEET, "studio", "small", 0, 2, NOW, extra_class=1))

    def test_in_flight_detail_reads_the_class(self):
        self.assertEqual(cap.in_flight_detail(self.FLEET[0][1], NOW), {"a": "big", "b": "big", "c": "small"})


if __name__ == "__main__":
    unittest.main()

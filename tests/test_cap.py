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
    """limits.max_per_worker and limits.max_per_class: a job over its share is told why (the caller queues it), never refused here."""
    PER = {"main": {"a": "big", "b": "big", "c": "small"}, "studio": {"d": "big"}}

    def test_a_worker_at_its_limit_is_named_with_the_count_and_the_setting(self):
        kind, why = cap.share_problem(self.PER, "main", "other", 3, 0)
        self.assertEqual(kind, "worker")
        self.assertIn("main already has 3 in flight", why)
        self.assertIn("limits.max_per_worker", why)
        self.assertIsNone(cap.share_problem(self.PER, "studio", "other", 3, 0))

    def test_a_command_at_its_limit_counts_across_every_worker(self):
        kind, why = cap.share_problem(self.PER, "studio", "big", 0, 3)
        self.assertEqual(kind, "class")
        self.assertIn("3 of big", why)
        self.assertIn("limits.max_per_class", why)
        self.assertIsNone(cap.share_problem(self.PER, "studio", "small", 0, 3))      # another command is not held back by the big batch

    def test_jobs_not_yet_visible_and_sends_in_progress_count(self):
        self.assertIsNotNone(cap.share_problem(self.PER, "studio", "small", 0, 2, extra_class=1))
        self.assertIsNotNone(cap.share_problem(self.PER, "studio", "x", 2, 0, reserved=[("studio", "x")]))
        self.assertIsNone(cap.share_problem(self.PER, "studio", "x", 3, 0, reserved=[("main", "x")]))

    def test_in_flight_detail_reads_the_class(self):
        self.assertEqual(cap.in_flight_detail(Repos({"a": 1, "b": 1}, classes={"a": "big", "b": "small"}), NOW), {"a": "big", "b": "small"})

    def test_no_limit_gives_no_checker_and_reads_nothing(self):
        ctx = type("C", (), {"max_per_worker": 0, "max_per_class": 0, "fleet": lambda self: 1 / 0})()
        self.assertIsNone(cap.checker_for(ctx))

    def test_the_checker_looks_at_the_workers_once_however_many_jobs_ask(self):
        import tempfile
        looks = []

        class Fleet:
            max_per_worker, max_per_class, home = 2, 0, tempfile.mkdtemp()

            def fleet(self):
                looks.append(1)
                return [("main", Repos({"a": 1}, classes={"a": "big"}))]
        check = cap.checker_for(Fleet(), NOW)
        for _ in range(5):
            check("main", "big")
        self.assertEqual(len(looks), 1)
        self.assertIsNone(check("main", "big"))
        self.assertEqual(check("main", "big", extra_worker=1)[0], "worker")


if __name__ == "__main__":
    unittest.main()

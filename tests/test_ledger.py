import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401
from ct import ledger

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Usage(unittest.TestCase):
    def test_turns_and_cost_come_from_the_json_claude_printed(self):
        out = json.dumps({"result": "hi", "num_turns": 4, "total_cost_usd": 0.0312, "usage": {"input_tokens": 100, "output_tokens": 20}})
        self.assertEqual(ledger.usage_of(out), {"turns": 4, "cost_usd": 0.0312, "input_tokens": 100, "output_tokens": 20})

    def test_anything_else_is_no_usage_not_an_error(self):
        for bad in ("", "not json", "[1]", json.dumps({"result": "x"}), None):
            self.assertIsNone(ledger.usage_of(bad))


class Daily(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.l = ledger.Ledger(tempfile.mkdtemp(), jobs_per_day=2, turns_per_day=10, clock=self.clock)

    def test_the_second_job_of_a_limit_of_two_is_allowed_and_the_third_is_not(self):
        self.assertIsNone(self.l.exceeded())
        self.l.start("a", "c")
        self.assertIsNone(self.l.exceeded())
        self.l.start("b", "c")
        self.assertIn("2 Claude job", self.l.exceeded())

    def test_a_job_counts_when_it_starts_so_a_burst_cannot_all_slip_under_the_limit(self):
        self.l.start("a", "c"), self.l.start("b", "c")
        self.assertEqual(self.l.today()["jobs"], 2)

    def test_turns_over_the_limit_pause_too_and_cost_is_recorded_never_enforced(self):
        self.l.start("a", "c")
        self.l.finish("a", "c", {"turns": 11, "cost_usd": 99.0}, "ok")
        self.assertIn("10 Claude turn", self.l.exceeded())
        self.assertEqual(self.l.today()["cost_usd"], 99.0)

    def test_a_new_utc_day_starts_clean(self):
        self.l.start("a", "c"), self.l.start("b", "c")
        self.clock.t = T0 + timedelta(days=1)
        self.assertIsNone(self.l.exceeded())
        self.assertEqual(self.l.today()["jobs"], 0)

    def test_zero_means_no_limit(self):
        free = ledger.Ledger(tempfile.mkdtemp(), 0, 0, clock=self.clock)
        for i in range(50):
            free.start(str(i), "c")
        self.assertIsNone(free.exceeded())

    def test_the_summary_never_names_a_job_or_a_class(self):
        self.l.start("secret-job", "secret-class")
        self.assertNotIn("secret", json.dumps(self.l.summary()))

    def test_the_file_is_private_and_a_damaged_line_is_skipped(self):
        self.l.start("a", "c")
        self.assertEqual(self.l.file.stat().st_mode & 0o777, 0o600)
        with open(self.l.file, "a") as f:
            f.write("{broken\n")
        self.assertEqual(self.l.today()["jobs"], 1)

    def test_old_days_are_pruned(self):
        self.l.start("a", "c")
        self.clock.t = T0 + timedelta(days=ledger.KEEP_DAYS + 2)
        self.assertEqual(self.l.prune(), 1)
        self.assertEqual(self.l._rows(), [])


if __name__ == "__main__":
    unittest.main()

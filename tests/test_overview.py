"""The operator's status panel (R6): what it says from the queue, the worker readings and the audit trail, and that its bars and lines fit."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from armada.admin import workers
from armada.client import audit, overview, queue

NOW = 1_800_000_000
DOC = {"updated": "2027-01-15T07:59:00Z", "hardware": {"cores": 4, "mem_total_mb": 8000}, "observed": {"load_1m": 2.0, "mem_available_mb": 4000}, "os": {"id": "debian", "version": "12"}}


class Collect(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def rows(self, **over):
        return [("w1", workers.row_for("w1", {**DOC, **over}, now=NOW))]

    def test_queue_depth_by_priority_high_first_and_the_oldest_wait(self):
        queue.add(self.home, "a-1", "c", "high", {}, "w", now=NOW - 7200)
        queue.add(self.home, "a-2", "c", "low", {}, "w", now=NOW - 60)
        queue.add(self.home, "a-3", "c", "low", {}, "w", now=NOW - 60)
        d = overview.collect(self.home, self.rows(), now=NOW)
        self.assertEqual(list(d["queue"]["depth"].items()), [("high", 1), ("normal", 0), ("low", 2)])
        self.assertEqual((d["queue"]["total"], d["queue"]["oldest"]), (3, "2 h"))

    def test_running_is_placed_without_an_outcome_and_recent_is_the_outcomes_newest_first(self):
        audit.append(self.home, "placed", "j-1", NOW - 600, worker="w1", **{"class": "build"})
        audit.append(self.home, "placed", "j-2", NOW - 500, worker="w1", **{"class": "test"})
        audit.append(self.home, "outcome", "j-1", NOW - 100, status="error")
        audit.append(self.home, "placed", "j-old", NOW - 3 * 86400, worker="w1", **{"class": "old"})          # no outcome, but too old to be running
        d = overview.collect(self.home, self.rows(), now=NOW)
        self.assertEqual([j["job"] for j in d["running"]], ["j-2"])
        self.assertEqual(d["workers"][0]["running"], 1)
        self.assertEqual([(j["job"], j["status"], j["class"], j["worker"]) for j in d["recent"]], [("j-1", "error", "build", "w1")])

    def test_a_worker_state_is_ok_silent_stale_or_missing(self):
        old = overview.collect(self.home, self.rows(updated="2027-01-14T00:00:00Z"), now=NOW)["workers"][0]
        self.assertEqual(old["state"], "SILENT")
        self.assertTrue(old["bad"])
        self.assertEqual(overview.collect(self.home, [("w1", None)], now=NOW)["workers"][0]["state"], "no state")
        self.assertEqual(overview.collect(self.home, self.rows(), now=NOW)["workers"][0]["state"], "ok")

    def test_an_empty_home_draws_every_section_with_words(self):
        text = "\n".join(overview.lines(overview.collect(self.home, [], now=NOW), 40, color=False))
        for word in ("WORKERS", "no workers listed", "nothing waits", "nothing running", "no results seen yet"):
            self.assertIn(word, text)


class Aliases(unittest.TestCase):
    def test_the_audit_trail_names_the_real_worker_and_the_panel_still_counts_it_under_the_alias(self):
        home = Path(tempfile.mkdtemp())
        audit.append(home, "placed", "j-1", NOW - 60, worker="real1", **{"class": "build"})
        with mock.patch("armada.alias.show", side_effect=lambda n: {"real1": "Studio"}.get(n, n)):
            d = overview.collect(home, [("real1", workers.row_for("real1", DOC, now=NOW))], now=NOW)
        self.assertEqual((d["workers"][0]["name"], d["workers"][0]["running"], d["running"][0]["worker"]), ("Studio", 1, "Studio"))


class Bars(unittest.TestCase):
    def test_a_bar_is_exactly_as_wide_as_asked_and_never_empty_for_a_value(self):
        for room in (0, 1, 4, 24):
            for value, total in ((0, 5), (1, 100), (5, 5), (9, 5), (3, 0)):
                self.assertEqual(len(overview.bar(value, total, room)), room)
        self.assertEqual(overview.bar(1, 100, 10).count("█"), 1)
        self.assertEqual(overview.bar(0, 5, 10), "░" * 10)

    def test_no_line_is_wider_than_the_room(self):
        data = overview.collect(Path(tempfile.mkdtemp()), [("a-long-worker-name-" * 3, workers.row_for("x", DOC, now=NOW))], now=NOW)
        data["queue"] = {"depth": {"high": 99, "normal": 1, "low": 0}, "total": 100, "oldest": "3 d"}
        for room in (20, 26, 36, 56, 96):
            for line in overview.lines(data, room, color=False):
                self.assertLessEqual(len(line), room, line)


if __name__ == "__main__":
    unittest.main()

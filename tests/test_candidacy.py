import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from armada import config  # noqa: E402
from armada.client import candidacy  # noqa: E402

F = dict(runs=1, median_s=2.0, pinned=False, class_bound=False, batchable=False, splittable=False)


def judge(**kw):
    return candidacy.judge({**F, **kw}, repeat_runs=3, min_seconds=30.0)


def by_rung(res):
    return {r["rung"]: r for r in res["rungs"]}


class Settings(unittest.TestCase):
    def test_the_two_repeat_settings_exist_with_the_agreed_defaults(self):
        spec = config.SCHEMA["offload"]
        self.assertEqual(spec["repeat_runs"][0], 3)
        self.assertEqual(spec["repeat_days"][0], 14)
        self.assertIsNone(spec["repeat_runs"][1](0)[0])           # at least once
        self.assertEqual(spec["repeat_days"][1](14)[0], 14)
        self.assertIsNone(spec["repeat_days"][1](True)[0])        # a flag is not a number of days


class EveryRungIsRead(unittest.TestCase):
    def test_every_rung_is_reported_even_after_a_miss(self):
        res = judge()
        self.assertEqual(sorted(by_rung(res)), [1, 2, 3, 4])
        self.assertEqual(by_rung(res)[1]["verdict"], "not met")
        self.assertIsNone(res["headline"])

    def test_fewer_tokens_is_never_guessed(self):
        r = by_rung(judge(pinned=True, median_s=600, runs=9))[2]
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("AI layer", r["reason"])

    def test_the_weights_come_from_one_table(self):
        self.assertEqual({k: candidacy.WEIGHTS[k] for k in (1, 2, 3, 4)}, {1: 100, 2: 80, 3: 60, 4: 40})
        self.assertEqual([r["weight"] for r in judge()["rungs"]], [100, 80, 60, 40])


class Headline(unittest.TestCase):
    def test_a_pinned_slow_command_is_headlined_by_the_highest_rung(self):
        res = judge(pinned=True, median_s=120, runs=5)
        self.assertEqual(res["headline"], 1)
        self.assertEqual([r["rung"] for r in res["rungs"] if r["verdict"] == "met"], [1, 4])

    def test_a_class_bound_to_one_worker_meets_rung_one(self):
        self.assertEqual(judge(class_bound=True)["headline"], 1)

    def test_a_batchable_command_meets_rung_three(self):
        self.assertEqual(judge(batchable=True, median_s=1)["headline"], 3)
        self.assertEqual(judge(splittable=True, median_s=1)["headline"], 3)

    def test_a_long_command_meets_rung_four(self):
        res = judge(median_s=45.0)
        self.assertEqual(res["headline"], 4)
        self.assertIn("45", by_rung(res)[4]["reason"])

    def test_a_command_just_under_the_limit_does_not(self):
        self.assertIsNone(judge(median_s=29.9, runs=1)["headline"])


class Repeatable(unittest.TestCase):
    def test_short_runs_that_repeat_enough_count_when_their_total_is_worth_it(self):
        res = judge(median_s=12.0, runs=3)                       # 3 runs, 36 s in all
        self.assertEqual(res["headline"], 4)
        self.assertIn("3", by_rung(res)[4]["reason"])

    def test_the_run_count_is_the_setting_not_a_constant(self):
        self.assertIsNone(candidacy.judge({**F, "median_s": 12.0, "runs": 3}, repeat_runs=4, min_seconds=30.0)["headline"])
        self.assertEqual(candidacy.judge({**F, "median_s": 12.0, "runs": 3}, repeat_runs=2, min_seconds=30.0)["headline"], 4)

    def test_repeats_of_a_trivial_command_do_not(self):
        self.assertIsNone(judge(median_s=0.2, runs=50)["headline"])      # 10 s in all: not worth a trip

    def test_no_timing_is_unknown_not_a_miss(self):
        r = by_rung(judge(median_s=None, runs=9))[4]
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("no timing", r["reason"])


class Window(unittest.TestCase):
    def test_runs_inside_the_window_only(self):
        day = 86400
        now = 100 * day
        recs = [{"ts": now - 20 * day}, {"ts": now - 13 * day}, {"ts": now - 2 * day}, {"ts": now}]
        self.assertEqual(candidacy.runs_in_window(recs, days=14, now=now), 3)
        self.assertEqual(candidacy.runs_in_window(recs, days=1, now=now), 1)
        self.assertEqual(candidacy.runs_in_window([], days=14, now=now), 0)


if __name__ == "__main__":
    unittest.main()

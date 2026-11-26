import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import config  # noqa: E402
from ct.client import candidacy, hostload  # noqa: E402

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
        self.assertEqual(sorted(by_rung(res)), [1, 2, 3, 4, 5])
        self.assertEqual(by_rung(res)[1]["verdict"], "not met")
        self.assertIsNone(res["headline"])

    def test_fewer_tokens_is_never_guessed(self):
        r = by_rung(judge(pinned=True, median_s=600, runs=9))[2]
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("AI layer", r["reason"])

    def test_the_weights_come_from_one_table(self):
        self.assertEqual({k: candidacy.WEIGHTS[k] for k in (1, 2, 3, 4, 5)}, {1: 100, 2: 80, 3: 60, 4: 40, 5: 50})
        self.assertEqual([r["weight"] for r in judge()["rungs"]], [100, 80, 60, 40, 50])


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


def busy(load_ratio, **kw):
    return by_rung(candidacy.judge({**F, "load_ratio": load_ratio, **kw}, repeat_runs=3, min_seconds=30.0, busy_ratio=1.0, busy_min_seconds=10.0))[5]


class BusyHost(unittest.TestCase):
    """Rung 5, "your computer is busy" (weight 50, between Faster and Free your own computer): a live reading, never a stored fact."""

    def test_the_settings_exist_and_are_validated(self):
        spec = config.SCHEMA["offload"]
        self.assertEqual(spec["busy_load_ratio"][0], 1.0)
        self.assertEqual(spec["busy_min_seconds"][0], 10)
        self.assertIsNotNone(spec["busy_load_ratio"][1](0)[1])           # must be positive
        self.assertIsNotNone(spec["busy_load_ratio"][1](500)[1])
        self.assertIsNone(spec["busy_load_ratio"][1](2.5)[1])
        self.assertIsNotNone(spec["busy_load_ratio"][1](True)[1])
        self.assertIsNone(spec["busy_min_seconds"][1](10)[1])

    def test_busy_now_and_a_command_worth_the_trip_meets_it(self):
        r = busy(25.4, median_s=15.0)
        self.assertEqual(r["verdict"], "met")
        self.assertIn("25.4", r["reason"])

    def test_it_can_lift_a_command_no_other_rung_picks_and_outranks_rung_four(self):
        res = candidacy.judge({**F, "load_ratio": 3.0, "median_s": 15.0}, 3, 30.0, 1.0, 10.0)
        self.assertEqual(res["headline"], 5)
        res = candidacy.judge({**F, "load_ratio": 3.0, "median_s": 60.0}, 3, 30.0, 1.0, 10.0)
        self.assertEqual(res["headline"], 5)                              # 50 beats 40
        res = candidacy.judge({**F, "load_ratio": 3.0, "median_s": 60.0, "batchable": True}, 3, 30.0, 1.0, 10.0)
        self.assertEqual(res["headline"], 3)                              # 60 beats 50

    def test_not_busy_is_not_met_and_says_the_reading(self):
        r = busy(0.4, median_s=60.0)
        self.assertEqual(r["verdict"], "not met")
        self.assertIn("0.4", r["reason"])

    def test_a_quick_command_is_not_worth_a_trip_even_when_busy(self):
        r = busy(9.0, median_s=2.0)
        self.assertEqual(r["verdict"], "not met")
        self.assertIn("too quick", r["reason"])

    def test_a_command_that_cannot_run_on_a_worker_never_meets_it(self):
        self.assertEqual(busy(9.0, median_s=60.0, refused=True)["verdict"], "not met")

    def test_no_reading_or_no_timing_cannot_be_judged_never_guessed(self):
        self.assertEqual(busy(None, median_s=60.0)["verdict"], "unknown")
        self.assertIn("cannot be read", busy(None)["reason"])
        self.assertEqual(busy(9.0, median_s=None)["verdict"], "unknown")

    def test_the_threshold_is_the_setting(self):
        met = lambda ratio: by_rung(candidacy.judge({**F, "load_ratio": 1.5, "median_s": 60.0}, 3, 30.0, ratio, 10.0))[5]["verdict"]
        self.assertEqual((met(1.0), met(2.0)), ("met", "not met"))


class HostLoad(unittest.TestCase):
    """The cheap local signal: load average over the last 5 minutes divided by core count."""

    def test_macos_and_linux_use_the_five_minute_average_per_core(self):
        for plat in ("darwin", "linux"):
            got = hostload.read(platform=plat, getloadavg=lambda: (9.0, 203.0, 100.0), cpus=lambda: 8)
            self.assertEqual(got["cores"], 8)
            self.assertAlmostEqual(got["ratio"], 203.0 / 8)
            self.assertEqual(got["load"], 203.0)

    def test_windows_cannot_be_judged(self):
        self.assertIsNone(hostload.read(platform="win32", getloadavg=lambda: (1, 1, 1), cpus=lambda: 4))

    def test_a_failed_read_cannot_be_judged(self):
        def boom():
            raise OSError("no load average")
        self.assertIsNone(hostload.read(platform="linux", getloadavg=boom, cpus=lambda: 4))
        self.assertIsNone(hostload.read(platform="linux", getloadavg=lambda: (1, 1, 1), cpus=lambda: None))
        self.assertIsNone(hostload.read(platform="linux", getloadavg=lambda: (1, 1, 1), cpus=lambda: 0))

    def test_the_real_reader_answers_on_this_machine_or_says_it_cannot(self):
        got = hostload.read()
        self.assertTrue(got is None or got["ratio"] >= 0)


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

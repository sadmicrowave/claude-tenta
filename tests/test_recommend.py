import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct.client import recommend, view  # noqa: E402


def runs(cls, worker, *secs, ok=True):
    return [{"job": f"{cls}-{worker}-{i}", "class": cls, "worker": worker, "secs": s, "ok": ok, "at": 1} for i, s in enumerate(secs)]


class Recommend(unittest.TestCase):
    def test_a_clearly_faster_worker_is_recommended_with_its_numbers(self):
        v, = recommend.recommend(runs("nmap", "a", 100, 110, 90) + runs("nmap", "b", 50, 60, 55), {"nmap": "a"})
        self.assertEqual((v["verdict"], v["worker"], v["gain_pct"], v["median"]), ("better", "b", 45, {"a": 100, "b": 55}))

    def test_a_small_gain_keeps_the_pin(self):
        v, = recommend.recommend(runs("nmap", "a", 100, 100, 100) + runs("nmap", "b", 90, 90, 90), {"nmap": "a"})
        self.assertEqual(v["verdict"], "keep")

    def test_too_few_runs_on_the_other_worker_is_said_not_guessed(self):
        v, = recommend.recommend(runs("nmap", "a", 100, 100, 100) + runs("nmap", "b", 10, 10), {"nmap": "a"})
        self.assertEqual((v["verdict"], v["need"]), ("more_runs", {"b": 2}))

    def test_a_pin_with_no_runs_says_so(self):
        v, = recommend.recommend(runs("nmap", "b", 10, 10, 10), {"nmap": "a"})
        self.assertEqual(v["verdict"], "no_runs")

    def test_failed_runs_never_count_and_the_median_ignores_one_slow_run(self):
        rows = runs("x", "a", 100, 100, 100) + runs("x", "b", 1, 1, 1, ok=False) + runs("x", "b", 95, 96, 900)
        v, = recommend.recommend(rows, {"x": "a"})
        self.assertEqual((v["verdict"], v["median"]["b"]), ("keep", 96))

    def test_unpinned_classes_are_not_judged(self):
        self.assertEqual(recommend.recommend(runs("x", "a", 1, 1, 1), {}), [])


class TheRunLog(unittest.TestCase):
    def res(self, status="ok", cls="nmap", end="2026-01-01T00:01:40Z"):
        return {"class": cls, "status": status, "started": "2026-01-01T00:00:00Z", "finished": end}

    def test_a_finished_job_is_recorded_once_with_its_worker_and_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            log = recommend.RunLog(d)
            now = view.parse_when("2026-01-02T00:00:00Z").timestamp()
            self.assertEqual(log.observe({"j1": self.res()}, lambda j: "w1", view.parse_when, now), 1)
            self.assertEqual(log.observe({"j1": self.res()}, lambda j: "w1", view.parse_when, now), 0)       # seen again: not counted twice
            r, = log.read()
            self.assertEqual((r["job"], r["worker"], r["secs"], r["ok"]), ("j1", "w1", 100, True))
            self.assertEqual(oct(log.path.stat().st_mode & 0o777), "0o600")

    def test_unfinished_unknown_worker_and_old_jobs_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            log = recommend.RunLog(d)
            now = view.parse_when("2026-01-02T00:00:00Z").timestamp()
            got = log.observe({"a": self.res("running"), "b": self.res(), "c": {"class": "x", "status": "ok"}}, lambda j: None if j == "b" else "w", view.parse_when, now)
            self.assertEqual(got, 0)
            self.assertEqual(log.observe({"o": self.res()}, lambda j: "w", view.parse_when, now + 61 * 86400), 0)


if __name__ == "__main__":
    unittest.main()

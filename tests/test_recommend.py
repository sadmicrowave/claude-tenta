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


T = 10 ** 6


class TheHint(unittest.TestCase):
    def better(self, w="b"):
        return recommend.recommend(runs("nmap", "a", 100, 100, 100) + runs("nmap", "b", 50, 50, 50), {"nmap": "a"})

    def test_a_better_worker_is_hinted_once_per_suggestion(self):
        v, st = recommend.hint(self.better(), {}, T)
        self.assertEqual(v["worker"], "b")
        self.assertIsNone(recommend.hint(self.better(), st, T + 99 * 3600)[0])             # same suggestion: not again

    def test_hints_are_rate_limited_and_a_vanished_suggestion_is_forgotten(self):
        _, st = recommend.hint(self.better(), {}, T)
        keep = recommend.recommend(runs("nmap", "a", 100, 100, 100) + runs("nmap", "b", 99, 99, 99), {"nmap": "a"})
        _, st = recommend.hint(keep, st, T + 3600)                                     # inside the window: nothing said, and the old suggestion is dropped
        self.assertEqual(st["said"], {})
        self.assertIsNone(recommend.hint(self.better(), st, T + 3600)[0])                  # back, but inside the rate limit
        self.assertIsNotNone(recommend.hint(self.better(), st, T + 25 * 3600)[0])

    def test_nothing_better_says_nothing(self):
        keep = recommend.recommend(runs("nmap", "a", 100, 100, 100), {"nmap": "a"})
        self.assertEqual(recommend.hint(keep, {}, T)[0], None)

    def test_a_broken_state_file_is_just_no_history(self):
        with tempfile.TemporaryDirectory() as d:
            st = recommend.HintState(d)
            st.path.write_text("{not json")
            self.assertEqual(st.read(), {})
            st.write({"at": 1, "said": {}})
            self.assertEqual(oct(st.path.stat().st_mode & 0o777), "0o600")


class TheHintLine(unittest.TestCase):
    def ctx(self, d, on=True):
        from ct.client import cli
        return cli.Context(None, home=d, clock=lambda: 10 ** 9, recommend_hint=on)

    def run_hint(self, d, on=True):
        import contextlib
        import io
        from unittest import mock
        from ct.client import cli, here
        recommend.RunLog(d).path.write_text("".join(__import__("json").dumps(r) + "\n" for r in runs("nmap", "a", 100, 100, 100) + runs("nmap", "b", 50, 50, 50)))
        pins = mock.Mock(commands={"nmap": "a"})
        err = io.StringIO()
        with mock.patch.object(here, "global_pins", return_value=pins), mock.patch.object(here, "pins_for", return_value=None), contextlib.redirect_stderr(err):
            cli.show_hint(self.ctx(d, on))
        return err.getvalue()

    def test_the_line_names_the_command_the_worker_and_the_off_switch_then_stays_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_hint(d)
            self.assertIn("faster worker", out)
            self.assertIn("offload.recommend_hint = false", out)
            self.assertEqual(self.run_hint(d), "")

    def test_the_off_switch_silences_it(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hint(d, on=False), "")


if __name__ == "__main__":
    unittest.main()

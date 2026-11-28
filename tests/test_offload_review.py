"""The offload review summary (slice 6 of theme/offload-value): what the person's own command log says a worker could take, ranked by the ladder.
`CommandLog.review` is pure (every number it reads is passed in: time, load, settings); `ct-run offload review` and the end of `ct-setup install` print it."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import config, refusals  # noqa: E402
from ct.client import cli, cmdlog  # noqa: E402
from ct.client.repo import ClientRepos  # noqa: E402
from ct.setup import cli as setup_cli  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
IDLE = lambda: {"load": 0.1, "cores": 4, "ratio": 0.03}      # a computer that is not busy: output never depends on the machine running the tests
OWN = ("ct-run", "homect")


def rec(cmd, secs, days_ago, n=0):
    return {"ts": int(NOW - days_ago * DAY + n), "cmd": cmd, "secs": secs, "cwd": "/x"}


def suite_runs(n, secs=60.0, days_ago=2, cmd="cd /work/proj && scripts/t test_a test_b | tail -5"):
    """The shape the real log has: a compound line whose one real command is the script."""
    return [rec(cmd, secs, days_ago, i) for i in range(n)]


def filler(n, days_ago=5):
    return [rec(f"ls dir{i}", 0.1, days_ago, i) for i in range(n)]


class Review(unittest.TestCase):
    def setUp(self):
        self.log = cmdlog.CommandLog(tempfile.mkdtemp(), own_tools=OWN)

    def review(self, recs, **kw):
        return self.log.review(recs, now=NOW, **kw)

    def test_an_empty_log_says_so_and_claims_nothing(self):
        r = self.review([])
        self.assertEqual((r["state"], r["recs"], r["seconds"]), ("empty", [], 0))

    def test_a_short_log_shows_no_hours(self):
        r = self.review(suite_runs(5, days_ago=1))
        self.assertEqual(r["state"], "short")
        self.assertEqual((r["commands_recorded"], r["seconds"], r["recs"]), (5, 0, []))

    def test_a_log_of_only_trivial_commands_has_no_candidates_and_says_why(self):
        r = self.review(filler(40))
        self.assertEqual(r["state"], "none")
        self.assertEqual(r["commands_recorded"], 40)
        self.assertTrue(r["reasons"])
        self.assertIn("trivial shell plumbing", [why for why, _ in r["reasons"]])

    def test_compound_commands_count_as_the_real_command_and_their_seconds_add_up(self):
        r = self.review(suite_runs(10) + filler(20))
        self.assertEqual(r["state"], "ok")
        (top,) = r["recs"]
        self.assertEqual((top["shape"], top["count"], top["total"], top["median"]), ("scripts/t", 10, 600.0, 60.0))
        self.assertEqual((r["commands"], r["seconds"], r["commands_recorded"]), (10, 600.0, 30))

    def test_only_the_window_counts(self):
        old = suite_runs(10, days_ago=20)
        r = self.review(old + suite_runs(5, days_ago=3) + filler(20), days=14)
        self.assertEqual((r["state"], r["commands"], r["seconds"]), ("ok", 5, 300.0))

    def test_the_window_shrinks_to_the_log_when_the_log_is_younger(self):
        r = self.review(suite_runs(25, days_ago=6))
        self.assertEqual((r["state"], r["window_days"]), ("ok", 6))
        r = self.review(suite_runs(25, days_ago=40) + suite_runs(1, days_ago=1), days=14)
        self.assertEqual(r["window_days"], 14)

    def test_untimed_commands_are_counted_not_guessed(self):
        recs = suite_runs(8) + [rec("cd /w && scripts/t x", None, 2, 99), rec("cd /w && scripts/t y", None, 2, 98)] + filler(20)
        r = self.review(recs)
        self.assertEqual((r["seconds"], r["untimed"]), (480.0, 2))

    def test_the_ladder_decides_the_order_before_the_hours(self):
        """A busy computer lifts a single 20 s command (rung 5, weight 50) above a command that repeats 20 times at 2 s (rung 4, weight 40) that has more seconds in all."""
        recs = suite_runs(20, secs=2.0, cmd="cd /w && lint-all | tail -3") + [rec("cd /w && cargo-nextest run", 20.0, 2, 77)] + filler(20)
        calm = self.review(recs, load_ratio=0.1)
        busy = self.review(recs, load_ratio=3.0)
        self.assertEqual([x["shape"] for x in calm["recs"]], ["lint-all"])
        self.assertEqual([x["shape"] for x in busy["recs"]], ["cargo-nextest", "lint-all"])
        self.assertEqual((busy["recs"][0]["rung"], busy["recs"][1]["rung"]), (5, 4))
        self.assertEqual(calm["recs"][0]["headline"], "Free your own computer")

    def test_the_list_is_cut_to_top_and_says_how_many_more(self):
        recs = filler(20)
        for i in range(7):
            recs += suite_runs(3, secs=100.0 + i, cmd=f"cd /w && tool{i} --run | tail -1")
        r = self.review(recs, top=5)
        self.assertEqual((len(r["recs"]), r["more"]), (5, 2))
        self.assertEqual((r["commands"], r["kinds"]), (21, 7))            # the totals cover every candidate, not just the five shown

    def test_each_entry_carries_only_what_slice_7_needs_and_no_paths_or_values(self):
        r = self.review(suite_runs(10, cmd="cd /srv/example/private-project && scripts/t test_a --seed-value=1 | tail -5") + filler(20))
        (e,) = r["recs"]
        self.assertEqual(set(e), {"shape", "example", "count", "median", "total", "untimed", "imported", "rung", "headline", "why"})
        self.assertNotIn("private-project", json.dumps(e["shape"]))

    def test_the_settings_exist_with_the_agreed_defaults(self):
        spec = config.SCHEMA["offload"]
        self.assertEqual([spec[k][0] for k in ("review_days", "review_top", "review_min_commands", "review_min_days")], [14, 5, 20, 3])


class Screens(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=OWN, home=self.home, host_load=IDLE, clock=lambda: NOW)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()
        self.env = mock.patch.dict(os.environ, {"CT_COLOR": "never"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def put(self, recs):
        (self.home / "command-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx, cfg=self.cfg)
        return code, out.getvalue(), err.getvalue()

    def test_the_command_exists_and_is_listed_in_the_help(self):
        code, out, err = self.run_cli("offload")
        self.assertIn("offload review", out + err)

    def test_the_summary_says_how_many_commands_and_how_long_without_promising_speed(self):
        self.put(suite_runs(10) + filler(20))
        code, out, err = self.run_cli("offload", "review")
        text = out + err
        self.assertEqual(code, 0)
        self.assertIn("1 different command", text)
        self.assertIn("10 runs", text)
        self.assertIn("10 min", text)
        self.assertIn("scripts/t", text)
        self.assertIn("Free your own computer", text)
        self.assertNotIn("saved", text.lower())
        self.assertNotIn("faster", text.lower())
        self.assertIn("not a promise", text)

    def test_the_second_card_says_what_the_first_counts_in_the_same_units(self):
        recs = filler(20)
        for i in range(7):
            recs += suite_runs(3, secs=100.0, cmd=f"cd /w && tool{i} --run | tail -1")
        self.put(recs)
        code, out, err = self.run_cli("offload", "review")
        text = out + err
        self.assertIn("7 different commands", text)
        self.assertIn("21 runs", text)
        self.assertIn("Together 15 runs", text)
        self.assertIn("The other 2 ran 6 times", text)
        self.assertIn("OFFLOAD REASON", text.upper())
        self.assertNotIn("WHY IT PAYS", text.upper())

    def test_untimed_runs_are_mentioned(self):
        self.put(suite_runs(8) + [rec("cd /w && scripts/t x", None, 2, 99)] + filler(20))
        code, out, err = self.run_cli("offload", "review")
        self.assertIn("1 of those runs had no timing", out + err)

    def test_empty_log_points_at_the_import(self):
        code, out, err = self.run_cli("offload", "review")
        self.assertEqual(code, 0)
        self.assertIn("Nothing recorded yet", out + err)
        self.assertIn("offload import", out + err)

    def test_short_log_gives_no_hours(self):
        self.put(suite_runs(5, days_ago=1))
        code, out, err = self.run_cli("offload", "review")
        self.assertEqual(code, 0)
        self.assertIn("Only 5 commands", out + err)
        self.assertNotIn(" h ", out + err)
        self.assertNotIn("min in all", out + err)

    def test_trivial_log_says_none_could_go(self):
        self.put(filler(40))
        code, out, err = self.run_cli("offload", "review")
        self.assertEqual(code, 0)
        self.assertIn("none", (out + err).lower())
        self.assertIn("trivial shell plumbing", out + err)

    def test_an_unreadable_log_says_what_the_system_said(self):
        self.put(suite_runs(10))
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            code, out, err = self.run_cli("offload", "review")
        self.assertEqual(code, 2)
        self.assertIn("Permission denied", out + err)
        self.assertIn("review.log.unreadable", out + err)

    def test_the_refusal_is_in_the_catalog_with_steps(self):
        e = refusals.CATALOG["review.log.unreadable"]
        self.assertEqual(e.who, "client")
        self.assertTrue(e.steps)

    def test_after_install_the_card_shows_only_on_a_terminal_and_never_raises(self):
        self.put(suite_runs(10) + filler(20))
        tty, pipe = io.StringIO(), io.StringIO()
        tty.isatty = lambda: True
        pipe.isatty = lambda: False
        cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})(),
                             "offload": type("O", (), {"repeat_runs": 3, "repeat_days": 14, "busy_load_ratio": 1.0, "busy_min_seconds": 10, "review_days": 14, "review_top": 5, "review_min_commands": 20, "review_min_days": 3})()})()
        self.assertFalse(cli.after_install_review(cfg, stream=pipe, host_load=IDLE, now=NOW))
        self.assertEqual(pipe.getvalue(), "")
        self.assertTrue(cli.after_install_review(cfg, stream=tty, host_load=IDLE, now=NOW, ask=lambda p: "n"))
        self.assertIn("10 runs", tty.getvalue())
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            tty2 = io.StringIO()
            tty2.isatty = lambda: True
            self.assertTrue(cli.after_install_review(cfg, stream=tty2, host_load=IDLE, now=NOW))      # a note, not an exception
        self.assertIn("Permission denied", tty2.getvalue())

    def test_the_install_finish_card_calls_it_but_a_preview_and_a_failure_do_not(self):
        with mock.patch("ct.client.cli.after_install_review") as m, mock.patch("ct.config.load", return_value=object()), redirect_stderr(io.StringIO()):
            setup_cli.finish(None, [], True)
            setup_cli.finish(None, [("a job went through the whole loop", "failed", "")], False)
            self.assertEqual(m.call_count, 0)
            self.assertEqual(setup_cli.finish(None, [], False), 0)
            self.assertEqual(m.call_count, 1)

    def test_an_install_still_succeeds_when_the_summary_breaks(self):
        with mock.patch("ct.client.cli.after_install_review", side_effect=RuntimeError("boom")), mock.patch("ct.config.load", return_value=object()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(setup_cli.finish(None, [], False), 0)
        self.assertIn("boom", err.getvalue())


if __name__ == "__main__":
    unittest.main()

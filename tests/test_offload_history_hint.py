"""The usage-history hint (slice 8 of theme/offload-value): after a ct-run job command, one rate-limited line when the command log (recorded and imported) holds commands with an offload
reason, pointing at `ct-run offload review`. Same rules as the recommend hint: default on, an off switch, never fails the command; plus the review's remembered answers silence it."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import config, ui  # noqa: E402
from ct.client import cli, cmdlog, here, histhint, recommend, review_flow  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
IDLE = lambda: {"load": 0.1, "cores": 4, "ratio": 0.03}
OWN = ("ct-run", "homect")


def rec(cmd, secs, days_ago=2, n=0, **extra):
    return {"ts": int(NOW - days_ago * DAY + n), "cmd": cmd, "secs": secs, "cwd": "/x", **extra}


def suite(n=10, secs=60.0, cmd="cd /w && scripts/t test_a | tail -5"):
    return [rec(cmd, secs, 2, i) for i in range(n)]


def filler(n=20):
    return [rec(f"ls d{i}", 0.1, 5, i) for i in range(n)]


def write_log(home, rows):
    p = cmdlog.CommandLog(home, own_tools=OWN).path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def review_of(rows):
    return cmdlog.CommandLog(tempfile.mkdtemp(), own_tools=OWN).review(rows, now=NOW)


class Answers:
    def __init__(self, said=None):
        self.said = said or {}

    def silenced(self, shape, days):
        return self.said.get(shape)


class Pick(unittest.TestCase):
    def pick(self, review, state=None, answers=None, hours=24, now=NOW):
        return histhint.pick(review, answers or Answers(), state or {}, now, hours, OWN, 30)

    def test_a_log_with_an_offload_reason_gives_one_line_of_facts(self):
        facts, new = self.pick(review_of(suite() + filler()))
        self.assertEqual((facts["kinds"], facts["seconds"], facts["untimed"]), (1, 600.0, 0))
        self.assertEqual(new["at"], NOW)

    def test_nothing_is_said_for_an_empty_short_or_candidate_free_log(self):
        for rows in ([], suite(5), filler(40)):
            self.assertIsNone(self.pick(review_of(rows))[0], rows[:1])

    def test_at_most_one_line_per_interval_then_again_only_when_the_set_changes(self):
        r = review_of(suite() + filler())
        _, st = self.pick(r)
        self.assertIsNone(self.pick(r, st, now=NOW + 3600)[0])                              # inside the interval
        self.assertIsNone(self.pick(r, st, now=NOW + 3 * DAY)[0])                           # past it, but the same set was already said
        more = review_of(suite() + [rec("scripts/e2e-matrix all", 120, 2, i) for i in range(5)] + filler())
        self.assertEqual(self.pick(more, st, now=NOW + 3 * DAY)[0]["kinds"], 2)             # a new command: said again

    def test_a_set_that_went_away_is_forgotten_so_its_return_is_said_again(self):
        r = review_of(suite() + filler())
        _, st = self.pick(r)
        _, st = self.pick(review_of(filler(40)), st, now=NOW + DAY)
        self.assertIsNotNone(self.pick(r, st, now=NOW + 2 * DAY)[0])

    def test_commands_answered_not_now_or_never_do_not_count(self):
        r = review_of(suite() + filler())
        for said in ("never", "not-now"):
            facts, _ = self.pick(r, answers=Answers({"scripts/t": said}))
            self.assertIsNone(facts, said)

    def test_untimed_runs_add_no_time_and_are_counted_not_guessed(self):
        r = review_of([rec("scripts/t a", None, 2, i, src="zsh") for i in range(30)])
        facts, _ = self.pick(r)
        if facts is not None:                                                               # whatever the ladder decides, no hours may come from rows with no timing
            self.assertEqual(facts["seconds"], 0)


REG = {"classes": {"t": {"run": ["scripts/t", "{a}"], "cost": "light", "tier": "A"}}}
EXCL = [{"argv": ["scripts/t", "test_a"], "reasons": ["too big"]}]


class Settled(unittest.TestCase):
    def left(self, views):
        facts, _ = histhint.pick(review_of(suite() + filler()), Answers(), {}, NOW, 24, OWN, 30, views)
        return facts

    def test_a_command_every_worker_has_registered_is_not_counted(self):
        self.assertIsNone(self.left([(REG, []), (REG, [])]))

    def test_a_worker_that_lacks_it_keeps_the_command_in(self):
        self.assertEqual(self.left([(REG, []), ({"classes": {}}, [])])["kinds"], 1)

    def test_tried_and_excluded_counts_as_answered(self):
        self.assertIsNone(self.left([(REG, []), ({"classes": {}}, EXCL)]))

    def test_a_worker_that_published_nothing_is_unknown_and_keeps_it_in(self):
        self.assertEqual(self.left([(REG, []), (None, [])])["kinds"], 1)
        self.assertEqual(self.left([])["kinds"], 1)

    def test_the_hint_reads_the_local_copies_when_it_runs(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            err = io.StringIO()
            with mock.patch.object(here, "global_pins", return_value=None), mock.patch.object(here, "pins_for", return_value=None), \
                    mock.patch.object(cli, "local_views", return_value=[(REG, [])]), contextlib.redirect_stderr(err):
                cli.show_hint(cli.Context(None, home=d, clock=lambda: NOW, own_tools=OWN, host_load=IDLE))
            self.assertEqual(err.getvalue(), "")


class Shown(unittest.TestCase):
    def ctx(self, home, **kw):
        return cli.Context(None, home=home, clock=lambda: NOW, own_tools=OWN, host_load=IDLE, **kw)

    def run_hint(self, home, **kw):
        err = io.StringIO()
        with mock.patch.object(here, "global_pins", return_value=None), mock.patch.object(here, "pins_for", return_value=None), contextlib.redirect_stderr(err):
            cli.show_hint(self.ctx(home, **kw))
        return err.getvalue()

    def test_the_line_names_the_review_and_the_off_switch_then_stays_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            out = self.run_hint(d)
            self.assertIn("offload reason", out)
            self.assertIn("ct-run offload review", out)
            self.assertIn("offload.history_hint = false", out)
            self.assertNotIn("pays", out)
            self.assertEqual(self.run_hint(d), "")

    def test_the_off_switch_silences_it_and_reads_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            self.assertEqual(self.run_hint(d, history_hint=False), "")
            self.assertFalse(histhint.State(d).path.exists())

    def test_the_interval_is_a_setting(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            self.run_hint(d, history_hint_hours=1)
            write_log(d, suite() + [rec("scripts/e2e-matrix all", 120, 2, i) for i in range(5)] + filler())
            self.assertEqual(self.run_hint(d, history_hint_hours=1), "")                    # same clock: inside the hour

    def test_a_remembered_answer_from_the_review_silences_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            review_flow.Answers(d, clock=lambda: NOW).decline("scripts/t", "never")
            self.assertEqual(self.run_hint(d), "")

    def test_the_faster_worker_hint_goes_first_and_only_one_line_is_shown(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            with mock.patch.object(cli, "_recommend_hint", return_value=True):
                self.assertEqual(self.run_hint(d), "")
            self.assertIn("offload reason", self.run_hint(d))

    def test_an_unreadable_log_or_state_never_breaks_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            p = cmdlog.CommandLog(d, own_tools=OWN).path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("not json\n\x00")
            self.assertEqual(self.run_hint(d), "")
            (Path(d) / histhint.STATE_FILE).write_text("[1")
            self.assertEqual(self.run_hint(d), "")

    def test_the_log_is_read_at_most_once_an_hour(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, filler(40))
            with mock.patch.object(cli, "review_of", wraps=cli.review_of) as seen:
                self.run_hint(d)
                self.run_hint(d)
            self.assertEqual(seen.call_count, 1)

    def test_having_looked_at_the_review_counts_as_seen(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            r = review_of(suite() + filler())
            histhint.mark_seen(d, r, review_flow.Answers(d, clock=lambda: NOW), NOW, OWN, 30)
            self.assertEqual(self.run_hint(d), "")

    def test_imported_untimed_runs_are_counted_as_not_timed_and_add_no_hours(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, [rec("scripts/t a", None, 2, i, src="zsh") for i in range(30)] + suite(3, 600.0) + filler())
            out = self.run_hint(d)
            self.assertIn("30 runs not timed", out)
            self.assertIn("30 min", out)                                                    # only the 3 timed runs (3 x 600 s) make up the time

    def test_the_state_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, suite() + filler())
            self.run_hint(d)
            self.assertEqual(histhint.State(d).path.stat().st_mode & 0o777, 0o600)


class Settings(unittest.TestCase):
    def test_default_on_with_a_day_between_lines(self):
        o = config.load().offload
        self.assertEqual((o.history_hint, o.history_hint_hours), (True, 24))


if __name__ == "__main__":
    unittest.main()

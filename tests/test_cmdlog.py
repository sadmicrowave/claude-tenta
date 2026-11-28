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
from armada.client import cli, cmdlog  # noqa: E402
from armada.client.repo import ClientRepos  # noqa: E402


def ev(cmd, tid="t1", tool="Bash"):
    return json.dumps({"tool_name": tool, "tool_use_id": tid, "session_id": "s", "tool_input": {"command": cmd}, "cwd": "/x"})


class Log(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.log = cmdlog.CommandLog(self.home, own_tools=("arm-run", "homect"))

    def run_cmd(self, cmd, secs, tid):
        self.log.hook("pre", ev(cmd, tid), now=1000.0)
        self.log.hook("post", ev(cmd, tid), now=1000.0 + secs)

    def test_the_hook_records_the_duration_privately(self):
        self.run_cmd("python3 -m unittest discover tests", 42.5, "a")
        (rec,) = self.log.read()
        self.assertEqual((rec["secs"], rec["cwd"]), (42.5, "/x"))
        self.assertEqual(oct(self.log.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(list(self.log.pending.iterdir()), [])                           # the start marker is cleaned up

    def test_secrets_and_other_tools_are_never_logged(self):
        self.run_cmd("curl -H 'Authorization: Bearer abc' https://x", 1, "a")
        self.run_cmd("export GITHUB_TOKEN=abc && make", 1, "b")
        self.run_cmd("echo password=hunter2", 1, "c")
        self.log.hook("post", ev("ls", "d", tool="Read"), now=5)
        self.log.hook("post", ev("   ", "e"), now=5)
        self.assertEqual(self.log.read(), [])

    def test_bad_input_never_raises(self):
        for raw in ("", "not json", "[1, 2]", "null", json.dumps({"tool_name": "Bash"}), json.dumps({"tool_name": "Bash", "tool_input": None})):
            self.log.hook("post", raw)
        self.assertEqual(self.log.read(), [])

    def test_a_command_with_no_start_event_has_no_duration_and_old_starts_are_swept(self):
        self.log.hook("post", ev("make slow-thing", "z"), now=5)
        self.assertEqual(self.log.read()[0]["secs"], None)
        self.log.hook("pre", ev("make a", "old"), now=1000.0)
        old = self.log.pending / self.log.key_of(json.loads(ev("make a", "old")))
        os.utime(old, (1.0, 1.0))
        self.log.hook("post", ev("make b", "new"), now=time_now())
        self.assertFalse(old.exists())

    def test_the_shape_drops_paths_and_values(self):
        self.assertEqual(cmdlog.shape("FOO=1 timeout 60 python3 -m pytest -q tests/a.py --maxfail=2")[1], "python3 -m pytest --maxfail -q")
        self.assertEqual(cmdlog.shape("npm run build -- --prod")[1], "npm run --prod")
        self.assertEqual(cmdlog.shape("python3 tools/gen.py --out=/tmp/x")[1], "python3 gen.py --out")
        self.assertIsNone(cmdlog.shape(""))
        self.assertIsNone(cmdlog.shape("FOO=1"))

    def test_classify(self):
        self.assertEqual(cmdlog.classify("git push origin main")[2], "network / version control")
        self.assertIn("compound", cmdlog.classify("make test && make lint")[2])
        self.assertIsNone(cmdlog.classify("python3 -m unittest discover tests")[2])
        self.assertIsNotNone(cmdlog.classify("cd /tmp && git status")[2])
        self.assertEqual(cmdlog.classify("npm test")[2], "package manager / infrastructure")
        self.assertEqual(cmdlog.classify("sudo make install")[2], "privileged / process control")
        self.assertIsNone(cmdlog.classify("   "))

    def test_this_installs_own_commands_are_never_candidates(self):
        self.assertEqual(self.log.rank(records=[{"cmd": "arm-run submit echo", "secs": 99}] * 3), [])
        self.assertEqual(cmdlog.classify("homect status", own_tools=("homect",))[2], cmdlog.OWN)
        self.assertEqual(cmdlog.classify("arm-run jobs", own_tools=("arm-run",))[2], cmdlog.OWN)

    def test_ranking_by_count_with_the_filters(self):
        for i in range(3):
            self.run_cmd("bash tests/e2e.sh", 90, f"e{i}")
        for i in range(5):
            self.run_cmd("python3 -m unittest discover tests", 40, f"u{i}")
        for i in range(9):
            self.run_cmd("python3 -m json.tool x.json", 0.1, f"q{i}")                  # frequent but quick
        for i in range(7):
            self.run_cmd("git fetch origin", 40, f"g{i}")                              # frequent, slow, but network
        rows = self.log.rank()
        self.assertEqual([r["shape"] for r in rows], ["python3 -m unittest", "bash e2e.sh"])
        self.assertEqual((rows[0]["count"], rows[0]["median"]), (5, 40))
        everything = {r["shape"]: r["reason"] for r in self.log.rank(show_all=True)}
        self.assertIn("too quick", everything["python3 -m json.tool"])
        self.assertEqual(everything["git fetch"], "network / version control")
        self.assertEqual([r["shape"] for r in self.log.rank(min_seconds=60)], ["bash e2e.sh"])

    def test_no_timing_is_not_a_candidate_and_a_compound_command_is_not_timed_per_part(self):
        self.log.hook("post", ev("make slow-thing", "z"), now=5)
        self.assertEqual(self.log.rank(), [])
        self.assertEqual(self.log.rank(show_all=True)[0]["reason"], "no timing recorded yet")

    def test_clear_forgets_everything(self):
        self.run_cmd("make big", 50, "a")
        self.log.clear()
        self.assertEqual(self.log.read(), [])
        self.log.clear()                                                                  # clearing nothing is fine

    def test_unreadable_lines_are_skipped(self):
        self.run_cmd("make big", 50, "a")
        with open(self.log.path, "a") as f:
            f.write("not json\n")
        self.assertEqual(len(self.log.read()), 1)


def time_now():
    import time
    return time.time()


class Command(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("arm-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()
        self.env = mock.patch.dict(os.environ, {"ARM_COLOR": "never"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = cli.main(list(args), ctx=self.ctx, cfg=self.cfg)
        return code, out.getvalue(), err.getvalue()

    def test_the_hook_is_silent_and_always_succeeds(self):
        code, out, err = self.run_cli("offload", "log", "hook", "pre", stdin=ev("make big"))
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, err = self.run_cli("offload", "log", "hook", "post", stdin="garbage")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_show_top_and_clear(self):
        code, out, err = self.run_cli("offload", "log", "show")
        self.assertIn("0 commands recorded", out + err)
        self.assertIn("(not created yet)", out + err)
        code, out, err = self.run_cli("offload", "log", "top")
        self.assertIn("Nothing recorded yet", out + err)
        log = cmdlog.CommandLog(self.home)
        for i in range(4):
            log.hook("pre", ev("python3 -m unittest discover tests", f"t{i}"), now=1000.0)
            log.hook("post", ev("python3 -m unittest discover tests", f"t{i}"), now=1060.0)
        code, out, err = self.run_cli("offload", "log", "top")
        self.assertEqual(code, 0)
        for needle in ("Candidates for offloading", "For homect; median time at least 30 s, or 3 runs in 14 days.", "From 4 commands recorded on this computer.", "python3 -m unittest",
                       "Most recent example of each", "offload fit -- <the example command>"):
            self.assertIn(needle, out + err)
        self.assertRegex(out, r"1\s+4\s+60s\s+240s\s+python3 -m unittest")
        code, out, err = self.run_cli("offload", "log", "top", "--min-seconds", "120")
        self.assertIn("No candidates yet", out + err)
        code, out, err = self.run_cli("offload", "log", "top", "--all")
        self.assertIn("All command shapes", out)
        self.assertIn("4 commands recorded", self.run_cli("offload", "log", "show")[1] + self.run_cli("offload", "log", "show")[2])
        code, out, err = self.run_cli("offload", "log", "clear")
        self.assertIn("Log cleared", out + err)
        self.assertFalse(log.path.exists())

    def test_the_group_without_a_command_says_where_to_start(self):
        for args in (["offload"], ["offload", "log"]):
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 0)
            self.assertIn("offload log top", out + err)

    def test_top_limits_the_list(self):
        log = cmdlog.CommandLog(self.home)
        for tool in ("aa", "bb", "cc"):
            for i in range(2):
                log.hook("pre", ev(f"{tool} --go", f"{tool}{i}"), now=1000.0)
                log.hook("post", ev(f"{tool} --go", f"{tool}{i}"), now=1100.0)
        code, out, err = self.run_cli("offload", "log", "top", "--top", "2")
        table = out.split("Most recent example of each")[0]
        self.assertEqual(sum(1 for l in table.splitlines() if "--go" in l), 2)


class Repeats(unittest.TestCase):
    """Item 8: repeated short work is a candidate too, counted over the person's own window (offload.repeat_runs / repeat_days)."""

    def setUp(self):
        self.log = cmdlog.CommandLog(Path(tempfile.mkdtemp()))
        self.now = time_now()

    def recs(self, n, secs, age_days=0):
        return [{"ts": int(self.now - age_days * 86400 - i), "cmd": "make big", "secs": secs} for i in range(n)]

    def test_three_short_runs_in_the_window_are_a_candidate_with_the_reason_named(self):
        rows = self.log.rank(records=self.recs(3, 12), repeat_runs=3, repeat_days=14, now=self.now)
        self.assertEqual([r["shape"] for r in rows], ["make big"])
        self.assertEqual(rows[0]["headline"], "Free your own computer")
        self.assertEqual([r["rung"] for r in rows[0]["rungs"]], [1, 2, 3, 4])

    def test_the_two_settings_decide(self):
        self.assertEqual(self.log.rank(records=self.recs(3, 12), repeat_runs=4, repeat_days=14, now=self.now), [])
        old = self.recs(3, 12, age_days=20)
        self.assertEqual(self.log.rank(records=old, repeat_runs=3, repeat_days=14, now=self.now), [])
        self.assertEqual(len(self.log.rank(records=old, repeat_runs=3, repeat_days=30, now=self.now)), 1)

    def test_a_long_run_needs_no_repeat(self):
        self.assertEqual(len(self.log.rank(records=self.recs(1, 90), now=self.now)), 1)

    def test_show_all_keeps_the_reason_for_a_miss(self):
        row = self.log.rank(records=self.recs(2, 12), show_all=True, repeat_runs=3, now=self.now)[0]
        self.assertIsNone(row["headline"])
        self.assertIn("too quick", row["reason"])


class RungsShown(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("arm-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()
        log = cmdlog.CommandLog(self.home)
        for i in range(3):
            log.hook("pre", ev("make big", f"m{i}"), now=1000.0)
            log.hook("post", ev("make big", f"m{i}"), now=1060.0)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"ARM_COLOR": "never"}), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx, cfg=self.cfg)
        return code, out.getvalue(), err.getvalue()

    def test_top_says_why_each_candidate_pays(self):
        code, out, err = self.run_cli("offload", "log", "top")
        self.assertIn("Free your own computer", out + err)

    def test_all_shows_every_rung_for_each_shape(self):
        code, out, err = self.run_cli("offload", "log", "top", "--all")
        text = out + err
        for needle in ("Every rung", "Worker-specific work", "Fewer AI tokens", "Faster", "Free your own computer", "needs the AI layer"):
            self.assertIn(needle.lower(), text.lower())


DAY = 86400


class Measure(unittest.TestCase):
    """Item 8, slice 3: the repeat settings suggested from the person's own log, never invented."""

    def setUp(self):
        self.log = cmdlog.CommandLog(Path(tempfile.mkdtemp()))
        self.now = time_now()

    def runs(self, cmd, gap_days, n=3, secs=12):
        return [{"ts": int(self.now - i * gap_days * DAY), "cmd": cmd, "secs": secs} for i in range(n)]

    def test_the_suggestion_is_the_typical_time_to_repeat_a_command(self):
        recs = self.runs("make big", 1) + self.runs("make lint", 3)           # 3 runs take 2 days and 6 days: typical 4
        m = self.log.measure(records=recs, repeat_runs=3, now=self.now)
        self.assertEqual(m["suggested_days"], 4)
        self.assertEqual(m["shapes_enough"], 2)

    def test_the_grid_counts_candidates_at_each_pair_of_values(self):
        recs = self.runs("make big", 1) + self.runs("make lint", 3)
        grid = self.log.measure(records=recs, repeat_runs=3, now=self.now)["grid"]
        self.assertEqual(grid[3][7], 2)
        self.assertEqual(grid[4][7], 0)                                       # nobody ran anything four times
        self.assertEqual(grid[2][7], 2)
        self.assertEqual(sorted(grid), [2, 3, 4])
        self.assertEqual(sorted(grid[3]), [7, 14, 30])

    def test_too_little_data_suggests_nothing_and_says_what_is_missing(self):
        m = self.log.measure(records=self.runs("make big", 1, n=2), repeat_runs=3, now=self.now)
        self.assertIsNone(m["suggested_days"])
        self.assertEqual(m["most_runs"], 2)
        self.assertEqual(self.log.measure(records=[], repeat_runs=3, now=self.now)["suggested_days"], None)

    def test_a_command_that_is_not_a_candidate_kind_is_left_out(self):
        recs = self.runs("git fetch origin", 1)
        self.assertEqual(self.log.measure(records=recs, repeat_runs=3, now=self.now)["shapes_enough"], 0)


class MeasureCommand(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("arm-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"ARM_COLOR": "never"}), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx, cfg=self.cfg)
        return code, out.getvalue(), err.getvalue()

    def fill(self, cmd, gap_days, n=3):
        log, now = cmdlog.CommandLog(self.home), time_now()
        for i in range(n):
            t = now - i * gap_days * DAY
            log.hook("pre", ev(cmd, f"{cmd}{i}"), now=t)
            log.hook("post", ev(cmd, f"{cmd}{i}"), now=t + 12)

    def test_nothing_recorded_says_so(self):
        code, out, err = self.run_cli("offload", "measure")
        self.assertEqual(code, 0)
        self.assertIn("Nothing recorded yet", out + err)

    def test_it_shows_the_data_and_the_command_that_applies_it_but_changes_nothing(self):
        self.fill("make big", 1)
        self.fill("make lint", 3)
        before = sorted(p.name for p in self.home.iterdir())
        code, out, err = self.run_cli("offload", "measure")
        text = out + err
        self.assertEqual(code, 0)
        for needle in ("Suggested", "4 days", "offload.repeat_days=4", "Candidates at each setting"):
            self.assertIn(needle, text)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), before)

    def test_too_little_data_says_how_much_more(self):
        self.fill("make big", 1, n=2)
        text = "".join(self.run_cli("offload", "measure")[1:])
        self.assertIn("No suggestion yet", text)
        self.assertNotIn("repeat_days=", text)


if __name__ == "__main__":
    unittest.main()

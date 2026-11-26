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
from ct.client import cli, cmdlog  # noqa: E402
from ct.client.repo import ClientRepos  # noqa: E402


def ev(cmd, tid="t1", tool="Bash"):
    return json.dumps({"tool_name": tool, "tool_use_id": tid, "session_id": "s", "tool_input": {"command": cmd}, "cwd": "/x"})


class Log(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.log = cmdlog.CommandLog(self.home, own_tools=("ct-run", "homect"))

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
        self.assertEqual(self.log.rank(records=[{"cmd": "ct-run submit echo", "secs": 99}] * 3), [])
        self.assertEqual(cmdlog.classify("homect status", own_tools=("homect",))[2], cmdlog.OWN)
        self.assertEqual(cmdlog.classify("ct-run jobs", own_tools=("ct-run",))[2], cmdlog.OWN)

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
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("ct-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()
        self.env = mock.patch.dict(os.environ, {"CT_COLOR": "never"})
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
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("ct-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()
        log = cmdlog.CommandLog(self.home)
        for i in range(3):
            log.hook("pre", ev("make big", f"m{i}"), now=1000.0)
            log.hook("post", ev("make big", f"m{i}"), now=1060.0)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}), redirect_stdout(out), redirect_stderr(err):
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
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=("ct-run", "homect"), home=self.home)
        self.cfg = type("C", (), {"paths": type("P", (), {"client_home": str(self.home)})()})()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}), redirect_stdout(out), redirect_stderr(err):
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


class CompoundCommands(unittest.TestCase):
    """A compound line is judged by its one real command: plumbing segments (cd, tail, grep, echo, loops) are dropped and the row's time belongs to what is left.
    The cmd strings below are real lines from a command log, with the folders replaced."""
    PLUMBING = "trivial shell plumbing"

    def real(self, cmd):
        shape, tool, reason = cmdlog.classify(cmd)
        self.assertIsNone(reason, (cmd, reason))
        return shape

    def test_real_lines_from_a_log_are_judged_by_their_real_command(self):
        self.assertEqual(self.real("cd /w/repo; scripts/t --snapshot > /tmp/o.out 2>&1; echo done"), "scripts/t --snapshot")
        self.assertEqual(self.real("cd /w/repo; scripts/t --snapshot > /tmp/o.out 2>&1; tail -c 300 /tmp/o.out"), "scripts/t --snapshot")
        self.assertEqual(self.real("scripts/t test_admin test_update test_docs 2>&1 | tail -6"), "scripts/t")
        self.assertEqual(self.real('scripts/t test_docs 2>&1 | grep -B2 -A8 "^FAIL" | head -20'), "scripts/t")
        self.assertEqual(self.real('(scripts/e2e-orbstack --fresh > /tmp/o.out 2>&1; echo "EXIT $?" >> /tmp/o.out)'), "scripts/e2e-orbstack --fresh")
        self.assertEqual(self.real("(CT_E2E_SCENARIO=direct CT_E2E_MACHINE=m scripts/e2e-orbstack --fresh > /tmp/o.out 2>&1; echo \"exit $?\" >> /tmp/o.out)"),
                         "scripts/e2e-orbstack --fresh")
        self.assertEqual(self.real(": > /tmp/o.out && (scripts/e2e-orbstack --fresh >> /tmp/o.out 2>&1; echo \"run-ended $?\" >> /tmp/o.out)"),
                         "scripts/e2e-orbstack --fresh")

    def test_a_waiting_loop_is_plumbing_not_a_candidate(self):
        cmd = 'for i in $(seq 1 58); do pgrep -f "scripts/t --snapshot" >/dev/null || break; sleep 10; done; tail -c 300 /tmp/o.out'
        self.assertEqual(cmdlog.classify(cmd)[2], self.PLUMBING)

    def test_edge_cases(self):
        self.assertEqual(self.real("cd a && make test"), "make test")
        self.assertEqual(self.real("make test | tail"), "make test")
        self.assertEqual(self.real("cd a && make test | tail -5"), "make test")
        self.assertEqual(self.real("cd a; make test || echo failed"), "make test")
        self.assertEqual(self.real("cd a && FOO=1 timeout 600 make test 2>&1 | tee /tmp/o | tail -3"), "make test")
        self.assertEqual(self.real("cd a && ./scripts/t x | tail"), "scripts/t")
        self.assertEqual(self.real("cd a && ../scripts/t x | tail"), "scripts/t")

    def test_two_real_commands_stay_skipped_with_the_reason_named(self):
        shape, tool, reason = cmdlog.classify("cd a && make test && make lint | tail")
        self.assertEqual((shape, tool), ("make test | make lint", "make"))
        self.assertIn("compound", reason)
        self.assertIn("several", reason)

    def test_version_control_network_and_privileged_segments_still_exclude_the_line(self):
        for cmd in ("cd x && git push", "cd x && scripts/t a | tail && git push", "git status; scripts/t a", "cd x && curl -s u | scripts/t",
                    "cd x && sudo scripts/t", "cd x && npm test | tail"):
            self.assertIsNotNone(cmdlog.classify(cmd)[2], cmd)
        self.assertEqual(cmdlog.classify("cd x && git push")[2], "network / version control")
        self.assertEqual(cmdlog.classify("cd x && scripts/t a | tail", own_tools=("t",))[2], cmdlog.OWN)

    def test_plumbing_alone_keeps_its_old_reason(self):
        for cmd in ("ls -la", "cd x && ls | wc -l", "cat a | grep b | head"):
            self.assertEqual(cmdlog.classify(cmd)[2], self.PLUMBING, cmd)

    def test_a_heredoc_body_is_data_not_commands(self):
        shape, tool, reason = cmdlog.classify("python3 - <<'EOF'\nimport os\nfor x in y:\n    git = 1\nprint(x)\nEOF")
        self.assertEqual((shape, tool, reason), ("python3 -", "python3", cmdlog.INLINE))

    def test_what_runs_after_a_heredoc_is_still_judged_and_an_inline_script_is_not_a_candidate(self):
        self.assertEqual(self.real("cd /w && cat > /tmp/f <<'EOF'\nfor x in y:\n    pass\nEOF\nscripts/t test_a | tail -3"), "scripts/t")
        self.assertEqual(cmdlog.classify("cd /w && python3 - <<'EOF'\ns=open('p').read()\nEOF\ntail -2 f")[2], cmdlog.INLINE)
        self.assertEqual(cmdlog.classify("python3 -c 'print(1)' | tail")[2], cmdlog.INLINE)
        self.assertIsNone(cmdlog.classify("python3 -m unittest x | tail")[2])

    def test_a_command_named_by_a_variable_or_substitution_is_not_judged(self):
        self.assertIsNone(cmdlog.shape("$PY -m unittest x"))
        self.assertIsNone(cmdlog.shape("$(which python) x"))
        self.assertEqual(self.real("cd a && $PY -m x | tail; scripts/t b"), "scripts/t")

    def test_a_separator_inside_quotes_or_a_multiline_quoted_argument_does_not_split(self):
        self.assertEqual(cmdlog.segments('grep -E "a|b" f; scripts/t x'), ['grep -E "a|b" f', "scripts/t x"])
        self.assertEqual(self.real('cd /w && scripts/t a 2>&1 | tail -3'), "scripts/t")
        self.assertEqual(len(cmdlog.segments("make x -m 'line one\nline two came; and more'")), 1)
        self.assertEqual(self.real("/w/venv/bin/python3.13 -m unittest x | tail"), "python3 -m unittest")

    def test_the_example_shown_is_the_real_segment_alone(self):
        self.assertEqual(cmdlog.example_of("cd /w/private && scripts/t a b 2>&1 | tail -5"), "scripts/t a b 2>&1")
        self.assertEqual(cmdlog.example_of("cat > f <<'EOF'\nsecret body\nEOF\nmake test"), "make test")
        self.assertEqual(cmdlog.example_of("ls | wc -l"), "ls")
        log = cmdlog.CommandLog(Path(tempfile.mkdtemp()))
        rows = log.rank(records=[{"ts": 5, "cmd": "cd /w/private && make big | tail", "secs": 60}], now=10)
        self.assertEqual(rows[0]["example"], "make big")

    def test_interpreter_flags_before_the_module_do_not_change_the_shape(self):
        self.assertEqual(cmdlog.shape("python3 -B -I -m unittest discover -s tests")[1], "python3 -m unittest -s")
        self.assertEqual(cmdlog.shape("python3 -m unittest discover -s tests")[1], "python3 -m unittest -s")

    def test_project_scripts_keep_their_name_but_never_a_private_folder(self):
        self.assertEqual(cmdlog.shape("scripts/t a b")[1], "scripts/t")
        self.assertNotEqual(cmdlog.shape("scripts/t a")[1], cmdlog.shape("scripts/e2e-matrix a")[1])
        self.assertEqual(cmdlog.shape("/srv/acme-private/scripts/t x --snapshot")[1], "scripts/t --snapshot")
        self.assertEqual(cmdlog.shape("/opt/acme-private/run.sh --out=/tmp/x")[1], "run.sh --out")
        self.assertEqual(cmdlog.shape("/usr/bin/python3 -m pytest")[1], "python3 -m pytest")
        self.assertEqual(cmdlog.shape("~/.ct-venv/bin/python -m unittest")[1], "python -m unittest")

    def test_the_whole_rows_time_counts_for_the_one_real_command(self):
        log = cmdlog.CommandLog(Path(tempfile.mkdtemp()))
        recs = [{"ts": 1000 + i, "cmd": c, "secs": 40.0} for i, c in enumerate(
            ("cd /w/repo; scripts/t --snapshot > /tmp/o 2>&1; tail -c 300 /tmp/o", "scripts/t a b 2>&1 | tail -6", "cd /w && scripts/t c | tail"))]
        recs += [{"ts": 2000, "cmd": "cd /w && git push", "secs": 99.0}, {"ts": 2001, "cmd": "cd /w && make a && make b", "secs": 99.0}]
        rows = {r["shape"]: r for r in log.rank(records=recs, now=3000)}
        self.assertEqual(set(rows), {"scripts/t --snapshot", "scripts/t"})
        self.assertEqual(rows["scripts/t"]["median"], 40.0)
        self.assertEqual(rows["scripts/t"]["headline"], "Free your own computer")
        shown = {r["shape"]: r["reason"] for r in log.rank(records=recs, show_all=True, now=3000)}
        self.assertIn("compound", shown["make a | make b"])

    def test_a_secret_in_a_compound_line_is_still_never_recorded(self):
        home = Path(tempfile.mkdtemp())
        log = cmdlog.CommandLog(home)
        for cmd in ("cd x && API_KEY=abc scripts/t | tail", "cd x && scripts/t --token abc | tail"):
            log.hook("post", ev(cmd, "k"), now=5)
        self.assertEqual(log.read(), [])


if __name__ == "__main__":
    unittest.main()

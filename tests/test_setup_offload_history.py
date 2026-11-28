"""Slice 7b of theme/offload-value: a new install shows value right away.
`ct-run offload import` reads this computer's own past commands ONCE from shell history (zsh, bash, fish), locally, and records only the command lines (shapes are what any screen prints).
The first release assumes no AI model use: the coding assistant's hooks and transcripts live on the parked branch feat/assistant-hooks-and-transcripts.
Every test uses a temporary home: nothing here ever reads or writes the real shell history."""
import io
import json
import os
import stat
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
from ct.client import cli, cmdlog, histimport  # noqa: E402
from ct.client.repo import ClientRepos  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
IDLE = lambda: {"load": 0.1, "cores": 4, "ratio": 0.03}
OWN = ("ct-run", "homect")


class Base(unittest.TestCase):
    """Fixtures only: a temporary home with shell history files, and a client folder with the log."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "client"
        self.state.mkdir()
        self.log = cmdlog.CommandLog(self.state, own_tools=OWN)

    def zsh(self, text):
        (self.tmp / ".zsh_history").write_text(text)

    def bash(self, text):
        (self.tmp / ".bash_history").write_text(text)

    def fish(self, text):
        p = self.tmp / ".local" / "share" / "fish" / "fish_history"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def scan(self, sources=("zsh",), **kw):
        return histimport.scan(self.tmp, sources, now=NOW, **kw)


class Scan(Base):
    def test_zsh_extended_history_gives_dates_and_whole_second_durations(self):
        t = NOW - 2 * DAY
        self.zsh(f": {t}:75;scripts/t test_a\n: {t + 100}:0;ls\n: {t + 200}:3;make build\n")
        s = self.scan()
        rows = {r["cmd"]: r for r in s.rows}
        self.assertEqual(rows["scripts/t test_a"]["secs"], 75)
        self.assertIsNone(rows["ls"]["secs"])                              # 0 means "under a second": not a measurement, so untimed
        self.assertEqual(rows["make build"]["src"], "zsh")
        self.assertEqual((s.reports[0].timed, s.reports[0].untimed), (2, 1))
        self.assertEqual((s.reports[0].first, s.reports[0].last), (t, t + 200))

    def test_zsh_plain_history_has_no_dates_and_is_not_imported_but_is_counted(self):
        t = NOW - DAY
        self.zsh(f"ls\nmake all\n: {t}:40;make dated\n")
        s = self.scan()
        self.assertEqual([r["cmd"] for r in s.rows], ["make dated"])
        self.assertEqual(s.reports[0].undated, 2)

    def test_zsh_multiline_commands_and_secrets(self):
        t = NOW - DAY
        self.zsh(f": {t}:50;make a \\\n  && make b\n: {t + 1}:9;export API_KEY=zzz\n: {t + 2}:9;curl -H 'Authorization: Bearer abc' https://x\n")
        s = self.scan()
        self.assertEqual([r["cmd"] for r in s.rows], ["make a \\\n  && make b"])
        self.assertEqual(s.reports[0].screened, 2)

    def test_zdotdir_is_followed(self):
        t = NOW - DAY
        (self.tmp / "z").mkdir()
        (self.tmp / "z" / ".zsh_history").write_text(f": {t}:50;make elsewhere\n")
        s = histimport.scan(self.tmp, ("zsh",), now=NOW, env={"ZDOTDIR": str(self.tmp / "z")})
        self.assertEqual([r["cmd"] for r in s.rows], ["make elsewhere"])

    def test_bash_history_is_dated_only_with_timestamp_lines_and_never_timed(self):
        t = NOW - DAY
        self.bash(f"ls\n#{t}\nmake dated\nmake undated\n")
        s = self.scan(("bash",))
        self.assertEqual([(r["cmd"], r["secs"], r["src"]) for r in s.rows], [("make dated", None, "bash")])
        self.assertEqual(s.reports[0].undated, 2)

    def test_fish_history_is_dated_never_timed_and_its_escapes_are_undone(self):
        t = NOW - DAY
        self.fish(f"- cmd: make build\n  when: {t}\n- cmd: echo a\\nmake b \\\\ c\n  when: {t + 5}\n  paths:\n    - foo\n- cmd: export GITHUB_TOKEN=abc\n  when: {t + 6}\n- cmd: no date here\n")
        s = self.scan(("fish",))
        self.assertEqual([(r["cmd"], r["secs"], r["src"]) for r in s.rows], [("make build", None, "fish"), ("echo a\nmake b \\ c", None, "fish")])
        self.assertEqual((s.reports[0].screened, s.reports[0].undated), (1, 1))

    def test_fish_follows_xdg_data_home(self):
        t = NOW - DAY
        p = self.tmp / "data" / "fish" / "fish_history"
        p.parent.mkdir(parents=True)
        p.write_text(f"- cmd: make x\n  when: {t}\n")
        s = histimport.scan(self.tmp, ("fish",), now=NOW, env={"XDG_DATA_HOME": str(self.tmp / "data")})
        self.assertEqual([r["cmd"] for r in s.rows], ["make x"])

    def test_only_the_window_is_read_and_it_says_how_many_were_older(self):
        old, new = NOW - 200 * DAY, NOW - 3 * DAY
        self.zsh(f": {old}:40;make old\n: {new}:40;make new\n")
        s = self.scan(days=90)
        self.assertEqual([r["cmd"] for r in s.rows], ["make new"])
        self.assertEqual(s.reports[0].older, 1)

    def test_a_huge_file_is_skipped_and_named_and_the_others_still_import(self):
        t = NOW - DAY
        self.zsh(f": {t}:40;make big\n" + "x" * 5000)
        self.bash(f"#{t}\nmake small\n")
        s = self.scan(("zsh", "bash"), max_bytes=2000)
        self.assertEqual([r["cmd"] for r in s.rows], ["make small"])
        (path, why), = s.report("zsh").skipped
        self.assertTrue(path.endswith(".zsh_history"))
        self.assertIn("larger", why)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_an_unreadable_file_is_skipped_with_the_system_words_and_the_rest_import(self):
        t = NOW - DAY
        self.zsh(f": {t}:40;make bad\n")
        self.bash(f"#{t}\nmake fine\n")
        z = self.tmp / ".zsh_history"
        z.chmod(0)
        self.addCleanup(lambda: z.chmod(0o600))
        s = self.scan(("zsh", "bash"))
        self.assertEqual([r["cmd"] for r in s.rows], ["make fine"])
        self.assertIn("Permission denied", s.report("zsh").skipped[0][1])

    def test_a_missing_source_is_an_empty_report_not_an_error(self):
        s = histimport.scan(self.tmp / "nowhere", ("zsh", "bash", "fish"), now=NOW)
        self.assertEqual([(r.files, r.commands) for r in s.reports], [(0, 0)] * 3)
        self.assertEqual(s.rows, [])

    def test_binary_junk_in_a_history_file_does_not_break_the_import(self):
        (self.tmp / ".zsh_history").write_bytes(b": 1700000000:5;caf\xc3\xa9 \xff\xfe\n\x00\x00\n")
        (self.tmp / ".bash_history").write_bytes(b"#1700000000\n\xff\xfe\n")
        self.fish("\x00\xff- cmd:\n  when: x\n- cmd: a\n  when: 99999999999999999999999\n")
        self.scan(("zsh", "bash", "fish"))

    def test_nothing_is_modified_in_the_source_files(self):
        t = NOW - DAY
        self.zsh(f": {t}:40;make x\n")
        p = self.tmp / ".zsh_history"
        before, mtime = p.read_bytes(), p.stat().st_mtime_ns
        self.scan()
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), (before, mtime))

    def test_no_source_reads_anything_but_shell_history(self):
        self.assertEqual(sorted(histimport.SOURCES), ["bash", "fish", "zsh"])      # the first release assumes no AI model use


class Recording(Base):
    def rows(self, n=1):
        t = NOW - DAY
        self.zsh("".join(f": {t + i}:60;scripts/t a\n" for i in range(n)))
        return self.scan().rows

    def test_rows_are_recorded_privately_with_a_source_and_never_twice(self):
        self.assertEqual(self.log.import_rows(self.rows()), (1, 0))
        self.assertEqual(self.log.import_rows(self.rows()), (0, 1))          # run it again: no duplicates
        (rec,) = self.log.read()
        self.assertEqual((rec["src"], rec["secs"]), ("zsh", 60))
        self.assertEqual(stat.S_IMODE(self.log.path.stat().st_mode), 0o600)
        self.assertEqual(self.log.imported_count(), 1)

    def test_live_rows_are_untouched_by_an_import_and_by_clearing_it(self):
        live = {"ts": NOW - 10, "cmd": "make live", "secs": 5.0, "cwd": "/x"}
        self.log.path.write_text(json.dumps(live) + "\n")
        self.log.import_rows(self.rows())
        self.assertEqual(len(self.log.read()), 2)
        self.assertEqual(self.log.clear_imported(), 1)
        self.assertEqual(self.log.read(), [live])
        self.assertEqual(self.log.clear_imported(), 0)

    def test_an_imported_row_is_the_same_shape_as_a_live_row_plus_the_source_marks(self):
        self.log.import_rows(self.rows())
        self.assertEqual(set(self.log.read()[0]), {"ts", "cmd", "secs", "cwd", "src", "id"})

    def test_the_review_separates_imported_from_measured_and_untimed_adds_no_hours(self):
        recs = [{"ts": NOW - 5 * DAY + i, "cmd": "cd /w && scripts/t a | tail", "secs": 60.0, "cwd": "/x", "src": "zsh", "id": f"i{i}"} for i in range(8)]
        recs += [{"ts": NOW - 4 * DAY + i, "cmd": "cd /w && scripts/t a | tail", "secs": 60.0, "cwd": "/x"} for i in range(4)]
        recs += [{"ts": NOW - 3 * DAY + i, "cmd": "cd /w && scripts/t a | tail", "secs": None, "cwd": "/x", "src": "fish", "id": f"z{i}"} for i in range(5)]
        recs += [{"ts": NOW - 2 * DAY + i, "cmd": f"ls d{i}", "secs": 0.1, "cwd": "/x"} for i in range(20)]
        r = self.log.review(recs, now=NOW)
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["seconds"], 12 * 60)                           # the 5 untimed runs contribute no seconds
        self.assertEqual(r["untimed"], 5)
        self.assertEqual((r["imported_commands"], r["imported_seconds"]), (13, 8 * 60))
        self.assertEqual(r["recs"][0]["imported"], 13)

    def test_the_summary_card_says_which_runs_came_from_history(self):
        recs = [{"ts": NOW - 5 * DAY + i, "cmd": "scripts/t a", "secs": 60.0, "cwd": "/x", "src": "zsh", "id": f"i{i}"} for i in range(30)]
        r = self.log.review(recs, now=NOW)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            cli.show_review(r, self.log, "homect", err, io.StringIO())
        self.assertRegex(err.getvalue(), r"30 of those runs .*from your past history")
        self.assertNotIn("transcript", err.getvalue())
        r2 = self.log.review([{**x, "src": None} for x in recs], now=NOW)
        err2 = io.StringIO()
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            cli.show_review(r2, self.log, "homect", err2, io.StringIO())
        self.assertNotIn("past history", err2.getvalue())

    def test_an_empty_log_points_at_the_import_and_never_at_an_assistant(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            cli.show_review(self.log.review([], now=NOW), self.log, "homect", err, io.StringIO())
        self.assertIn("ct-run offload import", err.getvalue())
        self.assertNotIn("Claude", err.getvalue())

    def test_the_settings_exist_with_the_agreed_defaults(self):
        spec = config.SCHEMA["offload"]
        self.assertEqual((spec["import_days"][0], spec["import_max_mb"][0]), (90, 200))


class ImportCli(Base):
    def setUp(self):
        super().setUp()
        self.ctx = cli.Context(ClientRepos(self.tmp / "q", self.tmp / "r", "x", "y"), product="homect", own_tools=OWN, home=self.state, host_load=IDLE, clock=lambda: NOW, user_home=self.tmp)
        self.env = mock.patch.dict(os.environ, {"CT_COLOR": "never"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for k in ("ZDOTDIR", "XDG_DATA_HOME"):
            os.environ.pop(k, None)
        t = NOW - 3 * DAY
        self.zsh(f": {t}:70;make z\n: {t + 5}:0;ls\nplain\n")
        self.fish(f"- cmd: scripts/t a\n  when: {t + 9}\n")

    def run_cli(self, *args, tty=True, answers=()):
        out, err = io.StringIO(), io.StringIO()
        given, asked = list(answers), []

        def ask(p):
            asked.append(p)
            return given.pop(0) if given else ""
        self.ctx.is_tty, self.ctx.ask = (lambda: tty), ask
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["offload", "import", *args], ctx=self.ctx, cfg=None)
        return code, out.getvalue() + err.getvalue(), asked

    def test_it_shows_paths_counts_and_dates_then_asks_one_yes_per_source(self):
        code, text, asked = self.run_cli(answers=["y", "n"])
        self.assertEqual(code, 0)
        self.assertIn(str(self.tmp / ".zsh_history"), text)                # what will be read
        self.assertIn("2 commands", text)
        self.assertNotIn(".bash_history", text)                            # a file that is not there is not listed as read
        self.assertEqual(len(asked), 2)                                    # a separate question for each source that has commands
        self.assertEqual(sorted({r["src"] for r in self.log.read()}), ["zsh"])      # the no changed nothing for fish

    def test_declining_everything_changes_nothing(self):
        self.run_cli(answers=["n", "n", "n"])
        self.assertFalse(self.log.path.exists())

    def test_anything_but_yes_is_no(self):
        for a in ("", "no", "maybe", "later"):
            self.run_cli(answers=[a, a])
        self.assertFalse(self.log.path.exists())

    def test_without_a_terminal_it_reads_nothing_and_prints_the_command(self):
        code, text, asked = self.run_cli(tty=False)
        self.assertEqual(asked, [])
        self.assertFalse(self.log.path.exists())
        self.assertIn("ct-run offload import", text)
        self.assertNotIn(str(self.tmp / ".zsh_history"), text)             # not even counted

    def test_running_it_again_adds_nothing_and_says_so(self):
        self.run_cli(answers=["y", "y"])
        n = len(self.log.read())
        code, text, asked = self.run_cli(answers=["y", "y"])
        self.assertEqual((len(self.log.read()), asked), (n, []))           # nothing new, so nothing to ask
        self.assertIn("already", text.lower())

    def test_the_untimed_rows_are_labelled_untimed_and_no_hours_are_claimed(self):
        code, text, _ = self.run_cli(answers=["y", "y"])
        self.assertIn("untimed", text)
        self.assertNotRegex(text, r"\d+ h\b")

    def test_a_source_can_be_chosen(self):
        code, text, asked = self.run_cli("--source", "fish", answers=["y"])
        self.assertEqual(len(asked), 1)
        self.assertEqual({r["src"] for r in self.log.read()}, {"fish"})

    def test_clear_removes_only_what_was_imported(self):
        self.log.path.write_text(json.dumps({"ts": NOW, "cmd": "make live", "secs": 1.0, "cwd": "/x"}) + "\n")
        self.run_cli(answers=["y", "y"])
        code, text, _ = self.run_cli("--clear")
        self.assertEqual([r["cmd"] for r in self.log.read()], ["make live"])
        self.assertIn("Cleared", text)

    def test_nothing_to_import_says_so(self):
        (self.tmp / ".zsh_history").unlink()
        (self.tmp / ".local" / "share" / "fish" / "fish_history").unlink()
        code, text, asked = self.run_cli()
        self.assertEqual(asked, [])
        self.assertIn("nothing to import", text.lower())

    def test_an_unreadable_log_is_a_refusal_with_the_real_error_not_a_traceback(self):
        with mock.patch.object(cmdlog.CommandLog, "read", side_effect=PermissionError(13, "Permission denied")):
            code, text, _ = self.run_cli(answers=["y", "y"])
        self.assertNotEqual(code, 0)
        self.assertIn("Permission denied", text)
        self.assertIn("review.log.unreadable", text)

    def test_the_real_error_of_an_unwritable_log_is_shown(self):
        with mock.patch.object(cmdlog.CommandLog, "import_rows", side_effect=PermissionError(13, "Permission denied")):
            code, text, _ = self.run_cli(answers=["y", "y"])
        self.assertNotEqual(code, 0)
        self.assertIn("Permission denied", text)

    def test_the_assistant_hooks_command_is_not_part_of_this_release(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["offload", "hooks"], ctx=self.ctx, cfg=None)
        self.assertNotEqual(code, 0)


class SetupOffer(Base):
    """The end of `ct-setup install`: one offer, only while the log has too little in it."""

    def cfg(self):
        P = type("P", (), {"client_home": str(self.state)})()
        O = type("O", (), {"repeat_runs": 3, "repeat_days": 14, "busy_load_ratio": 1.0, "busy_min_seconds": 10, "review_days": 14, "review_top": 5, "review_min_commands": 20, "review_min_days": 3})()
        return type("C", (), {"paths": P, "offload": O})()

    def go(self, answers):
        given, asked = list(answers), []

        def ask(p):
            asked.append(p)
            return given.pop(0) if given else "n"
        s = io.StringIO()
        s.isatty = lambda: True
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            for k in ("ZDOTDIR", "XDG_DATA_HOME"):
                os.environ.pop(k, None)
            cli.after_install_review(self.cfg(), stream=s, host_load=IDLE, now=NOW, ask=ask, user_home=self.tmp)
        return s.getvalue(), asked

    def history(self):
        t = NOW - 6 * DAY
        self.zsh("".join(f": {t + i * 60}:45;scripts/t a\n" for i in range(25)))

    def test_a_yes_imports_and_shows_a_real_summary(self):
        self.history()
        text, asked = self.go(["y", "n"])
        self.assertGreater(self.log.imported_count(), 0)
        self.assertIn("past history", text)
        self.assertEqual(len([a for a in asked if "Import" in a]), 1)

    def test_a_no_changes_nothing_and_names_the_command_for_later(self):
        self.history()
        text, asked = self.go(["n"])
        self.assertFalse(self.log.path.exists())
        self.assertIn("ct-run offload import", text)

    def test_with_no_history_at_all_nothing_is_asked(self):
        text, asked = self.go([])
        self.assertEqual([a for a in asked if "Import" in a], [])

    def test_a_log_that_already_has_enough_is_not_asked_again(self):
        self.history()
        recs = [{"ts": NOW - 5 * DAY + i, "cmd": "make a", "secs": 60.0, "cwd": "/x"} for i in range(30)]
        self.log.path.write_text("".join(json.dumps(r) + "\n" for r in recs))
        text, asked = self.go([])
        self.assertEqual([a for a in asked if "Import" in a], [])

    def test_an_unwritable_log_is_a_note_and_the_install_goes_on(self):
        self.history()
        with mock.patch.object(cmdlog.CommandLog, "import_rows", side_effect=PermissionError(13, "Permission denied")):
            text, asked = self.go(["y", "n"])
        self.assertIn("Permission denied", text)


class Catalog(unittest.TestCase):
    def test_the_refusal_is_in_the_catalog_with_steps(self):
        e = refusals.CATALOG["import.log.unwritable"]
        self.assertEqual(e.who, "client")
        self.assertTrue(e.steps)

    def test_no_assistant_refusal_is_in_this_release(self):
        self.assertEqual([c for c in refusals.CATALOG if c.startswith("hooks.")], [])


if __name__ == "__main__":
    unittest.main()

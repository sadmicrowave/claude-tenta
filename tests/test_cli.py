import hashlib
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
from cw import cli, config, guard, perms  # noqa: E402
from support import IP, REGISTRY, Base  # noqa: E402


class CliBase(Base):
    def setUp(self):
        super().setUp()
        self.answers = []
        self.ctx = cli.Context(self.w, product="testcw", admin="cw-admin", installed_rev=self.tmp / "installed-rev", notifier=self.notifier,
                               canary_status=self.tmp / "canary.json", ask=lambda prompt: self.answers.pop(0), uptime=lambda: 4000.0)
        (self.tmp / "installed-rev").write_text("abcdef1234567890\n")
        self.env = mock.patch.dict(os.environ, {"CW_COLOR": "never"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, *args, ctx=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx if ctx else None)
        return code, out.getvalue() + err.getvalue()

    def digest(self, jid):
        return hashlib.sha256((self.state / "inbox" / f"{jid}.json").read_bytes()).hexdigest()


class Passes(CliBase):
    def test_help_lists_every_command(self):
        code, out = self.run_cli("help")
        self.assertEqual(code, 0)
        for word in ("status", "approve", "cancel", "kill", "quarantine", "unlock", "prune-results", "test-alert"):
            self.assertIn(word, out)
        self.assertEqual(self.run_cli()[0], 0)                                               # no arguments is help too

    def test_fetch_step_and_exec_run_the_whole_loop(self):
        self.job("job-0001")
        self.assertEqual(self.run_cli("fetch")[0], 0)
        self.assertEqual(self.spawned, ["job-0001"])
        self.assertEqual(self.run_cli("exec", "job-0001")[0], 0)
        self.assertEqual(self.result("job-0001")["status"], "ok")
        self.assertEqual(self.run_cli("step")[0], 0)

    def test_run_once_does_one_fetch_and_step(self):
        self.job("job-0001")
        self.assertEqual(self.run_cli("run", "--once")[0], 0)
        self.assertEqual(self.spawned, ["job-0001"])

    def test_exec_refuses_something_that_is_not_a_job_id(self):
        code, out = self.run_cli("exec", "../../etc/passwd")
        self.assertEqual(code, 2)
        self.assertIn("not a job id", out)

    def test_unknown_and_incomplete_commands(self):
        for args in (["nonsense"], ["approve"], ["exec"], ["kill"], ["status", "--hours"], ["quarantine", "a", "b"]):
            self.assertNotEqual(self.run_cli(*args)[0], 0, args)


class Reading(CliBase):
    def test_show_prints_the_job_and_names_a_missing_one(self):
        self.job("job-0001")
        self.w.io.sync()
        code, out = self.run_cli("show", "job-0001")
        self.assertEqual((code, json.loads(out)["id"]), (0, "job-0001"))
        code, out = self.run_cli("show", "job-0009")
        self.assertEqual(code, 1)
        self.assertIn("No job called job-0009", out)

    def test_review_shows_the_approval_screen_for_a_profile_run_and_asks_nothing(self):
        self.job("profile-aaa111", "profile-run", argv=["ls", "-l"], what="lists files", expect="instant", est_time_s=1, est_mem_mb=10, flags={"-l": "long"})
        self.w.fetch()
        code, out = self.run_cli("review", "profile-aaa111", "profile-nosuch1")
        self.assertEqual(code, 0)
        for needle in ("PROFILE RUN. If you approve, testcw will run exactly this command", "    ls -l", "WHAT IT DOES", "LOOKED UP BY TESTCW ITSELF", "AT A GLANCE"):
            self.assertIn(needle, out)
        self.assertIn("testcw has no copy of this job", out)
        self.assertEqual(self.answers, [])

    def test_review_of_an_ordinary_job_shows_the_raw_file(self):
        self.job("job-0001", "careful")
        code, out = self.run_cli("review", "job-0001")
        self.assertIn('"class": "careful"', out)


class Approving(CliBase):
    def waiting(self, jid="job-0001"):
        self.job(jid, "careful")
        self.w.fetch()

    def test_interactive_yes_approves_and_shows_what_is_approved(self):
        self.waiting()
        self.answers = ["y"]
        code, out = self.run_cli("approve", "job-0001")
        self.assertEqual(code, 0)
        self.assertIn("APPROVED", out)
        self.assertIn("cw-run result job-0001", out)
        self.assertEqual((self.state / "approved" / "job-0001").read_text(), self.digest("job-0001"))

    def test_interactive_no_leaves_it_waiting(self):
        self.waiting()
        self.answers = ["n"]
        code, out = self.run_cli("approve", "job-0001")
        self.assertEqual(code, 1)
        self.assertIn("Not approved", out)
        self.assertFalse((self.state / "approved" / "job-0001").exists())

    def test_yes_approves_without_asking(self):
        self.waiting()
        self.assertEqual(self.run_cli("approve", "job-0001", "--yes")[0], 0)
        self.assertEqual(self.answers, [])
        self.assertTrue((self.state / "approved" / "job-0001").exists())

    def test_a_pinned_digest_must_match_what_the_worker_holds(self):
        self.waiting()
        code, out = self.run_cli("approve", "job-0001", "--sha256", "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("NOT approved: the job changed", out)
        self.assertFalse((self.state / "approved" / "job-0001").exists())
        code, out = self.run_cli("approve", "job-0001", "--sha256", self.digest("job-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(self.answers, [])                                                   # the person already reviewed it: no prompt

    def test_many_are_each_pinned_and_a_partial_result_has_a_failing_exit_code(self):
        self.waiting("job-0001")
        self.waiting("job-0002")
        code, out = self.run_cli("approve", "--many", f"job-0001:{self.digest('job-0001')}", f"job-0002:{'0' * 64}", "job-0003")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"APPROVED\s+job-0001")
        self.assertRegex(out, r"REFUSED\s+job-0002\s+the job changed")
        self.assertRegex(out, r"REFUSED\s+job-0003\s+no copy")
        self.assertIn("1 approved, 2 refused", out)
        self.assertTrue((self.state / "approved" / "job-0001").exists())
        self.assertFalse((self.state / "approved" / "job-0002").exists())
        code, out = self.run_cli("approve", "--many", "job-0002")                              # no digest: approves the worker's own first-seen copy
        self.assertEqual(code, 0)

    def test_bad_and_unknown_ids(self):
        self.assertEqual(self.run_cli("approve", "../x")[0], 2)
        code, out = self.run_cli("approve", "job-0009")
        self.assertEqual(code, 1)
        self.assertIn("No job called", out)


class Stopping(CliBase):
    def test_cancel_a_waiting_job_and_refuse_a_running_or_unknown_one(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        code, out = self.run_cli("cancel", "job-0001")
        self.assertEqual(code, 0)
        self.assertIn("Cancelled", out)
        self.assertEqual(self.result("job-0001")["status"], "cancelled")
        code, out = self.run_cli("cancel", "job-0001")
        self.assertEqual(code, 1)
        self.assertIn("already running, finished, or unknown", out)
        self.assertIn("cw-kill", out)

    def test_kill_says_when_nothing_is_running(self):
        code, out = self.run_cli("kill", "job-0001")
        self.assertEqual(code, 1)
        self.assertIn("is not running", out)
        self.assertEqual(self.run_cli("kill", "bad id")[0], 2)

    def test_kill_signals_a_job_that_is_really_running(self):
        marker = self.state / "running" / "job-0001"
        marker.write_text(json.dumps({"pid": 123456, "weight": 1}))
        self.make_worker(is_job_process=lambda pid, jid: True)
        self.ctx.w = self.w
        with mock.patch("os.kill") as kill:
            code, out = self.run_cli("kill", "job-0001")
        kill.assert_called_once()
        self.assertEqual(code, 0)


class Security(CliBase):
    def leak(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()

    def test_quarantine_lists_and_reads_withheld_results_and_unlock_clears_the_lock(self):
        code, out = self.run_cli("quarantine")
        self.assertIn("Not locked", out)
        self.assertIn("Nothing in quarantine", out)
        self.leak()
        code, out = self.run_cli("quarantine")
        self.assertIn("testcw is LOCKED", out)
        self.assertIn("private_ip", out)
        self.assertIn("job-0001", out)
        code, out = self.run_cli("quarantine", "job-0001")
        self.assertIn(IP, out)                                                                  # the full text, which never left the machine
        self.assertIn("No quarantined result job-0009", self.run_cli("quarantine", "job-0009")[1])
        self.assertIn("No quarantined result", self.run_cli("quarantine", "../x")[1])
        code, out = self.run_cli("unlock")
        self.assertEqual(code, 0)
        self.assertIn("Unlocked", out)
        self.assertFalse(self.lockdown.locked())
        self.assertFalse(json.loads(self.result_file("security.json"))["locked"])
        self.assertIn("Not locked", self.run_cli("unlock")[1])

    def result_file(self, name):
        import subprocess
        return subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", f"main:{name}"], capture_output=True, text=True).stdout

    def test_canary_alarm_locks_the_worker_and_strips_odd_characters(self):
        code, out = self.run_cli("canary-alarm", "decoy file read by /usr/bin/cat\x00\x1b[31m as worker")
        self.assertEqual(code, 0)
        self.assertIn("Canary alarm raised", out)
        st = self.lockdown.locked()
        self.assertEqual((st["kind"], st["rules"]), ("breach", ["canary_access"]))
        self.assertNotIn("\x00", st["detail"])
        self.assertNotIn("\x1b", st["detail"])
        self.assertEqual([r.get_header("Title") for r in self.pushes], ["testcw BREACH", "testcw LOCKDOWN"])

    def test_test_alert_sends_two_samples_and_locks_nothing(self):
        code, out = self.run_cli("test-alert")
        self.assertEqual(code, 0)
        self.assertEqual([r.get_header("Title") for r in self.pushes], ["testcw TEST", "testcw STILL LOCKED (test)"])
        self.assertIn("Why: Withheld because", self.pushes[0].data.decode())
        self.assertFalse(self.lockdown.locked())
        self.ctx.notifier = type("N", (), {"url": ""})()
        self.assertIn("No phone alert address", self.run_cli("test-alert")[1])

    def test_kept_output_of_a_failed_command_is_listed_and_readable(self):
        self.assertIn("No kept output", self.run_cli("output")[1])
        self.keeper.keep("job-0001", "full trace here")
        code, out = self.run_cli("output")
        self.assertIn("job-0001", out)
        self.assertIn("cw-output <job>", out)
        self.assertEqual(self.run_cli("output", "job-0001")[1].strip(), "full trace here")
        self.assertIn("No kept output for job-0002", self.run_cli("output", "job-0002")[1])

    def test_prune_results_reports_what_it_did(self):
        self.job("job-0001")
        self.run_all()
        self.assertIn("no prune needed", self.run_cli("prune-results")[1])
        self.assertIn("squashed", self.run_cli("prune-results", "--days", "7", "--force")[1])


class Passes(unittest.TestCase):
    def test_a_failing_pass_is_recorded_for_status_and_still_fails(self):
        import tempfile
        d = Path(tempfile.mkdtemp())

        class W:
            state = d

            def fetch(self):
                raise FileNotFoundError(2, "No usable temporary directory")

            def step(self):
                pass
        ctx = type("C", (), {"w": W()})()
        with self.assertRaises(FileNotFoundError):
            cli.run_pass(ctx, "fetch")
        self.assertEqual(cli.run_pass(ctx, "step"), 0)
        data = json.loads((d / cli.PASS_FILE).read_text())
        self.assertFalse(data["fetch"]["ok"])
        self.assertIn("No usable temporary directory", data["fetch"]["error"])
        self.assertTrue(data["step"]["ok"])


class Status(CliBase):
    def test_an_idle_healthy_worker(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        for needle in ("testcw", "WORKER", "Up for", "67 min", "Capacity", "abcdef1", "job classes", "none yet",
                       "Nothing queued or running; testcw is idle.", "LAST 12 HOURS", "No jobs finished", "CHECKS", "Isolation", "OFF", "Memory",
                       "Heat", "40 C", "NOT LOCKED"):
            self.assertIn(needle, out)

    def test_it_shows_work_in_progress_and_what_waits_for_you(self):
        self.job("job-0001")
        self.job("job-0002", "careful")
        self.w.fetch()
        code, out = self.run_cli("status")
        self.assertRegex(out, r"running\s+job-0001\s+pid 4242 PROCESS GONE")
        self.assertIn("WAITING FOR YOU", out)
        self.assertIn("cw-admin approve job-0002", out)                                    # the client's command name is configurable
        self.assertIn("job-0002", out)

    def test_recent_results_are_summarised(self):
        self.job("job-0001")
        self.job("job-0002")
        self.runner.reply = ("error", 1, "ValueError: x\n", {}, "")
        self.run_all()
        code, out = self.run_cli("status", "--hours", "24")
        self.assertIn("LAST 24 HOURS", out)
        self.assertIn("2 finished", out)
        self.assertIn("2 error", out)
        self.assertRegex(out, r"Last job\s+job-000[12]\s+error")

    def test_a_lockdown_is_unmistakable(self):
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        code, out = self.run_cli("status")
        self.assertIn("LOCKED", out)
        self.assertIn("SECURITY LOCKDOWN", out)
        self.assertIn("cw-worker quarantine", out)
        self.assertNotIn("NOT LOCKED", out)

    def test_the_canary_check_reads_the_watchers_status_file(self):
        (self.tmp / "canary.json").write_text(json.dumps({"ts": __import__("time").time(), "monitoring": True}))
        self.assertRegex(self.run_cli("status")[1], r"Canary\s+ON")
        (self.tmp / "canary.json").write_text(json.dumps({"ts": 1, "monitoring": True}))
        self.assertRegex(self.run_cli("status")[1], r"Canary\s+OFF")
        (self.tmp / "canary.json").write_text(json.dumps({"ts": __import__("time").time(), "monitoring": False, "reason": "auditd is not installed"}))
        self.assertIn("auditd is not installed", self.run_cli("status")[1])

    def test_status_says_why_nothing_is_moving_when_a_pass_fails(self):
        out = self.run_cli("status")[1]
        self.assertRegex(out, r"Fetch\s+NOT RUN YET")
        cli.record_pass(self.w.state, "fetch", False, "FileNotFoundError: No usable temporary directory")
        cli.record_pass(self.w.state, "step", True)
        out = self.run_cli("status")[1]
        self.assertRegex(out, r"Fetch\s+FAILED")
        self.assertIn("No usable temporary", out)
        self.assertRegex(out, r"Step\s+OK")

    def test_status_shows_the_claude_tool_and_is_honest_about_the_older_release(self):
        from cw import claudeupdate
        import time as _t
        claudeupdate.save(self.w.state, {"ts": int(_t.time()), "kind": "native", "before": "2.1.325", "after": "2.1.325", "ok": True, "note": ""})
        self.assertRegex(self.run_cli("status")[1], r"Claude\s+2\.1\.325")
        claudeupdate.save(self.w.state, {"ts": int(_t.time()), "kind": "legacy", "before": "2.1.112", "after": "2.1.112", "ok": False, "note": "x"})
        out = self.run_cli("status")[1]
        self.assertRegex(out, r"Claude\s+OLDER 2\.1\.112")
        self.assertIn("newer ones need a newer machine", out)

    def test_hours_are_clamped(self):
        self.assertIn("LAST 168 HOURS", self.run_cli("status", "--hours", "9999")[1])
        self.assertIn("LAST 1 HOURS", self.run_cli("status", "--hours", "0")[1])


class FromConfig(unittest.TestCase):
    def cfg(self, extra=""):
        d = Path(tempfile.mkdtemp())
        self.root = d
        toml = d / "cw.toml"
        toml.write_text(f'[identity]\nname = "homecw"\n[paths]\ncode = "{d}/code"\nstate_root = "{d}/var"\nconfig = "{d}/etc"\nworker_home = "{d}/home"\n'
                        f'[github]\nowner = "alice"\n[hardware]\nprofile = "old-laptop"\n{extra}')
        return config.load(toml, env={"CW_CONFIG": ""})

    def test_everything_is_built_from_the_configuration(self):
        cfg = self.cfg(extra='[claude]\nauth = "api-key"\n')
        (self.root / "etc").mkdir()
        sec = self.root / "etc" / "secrets.env"
        sec.write_text("CW_ALERT_TOKEN=tk_1\nANTHROPIC_API_KEY=key_1\nCLAUDE_CODE_OAUTH_TOKEN=tok_1\n")
        sec.chmod(0o640)
        ctx = cli.Context.from_config(cfg, env={})
        w = ctx.w
        self.assertEqual((ctx.product, w.s.product, w.s.identity, w.s.capacity, w.s.max_temp_c), ("homecw", "homecw", "homecw", 2, 70.0))
        self.assertEqual((str(w.state), str(w.s.registry_path), str(w.io.queue), str(w.io.results)),
                         (f"{self.root}/var/state", f"{self.root}/etc/registry.yaml", f"{self.root}/var/queue", f"{self.root}/var/results"))
        self.assertEqual(w.io.job_sources[0][0], "origin/jobs")
        self.assertEqual((w.api_key, w.oauth_token), ("key_1", None))               # api-key: the token that is also in the file is not given out

    def test_only_the_chosen_sign_in_reaches_the_worker(self):
        for auth, want in (("account", (None, "tok_1")), ("none", (None, None))):
            cfg = self.cfg(extra=f'[claude]\nauth = "{auth}"\n')
            (self.root / "etc").mkdir(exist_ok=True)
            sec = self.root / "etc" / "secrets.env"
            sec.write_text("ANTHROPIC_API_KEY=key_1\nCLAUDE_CODE_OAUTH_TOKEN=tok_1\n")
            sec.chmod(0o640)
            w = cli.Context.from_config(cfg, env={}).w
            self.assertEqual((w.api_key, w.oauth_token), want, auth)
        self.assertEqual(w.s.exec_argv[1:], ["-m", "cw.cli", "exec"])
        self.assertTrue((w.state / "quarantine").is_dir())
        self.assertEqual(oct((w.state / "quarantine").stat().st_mode & 0o777), "0o700")

    def test_alerts_come_from_the_configuration_and_the_token_from_secrets_only(self):
        cfg = self.cfg('[alerts]\nkind = "ntfy"\nurl = "https://ntfy.example.com/cw"\nremind_hours = [2, 4]\n')
        (self.root / "etc").mkdir()
        (self.root / "etc" / "secrets.env").write_text("CW_ALERT_TOKEN=tk_9\n")
        ctx = cli.Context.from_config(cfg, env={})
        self.assertEqual((ctx.notifier.url, ctx.notifier.token), ("https://ntfy.example.com/cw", "tk_9"))
        self.assertEqual(ctx.w.lock.remind_at, [2.0, 4.0])
        self.assertEqual(cli.Context.from_config(self.cfg(), env={}).notifier.url, "")

    def test_a_world_readable_secrets_file_is_warned_about(self):
        cfg = self.cfg()
        (self.root / "etc").mkdir()
        sec = self.root / "etc" / "secrets.env"
        sec.write_text("ANTHROPIC_API_KEY=k\n")
        sec.chmod(0o644)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.Context.from_config(cfg, env={})
        self.assertIn("can be read by every account", out.getvalue())

    def test_main_sets_a_private_umask_and_tightens_existing_modes_once(self):
        cfg = self.cfg()
        old = os.umask(0o022)
        try:
            with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"CW_COLOR": "never"}):
                (self.root / "var" / "state").mkdir(parents=True)
                (self.root / "var" / "state" / "old.txt").write_text("x")
                os.chmod(self.root / "var" / "state" / "old.txt", 0o644)
                self.assertEqual(cli.main(["unlock"], cfg=cfg), 0)
            self.assertEqual(os.umask(old), perms.STATE_UMASK)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.root / "var" / "state" / "old.txt").stat().st_mode), 0o640)
        self.assertTrue((self.root / "var" / "state" / perms.MODES_MARKER).exists())

    def test_a_bad_configuration_is_explained_not_a_traceback(self):
        bad = Path(tempfile.mkdtemp()) / "cw.toml"
        bad.write_text('[server]\nhost = "a b"\n')
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"CW_CONFIG": str(bad), "CW_COLOR": "never"}):
            self.assertEqual(cli.main(["status"]), 1)
        self.assertIn("The configuration has problems", out.getvalue())
        self.assertIn("server.host", out.getvalue())


class RootRunCommands(unittest.TestCase):
    def test_check_registry_validates_with_the_configured_tools_folder(self):
        d = Path(tempfile.mkdtemp())
        good, bad = d / "good.yaml", d / "bad.yaml"
        good.write_text("classes:\n  ok-one:\n    run: [/bin/true]\n    env: {PYTHONPATH: /opt/x/tools/pip}\n    packages: ['pip:a==1']\n")
        bad.write_text("classes:\n  ok-one:\n    run: [/bin/true]\n    env: {PYTHONPATH: /opt/elsewhere}\n")
        cfg = config.load(None, env={"CW_PATHS_CODE": "/opt/x"})
        err = io.StringIO()
        self.assertEqual(cli.cmd_check_registry(good, cfg.path("tools")), 0)
        with redirect_stderr(err):
            self.assertEqual(cli.cmd_check_registry(bad, cfg.path("tools")), 1)
            self.assertEqual(cli.cmd_check_registry(d / "missing.yaml", cfg.path("tools")), 1)
        self.assertIn("registry problem: class 'ok-one': env may only set", err.getvalue())
        self.assertIn("not valid YAML", err.getvalue())


if __name__ == "__main__":
    unittest.main()

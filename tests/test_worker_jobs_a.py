"""Part 1 of 2 of the test_worker_jobs tests (ClaudeJobs, Guard)."""
import unittest
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import guard  # noqa: E402
from support import REGISTRY, IP, Base, sh  # noqa: E402


class ClaudeJobs(Base):
    def setUp(self):
        super().setUp()
        self.registry_path.write_text(REGISTRY + "  askme:\n    kind: claude\n    tier: auto\n    cost: light\n    accepts_files: true\n    allowed_tools: [Read]\n")
        self.make_worker()

    def test_claude_is_told_which_files_are_attached_so_this_file_has_something_to_point_at(self):
        self.job("job-0001", "askme", prompt="What does this file say?", files={"README.md": "# hello\n", "notes.txt": "n\n"})
        self.w.fetch()
        self.w.execute("job-0001")
        call = self.runner.calls[-1]
        prompt = call["argv"][call["argv"].index("-p") + 1]
        self.assertTrue(prompt.startswith("What does this file say?"))
        self.assertIn("Attached files, in the current directory: README.md, notes.txt", prompt)
        self.assertEqual(call["files"], ["README.md", "notes.txt"])

    def test_a_claude_job_gets_exactly_its_tools_and_helper_agents_are_forbidden(self):
        self.job("job-0003", "askme", prompt="hi")
        self.w.fetch()
        self.w.execute("job-0003")
        argv = self.runner.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")                          # only the named tool exists for this job
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Task,Agent")           # and the tools that start helper agents are named as forbidden

    def test_a_claude_that_cannot_restrict_its_tools_is_not_run(self):
        self.w.claude_flags = lambda command: {"--allowedTools"}
        self.job("job-0004", "askme", prompt="hi")
        self.w.fetch()
        self.w.execute("job-0004")
        self.assertEqual(self.runner.calls, [])
        self.assertIn("cannot restrict its tools", str(self.result("job-0004")))

    def test_only_one_job_of_a_claude_class_runs_at_a_time(self):
        for jid in ("job-0005", "job-0006"):
            self.job(jid, "askme", prompt="hi")
            time.sleep(0.05)
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0005"])                                        # the second waits for the first: usage is spent one job at a time
        self.assertTrue(any("already running" in m for m in self.logged))

    def test_what_a_claude_job_used_goes_into_the_ledger_and_its_result(self):
        self.runner.reply = ("ok", 0, "answer", {"wall_s": 1.0, "claude": {"turns": 3, "cost_usd": 0.5}}, "")
        self.job("job-0007", "askme", prompt="hi")
        self.w.fetch()
        self.w.execute("job-0007")
        today = self.w.ledger.today()
        self.assertEqual((today["jobs"], today["turns"], today["cost_usd"]), (1, 3, 0.5))
        self.assertEqual(self.result("job-0007")["metrics"]["claude"]["turns"], 3)

    def test_at_the_daily_limit_new_claude_jobs_wait_other_jobs_run_and_it_is_said_once(self):
        self.w.ledger.jobs_per_day = 1
        self.job("job-0008", "askme", prompt="one")
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0008"])
        self.w._p("running", "job-0008").unlink()                      # the first one finished
        self.w._p("queued", "job-0008.json").unlink()
        self.spawned.clear()
        self.job("job-0009", "askme", prompt="two")
        self.job("job-0010", "echo")
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0010"])                   # the Claude job waits; the echo job behind it still starts
        self.assertTrue(any("daily limit" in m for m in self.logged))
        self.w.step()
        notices = [p for p in self.pushes if "PAUSED CLAUDE" in str(getattr(p, "headers", p))]
        self.assertEqual(len(notices), 1)                              # once a day, not every pass

    def test_a_class_that_returns_a_patch_publishes_the_difference_and_nothing_else_leaves(self):
        self.registry_path.write_text(REGISTRY + "  fixer:\n    kind: claude\n    tier: auto\n    cost: light\n    accepts_files: true\n    reviewed: true\n    returns: patch\n    allowed_tools: [Read, Edit]\n")
        self.make_worker()
        orig = self.runner.__call__

        def edit(argv, timeout, wd, **kw):                              # the "Claude" job changes one file, adds one and leaves a secret-free note
            (Path(wd) / "a.txt").write_text("two\n")
            (Path(wd) / "b.txt").write_text("new\n")
            return orig(argv, timeout, wd, **kw)
        self.w.runner = edit
        self.job("job-0011", "fixer", prompt="fix", files={"a.txt": "one\n"})
        self.w.fetch()
        self.w.execute("job-0011")
        res = self.result("job-0011")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["patch"]["files"], ["a.txt", "b.txt"])
        self.assertIn("+two", res["patch"]["text"])
        self.assertIn("new file mode", res["patch"]["text"])

    def test_a_secret_in_a_patch_is_quarantined_like_any_output(self):
        self.registry_path.write_text(REGISTRY + "  fixer:\n    kind: claude\n    tier: auto\n    cost: light\n    accepts_files: true\n    reviewed: true\n    returns: patch\n    allowed_tools: [Read, Edit]\n")
        self.make_worker()
        orig = self.runner.__call__

        def leak(argv, timeout, wd, **kw):
            (Path(wd) / "a.txt").write_text("-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n-----END RSA " + "PRIVATE KEY-----\n")
            return orig(argv, timeout, wd, **kw)
        self.w.runner = leak
        self.job("job-0012", "fixer", prompt="fix", files={"a.txt": "one\n"})
        self.w.fetch()
        self.w.execute("job-0012")
        res = self.result("job-0012")
        self.assertEqual(res["status"], "quarantined")
        self.assertNotIn("patch", res)
        self.assertNotIn("PRIVATE KEY", json.dumps(res))

    def test_a_prompt_with_no_files_is_left_exactly_as_written(self):
        self.job("job-0002", "askme", prompt="Say hi")
        self.w.fetch()
        self.w.execute("job-0002")
        call = self.runner.calls[-1]
        self.assertEqual(call["argv"][call["argv"].index("-p") + 1], "Say hi")


class Guard(Base):
    def test_a_leak_is_withheld_quarantined_and_locks_the_worker(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["output"]), ("quarantined", ""))
        self.assertNotIn(IP, json.dumps(res))
        self.assertIn("private_ip", res["note"])
        q = self.state / "quarantine" / "job-0001.json"
        self.assertIn(IP, q.read_text())                                                     # the full text stays on the worker
        self.assertEqual(oct(q.stat().st_mode & 0o777), "0o600")
        self.assertTrue(self.lockdown.locked())
        self.assertEqual([r.get_header("Title") for r in self.pushes], ["testct QUARANTINE", "testct LOCKDOWN"])
        self.assertTrue(json.loads(sh("git", "show", "main:security.json", cwd=self.tmp / "results.git"))["locked"])
        self.assertNotIn(IP, "".join(r.data.decode() for r in self.pushes))
        self.assertEqual(self.seclog.verify()[0], True)

    def test_nothing_new_runs_while_locked_until_it_is_cleared(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        self.runner.reply = ("ok", 0, "fine", {}, "")
        self.job("job-0002")
        self.run_all()
        self.assertIsNone(self.result("job-0002"))                                           # not even looked at
        self.assertEqual(len(self.runner.calls), 1)
        self.lockdown.clear()
        self.run_all()
        self.assertEqual(self.result("job-0002")["status"], "ok")

    def test_a_class_may_allow_what_it_legitimately_prints(self):
        self.registry_path.write_text(REGISTRY.replace("  keyed:\n", "  netcheck:\n    tier: auto\n    cost: light\n    run: [/bin/echo, x]\n    output_allow: [private_ip]\n  keyed:\n"))
        self.job("job-0001", "netcheck")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "ok")
        self.assertFalse(self.lockdown.locked())

    def test_the_hostname_rule_uses_the_configured_name(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, "running on zzhost today\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")
        self.assertIn("hostname", self.result("job-0001")["note"])

    def test_a_decoy_credential_in_output_is_a_breach(self):
        self.w.fetch()                                                                        # plants the decoys
        token = guard.canary_tokens(self.state)[0]
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"found {token}\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")
        self.assertEqual([r.get_header("Title") for r in self.pushes][0], "testct BREACH")

    def test_declared_output_keys_only_are_published(self):
        self.job("job-0001", "keyed")
        self.runner.reply = ("ok", 0, json.dumps({"ok": 1, "secret": "x"}), {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual(json.loads(res["output"]), {"ok": 1})
        self.assertIn("undeclared", res["note"])

    def test_a_failed_command_sends_only_its_error_lines_and_keeps_the_rest(self):
        self.job("job-0001")
        trace = 'Traceback (most recent call last):\n  File "/srv/x/t.py", line 3, in go\n    boom()\nValueError: bad input\n'
        self.runner.reply = ("error", 1, trace, {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["output"]), ("error", "ValueError: bad input"))
        self.assertIn("only the error lines were sent", res["note"])
        self.assertIn("/srv/x/t.py", self.keeper.read("job-0001"))

    def test_a_decoy_hidden_in_a_trace_is_caught_even_though_it_was_not_going_to_be_sent(self):
        self.w.fetch()
        token = guard.canary_tokens(self.state)[1]
        self.job("job-0001")
        self.runner.reply = ("error", 1, f'  File "x", line 1\n    token = "{token}"\nValueError: bad\n', {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")


if __name__ == "__main__":
    unittest.main()

"""ct-worker: job lifecycle, Claude jobs, worker state, the job ceiling and the guard."""
import unittest
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import guard  # noqa: E402
from support import REGISTRY, IP, Base, sh  # noqa: E402


class Lifecycle(Base):
    def test_an_auto_job_goes_from_a_requesters_push_to_a_published_result(self):
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                      # fetch queued it and the step pass started it
        self.assertTrue((self.state / "running" / "job-0001").exists())
        self.assertEqual(json.loads((self.state / "running" / "job-0001").read_text())["weight"], 1)
        self.w.execute("job-0001")
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["exit_code"], res["output"], res["class"]), ("ok", 0, "fine\n", "echo"))
        self.assertEqual(res["metrics"]["peak_rss_mb"], 40)
        call = self.runner.calls[0]
        self.assertEqual(call["argv"], ["/bin/echo", "hi"])                                # the argv was built from the template, not a shell string
        self.assertEqual((call["timeout"], call["kw"]["network"], call["kw"]["claude"]), (30, False, False))
        self.assertTrue((self.state / "done" / "job-0001").exists())
        self.assertFalse((self.state / "queued" / "job-0001.json").exists())
        self.assertFalse((self.state / "finished" / "job-0001.json").exists())

    def test_a_finished_job_is_never_run_again(self):
        self.job("job-0001")
        self.run_all()
        self.w.fetch()
        self.assertEqual((self.spawned, len(self.runner.calls)), ([], 1))

    def test_running_status_is_published_first_so_the_client_can_tell_it_was_picked_up(self):
        self.job("job-0001")
        self.w.fetch()
        seen = []
        orig = self.runner.__call__
        self.runner.reply = ("ok", 0, "x", {}, "")

        def peek(argv, timeout, wd, **kw):
            seen.append(self.result("job-0001")["status"])
            return orig(argv, timeout, wd, **kw)
        self.w.runner = peek
        self.w.execute("job-0001")
        self.assertEqual(seen, ["running"])
        self.assertEqual(self.result("job-0001")["status"], "ok")

    def test_input_files_are_written_into_the_job_folder_only_for_classes_that_accept_them(self):
        self.job("job-0001", "files", files={"in.txt": "hello", "sub/b.txt": "x"})
        self.job("job-0002", "echo", files={"in.txt": "hello"})
        self.run_all()
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt", "sub"])
        self.assertEqual(self.result("job-0002")["status"], "rejected")
        self.assertIn("does not accept files", self.result("job-0002")["note"])

    def test_a_file_is_never_written_outside_the_job_folder_even_if_the_name_check_were_wrong(self):
        from unittest import mock
        from ct import jobs
        with mock.patch.object(jobs, "file_name_problem", return_value=None):                  # the name check is the first defence; this is the second, on the path actually written
            self.job("job-0001", "files", files={"../escape.txt": "pwned"})
            self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["refusal"], "files.name.path")
        self.assertFalse((self.state / "work" / "escape.txt").exists())
        self.assertFalse((self.state / "escape.txt").exists())

    def test_a_job_whose_file_left_the_repo_is_still_handled_from_its_snapshot(self):
        self.job("job-0001")
        self.w.fetch()
        self.w._p("running", "job-0001").unlink()
        self.spawned.clear()
        req = self.tmp / "requester"
        sh("git", "rm", "-q", "jobs/job-0001.json", cwd=req)
        sh("git", "commit", "-q", "-m", "gone", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.w.fetch()
        self.assertEqual(self.w.orphan_inbox({}), [self.state / "inbox" / "job-0001.json"])
        self.w.execute("job-0001")
        self.assertEqual(self.result("job-0001")["status"], "ok")


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


class WorkerState(Base):
    """worker-state.json: published when the environment changes, refreshed at most every half hour."""
    FACTS = {"name": "testct", "release": "abc", "os": {"id": "ubuntu", "version": "26.04"}, "cpu": "X", "native_claude": True, "capacity": 4,
             "claude": {"auth": "none"}, "approvers": []}

    def setUp(self):
        super().setUp()
        self.facts = dict(self.FACTS)
        self.make_worker(state_facts=lambda: self.facts)

    def state_commits(self):
        log = sh("git", "log", "--format=%s", "main", cwd=self.tmp / "results.git")
        return sum(1 for l in log.splitlines() if l == "worker state")

    def published(self):
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_it_is_published_and_says_what_the_worker_is(self):
        self.w.fetch()
        doc = self.published()
        self.assertEqual((doc["name"], doc["os"]["version"], doc["claude"]["auth"], doc["locked"]), ("testct", "26.04", "none", False))
        self.assertIn("careful", doc["classes"])
        self.assertIn("load_1m", doc["observed"])

    def test_nothing_is_published_again_while_nothing_changed(self):
        self.w.fetch()
        self.w.fetch()
        self.w.fetch()
        self.assertEqual(self.state_commits(), 1)

    def test_a_changed_environment_is_published_at_once(self):
        self.w.fetch()
        self.facts["claude"] = {"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}}
        self.w.fetch()
        self.assertEqual(self.state_commits(), 2)
        self.assertEqual(self.published()["claude"]["tool"]["version"], "2.1.325")

    def test_a_lockdown_is_published_even_though_a_locked_worker_does_no_other_work(self):
        self.w.fetch()
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.w.fetch()
        self.assertTrue(self.published()["locked"])

    def test_the_readings_are_refreshed_after_half_an_hour(self):
        self.w.fetch()
        stamp = self.state / "worker-state.stamp"
        old = json.loads(stamp.read_text())
        old["at"] -= 3600
        stamp.write_text(json.dumps(old))
        time.sleep(1.1)                                                   # the file carries its own time to the second; an identical second would be no change to commit
        self.w.fetch()
        self.assertEqual(self.state_commits(), 2)

    def test_a_worker_with_no_facts_provider_publishes_nothing(self):
        self.make_worker()
        self.w.fetch()
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_a_provider_that_fails_does_not_break_the_pass(self):
        def boom():
            raise RuntimeError("no cpuinfo")
        self.make_worker(state_facts=boom)
        self.w.fetch()
        self.assertTrue(any("worker state" in m for m in self.logged))


class JobCeiling(Base):
    """A worker accepts only what it can hold (security spot-check F5): a ceiling from its free disk and memory (or [hardware] max_job_mb), checked on the local clone BEFORE the job file is read, a disk reserve that keeps it from
    starting jobs when nearly full, and the published limits following the ceiling on Direct."""

    def worker_with(self, **over):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, **over)
        self.make_worker()

    def shows(self):
        calls = []
        real = self.w.io.git_bytes
        def spy(repo, *args):
            calls.append(args)
            return real(repo, *args)
        self.w.io.git_bytes = spy
        return calls

    def test_a_job_over_the_ceiling_is_refused_before_its_file_is_read(self):
        self.worker_with(max_job_mb=1)
        calls = self.shows()
        self.job("job-0001", "files", files={"big.txt": "x" * 2_000_000})
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["refusal"]), ("rejected", "job.too_big_for_worker"))
        self.assertIn("1 MB", res["note"])
        self.assertFalse([c for c in calls if c and c[0] == "show"], "the whole job was read before its size was checked")
        self.assertTrue(any(c[:2] == ("cat-file", "-s") for c in calls))
        self.assertFalse((self.state / "inbox" / "job-0001.json").exists())
        self.assertEqual(self.runner.calls, [])

    def test_a_job_under_the_ceiling_is_accepted_and_runs(self):
        self.worker_with(max_job_mb=1)
        self.job("job-0002", "files", files={"small.txt": "x" * 1000})
        self.run_all()
        self.assertEqual(self.result("job-0002")["status"], "ok")

    def test_the_automatic_ceiling_is_the_smallest_of_a_quarter_of_free_disk_a_quarter_of_free_memory_and_one_gigabyte(self):
        for disk_gb, mem_mb, want in ((100, 4000, 1000), (1, 4000, 250), (1000, 100000, 1024), (100, 400, 100)):
            self.w.snapshot = lambda d=disk_gb, m=mem_mb: {"disk_free_gb": d, "mem_available_mb": m}
            self.assertEqual(self.w.job_ceiling_mb(), want, (disk_gb, mem_mb))
        self.worker_with(max_job_mb=7)
        self.w.snapshot = lambda: {"disk_free_gb": 1000, "mem_available_mb": 100000}
        self.assertEqual(self.w.job_ceiling_mb(), 7)                                              # the owner's number wins

    def test_a_nearly_full_disk_makes_the_worker_wait_instead_of_starting_a_job(self):
        from unittest import mock
        self.worker_with(disk_reserve=True)
        usage = lambda free_gb, total_gb: mock.Mock(free=int(free_gb * 1e9), total=int(total_gb * 1e9))      # noqa: E731
        self.assertEqual(self.w.disk_reserve_gb(total_gb=100), 10.0)                              # 10% of the volume ...
        self.assertEqual(self.w.disk_reserve_gb(total_gb=10), 2.0)                                # ... but never less than 2 GB
        with mock.patch("ct.worker.shutil.disk_usage", return_value=usage(1.5, 100)):
            self.assertIn("disk nearly full", self.w.busy_reason())
        with mock.patch("ct.worker.shutil.disk_usage", return_value=usage(50, 100)):
            self.assertIsNone(self.w.busy_reason())

    def test_on_direct_the_published_file_limits_follow_the_ceiling_and_on_github_they_do_not(self):
        self.worker_with(transport="direct", max_job_mb=50)
        self.w.load_registry()
        files = self.w.limits["files"]
        self.assertEqual((files["max_total_kb"], files["max_file_kb"]), (50_000, 50_000))
        self.assertGreater(files["max_count"], 200)
        self.worker_with(transport="github", max_job_mb=50)
        self.w.load_registry()
        self.assertEqual(self.w.limits["files"]["max_total_kb"], 2000)                            # GitHub keeps the old defaults
        self.assertEqual(self.w.limits["files"]["max_file_kb"], 500)

    def test_a_changed_connection_republishes_the_limits_though_the_registry_did_not_change(self):
        pub = lambda: json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["limits"]["files"]["max_total_kb"]       # noqa: E731
        self.worker_with(transport="github", max_job_mb=50)
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertEqual(pub(), 2000)
        self.worker_with(transport="direct", max_job_mb=50)
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertEqual(pub(), 50_000)

    def test_a_limit_the_owner_set_in_the_registry_still_wins_on_direct(self):
        self.registry_path.write_text(REGISTRY + "limits:\n  files:\n    max_total_kb: 100\n")
        self.worker_with(transport="direct", max_job_mb=50)
        self.w.load_registry()
        self.assertEqual(self.w.limits["files"]["max_total_kb"], 100)

    def test_the_worker_publishes_its_ceiling(self):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, max_job_mb=64)
        self.make_worker(state_facts=lambda: {"name": "testct", "release": "0" * 40, "os": {"id": "ubuntu"}, "cpu": "x", "capacity": 4})
        self.w.fetch()
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)["observed"]["job_ceiling_mb"], 64)

    def test_a_waiting_job_is_started_from_a_small_sidecar_not_by_parsing_the_whole_job(self):
        self.job("job-0003", "files", files={"in.txt": "hello"})
        self.w.fetch()
        meta = self.state / "queued" / "job-0003.meta"
        self.assertEqual(json.loads(meta.read_text())["class"], "files")
        self.w._p("queued", "job-0003.json").write_bytes(b"not json at all " * 1000)               # step must not need to parse it to learn the class
        self.w._p("running", "job-0003").unlink(missing_ok=True)                                   # (fetch already started it once)
        self.spawned.clear()
        self.w.step()
        self.assertEqual(self.spawned, ["job-0003"])


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

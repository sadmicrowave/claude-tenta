"""The whole lifecycle, end to end: a requester pushes a job file to a (bare) queue repo, the worker fetches, validates, holds for approval,
queues, runs, guards and publishes a result to a (bare) results repo. Only the command runner and the process spawner are faked."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import approvals, gitio, guard, runner, util  # noqa: E402
from support import REGISTRY, IP, Base, sh  # noqa: E402
from ct import jail as jail_mod  # noqa: E402


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


class SmartSleep(Base):
    """An idle worker stops asking GitHub every pass; any work ends the idleness at once."""

    def setUp(self):
        super().setUp()
        self.syncs = 0
        real = self.io.sync
        self.io.sync = lambda: (setattr(self, "syncs", self.syncs + 1), real())[1]
        self.settings.idle_after_s, self.settings.idle_fetch_s = 600, 90

    def age(self, name, seconds):
        p = self.state / name
        t = time.time() - seconds
        os.utime(p, (t, t))

    def go_idle(self):
        self.w.fetch()                                                   # a first pass: leaves the stamps
        (self.state / "activity.stamp").touch()
        (self.state / "last-sync.stamp").touch()
        self.age("activity.stamp", 3600)
        self.age("last-sync.stamp", 5)

    def test_a_worker_with_nothing_to_do_skips_the_check_between_slow_polls(self):
        self.go_idle()
        before = self.syncs
        self.assertEqual(self.w.fetch(), [])
        self.assertEqual(self.syncs, before)
        self.assertTrue(self.w.idle_now)

    def test_after_a_longer_quiet_it_sleeps_and_checks_only_at_the_sleep_pace(self):
        self.settings.sleep_after_s, self.settings.sleep_fetch_s = 7200, 300
        self.go_idle()
        self.age("activity.stamp", 8000)                                  # quiet for longer than sleep_after_s
        self.age("last-sync.stamp", 100)                                  # past the idle pace (90) but not the sleep pace (300)
        before = self.syncs
        self.w.fetch()
        self.assertEqual((self.syncs, self.w.mode), (before, "sleep"))
        self.age("last-sync.stamp", 400)
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_sleep_mode_can_be_turned_off_and_work_wakes_it(self):
        self.settings.sleep_after_s, self.settings.sleep_fetch_s = 7200, 0
        self.go_idle()
        self.age("activity.stamp", 8000)
        self.age("last-sync.stamp", 100)
        before = self.syncs
        self.w.fetch()
        self.assertEqual((self.syncs, self.w.mode), (before + 1, "idle"))     # no sleep mode: it stays at the idle pace
        self.settings.sleep_fetch_s = 300
        self.job("job-0002", "echo")
        self.age("last-sync.stamp", 400)
        self.w.fetch()                                                    # the slow check finds the job ...
        self.w.fetch()                                                    # ... and from then on the worker is active again
        self.assertEqual(self.w.mode, "active")

    def test_it_still_checks_at_the_slow_pace(self):
        self.go_idle()
        self.age("last-sync.stamp", 100)                                  # longer than idle_fetch_s
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_a_new_job_is_seen_by_the_next_slow_check_and_ends_the_idleness(self):
        self.go_idle()
        self.job("job-0001", "echo")
        self.age("last-sync.stamp", 100)
        self.w.fetch()
        self.assertFalse(self.w.idle_now and not self.w.has_work())
        self.assertTrue((self.state / "activity.stamp").stat().st_mtime > time.time() - 60)
        before = self.syncs
        self.w.fetch()                                                    # not idle any more: every pass checks again
        self.assertEqual(self.syncs, before + 1)

    def test_a_job_waiting_for_approval_keeps_the_normal_pace(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.age("activity.stamp", 3600)
        self.age("last-sync.stamp", 5)
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_a_locked_worker_keeps_the_normal_pace_so_an_unlock_order_is_seen_quickly(self):
        self.go_idle()
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_zero_turns_smart_sleep_off(self):
        self.settings.idle_fetch_s = 0
        self.go_idle()
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)


class SignedApproval(Base):
    """Approving from the administrator's own computer: a signed approval is published next to the job, and the worker checks it against its approver key."""

    def setUp(self):
        super().setUp()
        self.keys = Path(tempfile.mkdtemp())
        self.pub = self.keygen("key")
        self.signers = self.keys / "approvers"
        self.signers.write_text(approvals.with_approver("", self.pub))
        self.make_worker(signed_approval=self.verifier, approvers_file=self.signers)

    def keygen(self, name):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.keys / name)], check=True)
        return (self.keys / f"{name}.pub").read_text()

    def verifier(self, jid, digest):
        raw = self.io.approval_bytes(jid)
        return raw is not None and approvals.check(raw.decode(), jid, digest, self.signers)[0]

    def digest(self, jid):
        import hashlib
        return hashlib.sha256((self.state / "inbox" / f"{jid}.json").read_bytes()).hexdigest()

    def publish_approval(self, jid, digest, key="key"):
        record = approvals.make_record(jid, digest)
        text = approvals.bundle(record, approvals.sign(record, self.keys / key))
        req = self.tmp / "requester"
        remote = subprocess.run(["git", "-C", str(self.tmp / "queue.git"), "rev-parse", "--verify", "-q", "refs/heads/approvals"], capture_output=True).returncode == 0
        sh("git", "checkout", "-q", "-B", "approvals", "origin/approvals" if remote else "origin/main", cwd=req)
        (req / "approvals").mkdir(exist_ok=True)
        (req / "approvals" / f"{jid}.json").write_text(text)
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", f"approve {jid}", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/approvals", cwd=req)

    def test_a_signed_approval_lets_the_waiting_job_run_without_anyone_logging_in(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.assertEqual(self.result("job-0001")["status"], "awaiting_approval")
        self.assertEqual(self.spawned, [])
        self.publish_approval("job-0001", self.digest("job-0001"))
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])
        self.w.execute("job-0001")
        self.assertEqual(self.result("job-0001")["status"], "ok")

    def test_whoever_can_push_to_the_queue_repo_cannot_approve_with_a_key_of_their_own(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.keygen("intruder")
        self.publish_approval("job-0001", self.digest("job-0001"), key="intruder")
        self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("not accepted" in m or "signature" in m for m in self.logged) or self.result("job-0001")["status"] == "awaiting_approval")
        self.assertEqual(self.result("job-0001")["status"], "awaiting_approval")

    def test_an_approval_of_another_version_of_the_job_does_not_count(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.publish_approval("job-0001", "0" * 64)
        self.w.fetch()
        self.assertEqual(self.spawned, [])

    def test_a_worker_with_no_approver_key_never_accepts_a_signed_approval(self):
        self.signers.unlink()
        self.job("job-0001", "careful")
        self.w.fetch()
        self.publish_approval("job-0001", self.digest("job-0001"))
        self.w.fetch()
        self.assertEqual(self.spawned, [])


class PrivilegedJobs(SignedApproval):
    """A class of kind `privileged` runs as root through the helper, and only ever on the administrator's signed approval."""
    ACTIONS = ("\nactions:\n  fan-profile:\n    run: [/usr/local/sbin/fan-profile, \"{profile}\"]\n    params: {profile: {enum: [quiet, balanced]}}\n    sha256: \"%s\"\n"
               "classes_extra: {}\n" % ("d" * 64))

    def setUp(self):
        super().setUp()
        text = REGISTRY.rstrip("\n") + "\n  fan:\n    kind: privileged\n    action: fan-profile\n    cost: light\n" + self.ACTIONS.replace("classes_extra: {}\n", "")
        self.registry_path.write_text(text)
        self.calls, self.answer = [], {"status": "ok", "exit_code": 0, "output": "profile set\n", "note": ""}
        self.make_worker(signed_approval=self.verifier, approvers_file=self.signers, privileged_call=lambda raw, approval, timeout: (self.calls.append((raw, approval, timeout)), self.answer)[1])

    def submit(self, jid="fan-0001", **params):
        self.job(jid, "fan", **{"params": params or {"profile": "quiet"}})
        self.w.fetch()
        return jid

    def test_it_waits_and_says_it_runs_as_root_and_needs_the_signature(self):
        jid = self.submit()
        res = self.result(jid)
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertIn("runs as root", res["note"])
        self.assertIn("/usr/local/sbin/fan-profile quiet", res["note"])
        self.assertEqual(self.spawned, [])

    def test_the_registry_the_worker_publishes_tells_a_reviewer_what_the_class_does_as_root(self):
        self.w.fetch()
        info = json.loads(subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:registry.json"], capture_output=True, text=True).stdout)["classes"]["fan"]
        self.assertTrue(info["privileged"])
        self.assertEqual((info["run"], info["tier"]), (["/usr/local/sbin/fan-profile", "{profile}"], "approve"))

    def test_the_workers_own_approval_marker_and_an_approval_typed_on_the_worker_are_not_enough(self):
        jid = self.submit()
        ok, why = self.w.approve(jid)
        self.assertFalse(ok)
        self.assertIn("signature from your computer", why)
        (self.state / "approved").mkdir(exist_ok=True)
        (self.state / "approved" / jid).write_text(self.digest(jid))            # what a compromised job running as the worker could write
        self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.calls, [])

    def test_a_signed_approval_runs_it_through_the_helper_with_the_exact_file_and_the_signature(self):
        jid = self.submit()
        self.publish_approval(jid, self.digest(jid))
        self.w.fetch()
        self.assertEqual(self.spawned, [jid])
        self.w.execute(jid)
        (raw, approval, timeout), = self.calls
        self.assertEqual(raw, (self.state / "inbox" / f"{jid}.json").read_bytes())
        self.assertTrue(approvals.check(approval, jid, self.digest(jid), self.signers)[0])
        self.assertEqual(self.result(jid)["status"], "ok")
        self.assertIn("profile set", self.result(jid)["output"])

    def test_a_helper_that_refuses_is_an_error_result_with_its_reason(self):
        jid = self.submit()
        self.publish_approval(jid, self.digest(jid))
        self.w.fetch()
        self.answer = {"status": "refused", "note": "the program is not the one that was approved", "output": ""}
        self.w.execute(jid)
        res = self.result(jid)
        self.assertEqual(res["status"], "error")
        self.assertIn("not the one that was approved", res["note"])

    def test_without_a_helper_configured_it_fails_cleanly(self):
        self.make_worker(signed_approval=self.verifier, approvers_file=self.signers)
        jid = self.submit("fan-0002")
        self.publish_approval(jid, self.digest(jid))
        self.w.fetch()
        self.w.execute(jid)
        self.assertIn("no root helper", self.result(jid)["note"])

    def test_parameters_outside_the_action_are_rejected_before_anything_is_asked_of_root(self):
        self.job("fan-0003", "fan", params={"profile": "turbo"})
        self.w.fetch()
        self.assertEqual(self.result("fan-0003")["status"], "rejected")
        self.job("fan-0004", "fan", params={"profile": "quiet"}, prompt="and also do this")
        self.w.fetch()
        self.assertEqual(self.result("fan-0004")["status"], "rejected")
        self.assertEqual(self.calls, [])

    def test_what_root_prints_still_goes_through_the_output_guard(self):
        jid = self.submit("fan-0005")
        self.publish_approval(jid, self.digest(jid))
        self.w.fetch()
        self.answer = {"status": "ok", "exit_code": 0, "output": f"connected to {IP}\n", "note": ""}
        self.w.execute(jid)
        self.assertEqual(self.result(jid)["status"], "quarantined")
        self.assertTrue(self.lockdown.locked())


class SignedOrders(SignedApproval):
    """Unlock, cancel and kill ordered from the administrator's own computer."""

    def order(self, action, key="key", nonce=None, **kw):
        record = approvals.make_control_record(action, nonce=nonce, **kw)
        text = approvals.bundle(record, approvals.sign(record, self.keys / key, namespace=approvals.CONTROL_NAMESPACE))
        nonce = json.loads(record)["nonce"]
        req = self.tmp / "requester"
        remote = subprocess.run(["git", "-C", str(self.tmp / "queue.git"), "rev-parse", "--verify", "-q", "refs/heads/approvals"], capture_output=True).returncode == 0
        sh("git", "checkout", "-q", "-B", "approvals", "origin/approvals" if remote else "origin/main", cwd=req)
        (req / "control").mkdir(exist_ok=True)
        (req / "control" / f"{nonce}.json").write_text(text)
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", f"control {action}", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/approvals", cwd=req)
        return nonce

    def test_a_signed_cancel_cancels_a_job_that_is_waiting_for_approval(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.order("cancel", job="job-0001")
        self.w.fetch()
        self.assertEqual(self.result("job-0001")["status"], "cancelled")

    def test_a_signed_unlock_clears_the_lockdown_even_though_a_locked_worker_does_nothing_else(self):
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        since = self.lockdown.locked()["since"]
        self.order("unlock", lock=since)
        self.w.fetch()
        self.assertIsNone(self.lockdown.locked())

    def test_an_unlock_signed_for_an_earlier_lockdown_does_not_clear_a_later_one(self):
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.order("unlock", lock="1999-01-01T00:00:00Z")
        self.w.fetch()
        self.assertIsNotNone(self.lockdown.locked())
        self.assertTrue(any("earlier lockdown" in m for m in self.logged))

    def test_an_order_is_carried_out_at_most_once(self):
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.order("unlock", lock=self.lockdown.locked()["since"], nonce="aaaa1111")
        self.w.fetch()
        self.assertIsNone(self.lockdown.locked())
        self.lockdown.engage("quarantine", "job-0010", {"private_ip"}, cls="echo")          # a new lockdown: the old order is still on GitHub
        self.w.fetch()
        self.assertIsNotNone(self.lockdown.locked())

    def test_an_order_signed_with_a_key_the_worker_does_not_trust_does_nothing(self):
        self.keygen("intruder")
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.order("unlock", key="intruder", lock=self.lockdown.locked()["since"])
        self.w.fetch()
        self.assertIsNotNone(self.lockdown.locked())
        self.assertTrue(any("not accepted" in m for m in self.logged))

    def test_a_signed_kill_stops_the_running_job(self):
        killed = []
        self.w.kill_job = lambda jid, hard=False: killed.append((jid, hard)) or True
        self.order("kill", job="job-0001", hard=True)
        self.w.fetch()
        self.assertEqual(killed, [("job-0001", True)])

    def test_with_no_approver_key_installed_no_order_is_ever_carried_out(self):
        self.signers.unlink()
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.order("unlock", lock=self.lockdown.locked()["since"])
        self.w.fetch()
        self.assertIsNotNone(self.lockdown.locked())


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


class ProfileWithoutApproval(Base):
    """On a Direct worker the owner may let a profile run start without approval (transport.profile_approval = none), but only where the job really is confined: a jail that isolates (not a Windows Job Object) and is ready."""
    JOB = dict(argv=["ls", "-l"], what="lists files", expect="instant", est_time_s=1, est_mem_mb=10, flags={"-l": "long"})

    class Jail(jail_mod.Jail):
        def __init__(self, ready, isolates):
            self._ready, self.isolates = ready, isolates

        def ready(self):
            return self._ready

        def wrap(self, argv, **kw):
            return list(argv)

    def worker_with(self, approval, jail):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, profile_approval=approval)
        self.make_worker(jail=jail, state_facts=lambda: {"name": "testct", "release": "0" * 40, "os": {"id": "ubuntu"}, "cpu": "x", "capacity": 4})

    def status_of(self, jid="profile-aaa111"):
        self.job(jid, "profile-run", **self.JOB)
        self.run_all()
        return self.result(jid)["status"]

    def test_with_the_setting_off_and_an_isolating_jail_a_profile_run_starts_without_approval(self):
        self.worker_with("none", self.Jail(ready=True, isolates=True))
        self.assertEqual(self.status_of(), "ok")
        self.assertEqual(Path(self.runner.calls[0]["argv"][0]).name, "ls")                                  # it ran, with no approval given

    def test_it_still_waits_for_approval_when_the_jail_is_not_ready_or_does_not_isolate(self):
        for jail in (self.Jail(ready=False, isolates=True), self.Jail(ready=True, isolates=False), jail_mod.NoJail(), jail_mod.WindowsJobObject()):
            self.worker_with("none", jail)
            self.assertEqual(self.status_of("profile-" + type(jail).__name__.lower()[:6] + str(id(jail) % 1000)), "awaiting_approval", type(jail).__name__)

    def test_by_default_it_waits_for_approval_whatever_the_jail(self):
        self.worker_with("required", self.Jail(ready=True, isolates=True))
        self.assertEqual(self.status_of(), "awaiting_approval")

    def test_nothing_else_loses_its_approval(self):
        self.worker_with("none", self.Jail(ready=True, isolates=True))
        self.job("job-0001", "careful")                                                                   # an ordinary class registered with tier approve
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "awaiting_approval")

    def test_the_worker_says_what_it_will_do_in_its_published_state(self):
        for approval, jail, want in (("none", self.Jail(ready=True, isolates=True), "none"), ("none", self.Jail(ready=True, isolates=False), "required"), ("required", self.Jail(ready=True, isolates=True), "required")):
            self.worker_with(approval, jail)
            self.w.fetch()
            r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
            self.assertEqual(json.loads(r.stdout)["profile_approval"], want, (approval, type(jail).__name__))


class Rejections(Base):
    def rejected(self, jid, needle):
        self.run_all()
        res = self.result(jid)
        self.assertEqual(res["status"], "rejected", res)
        self.assertIn(needle, res["note"])
        self.assertEqual(self.runner.calls, [])                                            # nothing ran

    def test_a_refused_job_carries_the_code_of_its_refusal_in_its_published_result(self):
        self.job("job-0001", created=util.iso(util.now() - timedelta(days=3)))
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["refusal"], "job.expired")

    def test_unknown_class(self):
        self.job("job-0001", "nope")
        self.rejected("job-0001", "class not in installed registry")

    def test_bad_parameters_never_reach_a_command(self):
        self.job("job-0001", "echo", params={"word": "x; rm -rf /"})
        self.rejected("job-0001", "does not match the allowed pattern")

    def test_a_replayed_old_job_is_refused(self):
        self.job("job-0001", created=util.iso(util.now() - timedelta(days=3)))
        self.rejected("job-0001", "expired")

    def test_a_future_dated_job_is_refused(self):
        self.job("job-0001", created=util.iso(util.now() + timedelta(hours=3)))
        self.rejected("job-0001", "future")

    def test_an_id_that_does_not_match_the_file_name_is_refused(self):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "jobs", "origin/main", cwd=req)
        (req / "jobs").mkdir()
        (req / "jobs" / "job-0001.json").write_text(json.dumps({"id": "job-0002", "class": "echo", "created": util.iso()}))
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "x", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.rejected("job-0001", "does not match filename")

    def test_a_file_that_is_not_json_is_refused(self):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "jobs", "origin/main", cwd=req)
        (req / "jobs").mkdir()
        (req / "jobs" / "job-0001.json").write_text("{ nope")
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "x", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.rejected("job-0001", "not valid JSON")

    def test_a_rejection_is_published_once_and_not_repeated(self):
        self.job("job-0001", "nope")
        self.run_all()
        self.run_all()
        log = sh("git", "log", "--format=%s", "main", cwd=self.tmp / "results.git").splitlines()
        self.assertEqual(sum(1 for l in log if l.startswith("result job-0001")), 1)


class Approval(Base):
    def test_a_job_that_needs_approval_waits_then_runs_only_what_was_approved(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertIn("ct-admin approve job-0001", res["note"])
        self.assertEqual((self.spawned, self.runner.calls), ([], []))
        self.w.fetch()                                                                       # still waiting; not announced a second time
        log = sh("git", "log", "--format=%s", "main", cwd=self.tmp / "results.git").splitlines()
        self.assertEqual(sum(1 for l in log if "job-0001" in l), 1)
        ok, why = self.w.approve("job-0001")
        self.assertTrue(ok, why)
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])
        self.w.execute("job-0001")
        self.assertEqual(self.result("job-0001")["status"], "ok")

    def test_approval_is_pinned_to_the_digest_that_was_reviewed(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        ok, why = self.w.approve("job-0001", sha="0" * 64)
        self.assertFalse(ok)
        self.assertIn("changed since you viewed it", why)
        self.assertFalse((self.state / "approved" / "job-0001").exists())
        import hashlib
        digest = hashlib.sha256((self.state / "inbox" / "job-0001.json").read_bytes()).hexdigest()
        self.assertEqual(self.w.approve("job-0001", sha=digest), (True, ""))
        self.assertEqual(self.w.approve("job-nosuch1"), (False, "no copy of that job on the worker"))
        self.assertEqual(self.w.approve("../etc/passwd"), (False, "no copy of that job on the worker"))

    def test_a_job_edited_after_the_worker_saw_it_runs_the_snapshot_not_the_edit(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.w.approve("job-0001")
        self.job("job-0001", "big")                                                          # the requester swaps in a different class
        self.w.fetch()
        self.w.execute("job-0001")
        self.assertEqual(self.runner.calls[0]["argv"], ["/bin/echo", "careful"])
        self.assertEqual(self.result("job-0001")["class"], "careful")

    def test_an_approval_does_not_transfer_to_changed_bytes(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.w.approve("job-0001")
        inbox = self.state / "inbox" / "job-0001.json"
        inbox.write_text(inbox.read_text().replace("careful", "echo") + " ")                  # the snapshot itself is tampered with
        self.w.process(inbox, self.w.load_registry())
        self.assertEqual(self.spawned, [])

    def test_a_cancelled_waiting_job_is_published_as_cancelled(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.assertEqual(self.w.cancel("job-0001"), (True, ""))
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["class"], res["note"]), ("cancelled", "careful", "cancelled before it was approved"))
        self.assertFalse((self.state / "approved" / "job-0001").exists())
        self.assertFalse(self.w.cancel("job-0001")[0])                                        # already done
        self.assertFalse(self.w.cancel("job-nosuch1")[0])                                      # unknown
        self.assertFalse(self.w.cancel("../x")[0])

    def test_a_queued_job_can_be_cancelled_and_an_approval_does_not_survive_it(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.w.approve("job-0001")
        self.w.process(self.state / "inbox" / "job-0001.json", self.w.load_registry())         # queued (approved) but not started
        self.assertTrue((self.state / "queued" / "job-0001.json").exists())
        self.assertEqual(self.w.cancel("job-0001"), (True, ""))
        self.assertEqual(self.result("job-0001")["note"], "cancelled on testct before it started")
        self.assertFalse((self.state / "approved" / "job-0001").exists())

    def test_a_running_job_cannot_be_cancelled(self):
        (self.state / "running" / "job-0001").write_text("{}")
        self.assertEqual(self.w.cancel("job-0001")[0], False)


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


class Capacity(Base):
    def test_strict_fifo_within_the_capacity_budget(self):
        for jid, cls in (("job-0001", "echo"), ("job-0002", "big"), ("job-0003", "echo")):
            self.job(jid, cls)
            time.sleep(0.05)
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                          # big needs all 4 points: it waits, and job-0003 waits behind it
        self.assertTrue(any("job-0002 needs 4" in m for m in self.logged))

    def test_a_loaded_or_hot_machine_starts_nothing(self):
        self.job("job-0001")
        self.sensors.t = 99
        self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("core temp 99C" in m for m in self.logged))
        self.sensors.t = 40
        self.load = 9.0                                                                          # a busy machine, whatever this computer is doing
        self.w.step()
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("load" in m for m in self.logged))

    def test_two_step_passes_never_overlap(self):
        with gitio.FileLock(self.state / "step.lock", wait=0) as held:
            self.assertTrue(held.got)
            self.job("job-0001")
            self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.w.step(), ["job-0001"])

    def test_a_job_already_running_is_not_started_twice(self):
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.w.step(), [])
        self.assertEqual(self.spawned, ["job-0001"])


class Watchdog(Base):
    def test_a_crashed_job_gets_an_error_result(self):
        self.job("job-0001")
        self.w.fetch()                                                                        # started; the fake process id is not alive
        self.w.reap()
        self.w.publish_pending()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "error")
        self.assertIn("crash or reboot", res["note"])

    def test_a_job_past_its_deadline_is_asked_to_stop_then_killed(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.make_worker(is_job_process=lambda pid, jid: pid == proc.pid and proc.poll() is None)
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": proc.pid, "weight": 1, "started": util.iso(), "deadline": time.time() - 5}))
        self.w.reap()
        self.assertIn("term_sent", json.loads(marker.read_text()))
        proc.wait(timeout=10)                                                                 # SIGTERM stopped it
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertTrue(any("exceeded its time limit" in m for m in self.logged))

    def test_a_job_that_ignores_sigterm_is_hard_killed(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.5)
        self.make_worker(is_job_process=lambda pid, jid: pid == proc.pid and proc.poll() is None)
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": proc.pid, "weight": 1, "started": util.iso(), "deadline": time.time() - 100, "term_sent": time.time() - 60}))
        self.w.reap()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(json.loads(marker.read_text())["watchdog"], "hard-killed")
        self.make_worker()                                                                     # now the process is gone
        self.w.reap()
        self.w.publish_pending()
        self.assertEqual(self.result("job-0009")["status"], "killed")

    def test_kill_job_only_signals_a_process_that_really_is_that_job(self):
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": os.getpid(), "weight": 1}))
        self.assertFalse(self.w.kill_job("job-0009"))                                          # is_job_process says no: our own pid is untouched
        self.assertFalse(self.w.kill_job("job-0000"))

    def test_results_that_could_not_be_pushed_are_kept_and_retried(self):
        self.job("job-0001")
        sh("git", "remote", "set-url", "origin", str(self.tmp / "nowhere.git"), cwd=self.tmp / "w-results")
        self.run_all()
        self.assertTrue((self.state / "finished" / "job-0001.json").exists())                 # kept locally
        self.assertFalse((self.state / "done" / "job-0001").exists())
        sh("git", "remote", "set-url", "origin", str(self.tmp / "results.git"), cwd=self.tmp / "w-results")
        self.w.publish_pending()
        self.assertEqual(self.result("job-0001")["status"], "ok")
        self.assertTrue((self.state / "done" / "job-0001").exists())


class Profile(Base):
    def profile_job(self, jid, **over):
        body = {"argv": ["ls", "-l"], "what": "lists files", "expect": "instant", "est_time_s": 1, "est_mem_mb": 10, "flags": {"-l": "long"}}
        return self.job(jid, "profile-run", **{**body, **over})

    def test_a_profile_run_always_waits_for_approval_and_shows_facts_and_numbers(self):
        self.profile_job("profile-aaa111")
        self.w.fetch()
        res = self.result("profile-aaa111")
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertIn("AT A GLANCE", res["glance"])
        self.assertIn("testct now", res["glance"])
        self.assertEqual(res["facts"]["binary"], "/usr/bin/ls")
        self.assertEqual(self.runner.calls, [])

    def test_after_approval_it_runs_and_the_verdict_drafts_a_registry_entry(self):
        self.profile_job("profile-aaa111")
        self.w.fetch()
        self.w.approve("profile-aaa111")
        self.w.fetch()
        self.w.execute("profile-aaa111")
        res = self.result("profile-aaa111")
        self.assertEqual(res["status"], "ok")
        self.assertEqual((res["profile"]["verdict"], res["profile"]["cost"]), ("fits", "light"))
        self.assertEqual(res["profile"]["entry"]["run"][1:], ["-l"])
        call = self.runner.calls[0]
        self.assertEqual(call["argv"][0], "/usr/bin/ls") if os.path.exists("/usr/bin/ls") else None
        self.assertEqual(call["kw"]["limits"]["max_rss_mb"], 4800)                              # 60% of the sensor's 8000 MB
        self.assertFalse(call["kw"]["network"])
        self.assertFalse((self.state / "work" / "profile-aaa111").exists())                    # a profiled command's leftovers are removed

    def test_a_command_that_is_not_installed_is_refused_before_anything_is_approved(self):
        self.profile_job("profile-aaa111", argv=["nosuchprogram"])
        self.w.fetch()
        self.assertEqual(self.result("profile-aaa111")["status"], "rejected")

    def test_privilege_tools_are_refused(self):
        self.profile_job("profile-aaa111", argv=["sudo", "id"], packages=["apt:sudo"])
        self.w.fetch()
        self.assertIn("not allowed", self.result("profile-aaa111")["note"])


class ProfileTime(Base):
    """A profile run's time limit follows the person's estimate; the owner's ceiling is only for a shared computer."""
    def worker_with(self, **over):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, **over)
        self.make_worker()

    def test_the_limit_is_the_estimate_times_the_margin_unless_the_computer_is_shared(self):
        self.worker_with(isolation_purpose="special-use")
        reg = self.w.load_registry()
        cls = reg["classes"]["profile-run"]
        self.assertEqual(self.w.time_limit(cls, {"est_time_s": 1700}, reg), 5100)
        self.worker_with(isolation_purpose="shared")
        reg = self.w.load_registry()
        self.assertEqual(self.w.time_limit(reg["classes"]["profile-run"], {"est_time_s": 1700}, reg), reg["classes"]["profile-run"].get("max_timeout_s", 1800))

    def test_the_estimate_is_kept_with_the_queued_job_so_the_deadline_follows_it(self):
        self.worker_with(isolation_purpose="special-use")
        self.job("profile-aaa111", "profile-run", argv=["ls"], what="w", expect="e", est_time_s=100, est_mem_mb=1)
        self.w.fetch()
        self.assertEqual(self.w.approve("profile-aaa111"), (True, ""))
        self.w.fetch()
        meta = json.loads((self.w._p("queued", "profile-aaa111.meta")).read_text())
        self.assertEqual(meta.get("est_time_s"), 100)

    def test_a_worker_that_was_measured_at_install_gets_a_longer_limit_and_publishes_why(self):
        from ct import calibration, calibration_reference
        self.worker_with(isolation_purpose="special-use")
        slow = {p: v * 2 for p, v in calibration_reference.REFERENCE.items()}
        calibration.store(self.w._p("calibration.json"), slow)
        reg = self.w.load_registry()
        self.assertEqual(self.w.speed(), (2.0, f"install, {len(slow)} of 4 steps"))
        self.assertEqual(self.w.time_limit(reg["classes"]["profile-run"], {"est_time_s": 100}, reg), 600)       # 100 s x 2.0 x margin 3
        self.assertTrue(self.w.publish_registry_info(reg))
        pt = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]
        self.assertEqual((pt["factor"], pt["basis"]), (2.0, f"install, {len(slow)} of 4 steps"))

    def test_the_published_registry_says_what_the_limit_is_built_from(self):
        self.worker_with(isolation_purpose="special-use")
        self.w.publish_registry_info(self.w.load_registry())
        pt = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]
        self.assertEqual((pt["ceiling_s"], pt["shared"], pt["factor"]), (None, False, 1.0))
        self.worker_with(isolation_purpose="shared")
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))                    # a changed purpose is republished
        self.assertEqual(json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]["shared"], True)


class RegistryInfo(Base):
    def test_a_classs_submit_recipe_is_published_for_the_client_and_never_acted_on(self):
        self.registry_path.write_text(REGISTRY + '  packed:\n    tier: auto\n    run: [/bin/true]\n    accepts_files: true\n    submit: {"dir": "src", "include": ["*.js"]}\n')
        self.w.publish_registry_info(self.w.load_registry())
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["classes"]["packed"]["submit"], {"dir": "src", "include": ["*.js"]})

    def test_the_installed_registry_is_published_once_and_again_when_it_changes(self):
        reg = self.w.load_registry()
        self.assertTrue(self.w.publish_registry_info(reg))
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["mem_total_mb"], 8000)
        self.assertIn("profile-run", info["classes"])
        self.assertEqual(info["classes"]["profile-run"]["tier"], "approve")
        self.assertFalse(info["job_network_isolation"])                                          # a NoJail was injected
        n = sh("git", "rev-list", "--count", "main", cwd=self.tmp / "results.git")
        self.assertFalse(self.w.publish_registry_info(reg))
        self.assertEqual(sh("git", "rev-list", "--count", "main", cwd=self.tmp / "results.git"), n)
        self.registry_path.write_text(REGISTRY + "  extra:\n    tier: auto\n    run: [/bin/true]\n")
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertIn("extra", json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["classes"])

    def test_each_class_carries_the_day_it_was_first_installed_and_keeps_it_when_another_is_added(self):
        shown = lambda: json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["classes"]          # noqa: E731
        self.w.publish_registry_info(self.w.load_registry())
        first = shown()["profile-run"]["installed"]
        self.assertRegex(first, r"^\d{4}-\d\d-\d\dT")
        self.w.clock = lambda: __import__("datetime").datetime(2100, 1, 1, tzinfo=__import__("datetime").timezone.utc)                                                  # a later day
        self.registry_path.write_text(REGISTRY + "  extra:\n    tier: auto\n    run: [/bin/true]\n")
        self.w.publish_registry_info(self.w.load_registry())
        later = shown()
        self.assertEqual(later["profile-run"]["installed"], first)                            # kept
        self.assertGreater(later["extra"]["installed"], first)                                # the new one is dated when it appeared

    def test_the_trusted_registry_is_the_installed_file_never_the_repo(self):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "jobs", "origin/main", cwd=req)
        (req / "registry.yaml").write_text("classes:\n  evil:\n    tier: auto\n    run: [/bin/sh, -c, id]\n")
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "evil", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.job("job-0001", "evil")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "rejected")
        self.assertEqual(self.runner.calls, [])


class Isolation(Base):
    def test_with_the_real_runner_a_no_network_job_is_refused_when_isolation_is_unavailable(self):
        marker = self.tmp / "ran"
        self.registry_path.write_text(f"classes:\n  touchy:\n    tier: auto\n    cost: light\n    run: [{sys.executable}, -c, \"open('{marker}', 'w')\"]\n")
        self.make_worker(runner=runner.run_command)                                              # the real runner; NoJail means "cannot isolate"
        self.job("job-0001", "touchy")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "error")
        self.assertIn("not started", res["note"])
        self.assertFalse(marker.exists())                                                         # the command never ran

    def test_with_the_real_runner_a_job_runs_and_its_output_comes_back(self):
        self.registry_path.write_text(f"classes:\n  real:\n    tier: auto\n    cost: light\n    run: [{sys.executable}, -c, \"print(6 * 7)\"]\n    network: true\n")
        self.make_worker(runner=runner.run_command, jail=jail_mod.Unshare(False))      # the older method lets a network class through even with no namespace
        self.job("job-0001", "real")
        self.run_all()
        self.assertEqual((self.result("job-0001")["status"], self.result("job-0001")["output"].strip()), ("ok", "42"))


class SeenTime(Base):
    def test_a_result_says_when_the_worker_first_saw_the_job(self):
        self.job("job-0001", "echo")
        self.run_all()
        res = self.result("job-0001")
        self.assertRegex(res.get("seen", ""), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertLessEqual(res["seen"], res["started"])


class Housekeeping(Base):
    def test_old_snapshots_work_folders_and_kept_output_are_cleaned_but_unfinished_work_is_kept(self):
        old = time.time() - 10 * 86400
        (self.state / "inbox" / "job-done1.json").write_text("{}")
        (self.state / "done" / "job-done1").write_text("")
        (self.state / "inbox" / "job-wait1.json").write_text("{}")
        (self.state / "work" / "job-old11").mkdir()
        (self.state / "work" / "job-run11").mkdir()
        (self.state / "running" / "job-run11").write_text("{}")
        for p in (self.state / "inbox" / "job-done1.json", self.state / "inbox" / "job-wait1.json", self.state / "work" / "job-old11", self.state / "work" / "job-run11"):
            os.utime(p, (old, old))
        self.w.clean_work()
        self.assertFalse((self.state / "inbox" / "job-done1.json").exists())
        self.assertTrue((self.state / "inbox" / "job-wait1.json").exists())
        self.assertFalse((self.state / "work" / "job-old11").exists())
        self.assertTrue((self.state / "work" / "job-run11").exists())

    def test_results_history_is_pruned_at_most_once_a_day(self):
        self.settings.prune_threshold = 0
        self.assertIsNotNone(self.w.maybe_prune_results())
        self.assertIsNone(self.w.maybe_prune_results())

    def test_the_decoys_are_planted_under_the_configured_identity(self):
        self.settings.identity = "myinstall"
        self.w.fetch()
        self.assertTrue((self.home / ".git-credentials").read_text().startswith("https://myinstall:"))


class ToolFolders(Base):
    """A class may name folders of tools that already exist on the computer (Special Use). The worker enforces the owner's allowed folders at EVERY run, whatever the registry said."""

    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "existing"
        (self.root / "nmap" / "bin").mkdir(parents=True)
        (self.root / "extra").mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.use(["%s/nmap/bin" % self.root, "%s/extra" % self.root], mode="relaxed", roots=(str(self.root),))

    def use(self, tools, mode, roots):
        self.registry_path.write_text(REGISTRY + "  probe:\n    tier: auto\n    cost: light\n    run: [nmap-wrapper]\n    tools: [%s]\n" % ", ".join(tools))
        self.settings.isolation_mode, self.settings.tool_roots = mode, roots
        self.make_worker()

    def run_probe(self):
        self.job("job-0001", "probe")
        self.run_all()
        return self.result("job-0001")

    def test_the_folders_come_first_on_the_jobs_path_in_the_order_given(self):
        res = self.run_probe()
        self.assertEqual(res["status"], "ok", res)
        path = self.runner.calls[0]["kw"]["extra_env"]["PATH"].split(":")
        self.assertEqual(path, [str(self.root.resolve() / "nmap" / "bin"), str(self.root.resolve() / "extra")])

    def test_a_worker_in_full_isolation_refuses_and_says_how_the_owner_changes_it(self):
        self.use(["%s/nmap/bin" % self.root], mode="full", roots=())
        res = self.run_probe()
        self.assertEqual(res["status"], "error")
        self.assertIn("full isolation", res["note"])
        self.assertIn("isolation.tool_roots", res["note"])
        self.assertEqual(self.runner.calls, [])                                           # nothing ran

    def test_a_missing_folder_and_a_link_that_leaves_the_allowed_folders_are_refused(self):
        self.use(["%s/gone" % self.root], mode="relaxed", roots=(str(self.root),))
        self.assertIn("does not exist", self.run_probe()["note"])
        (self.root / "sneaky").symlink_to(self.outside)                                   # inside the root by name, outside it once followed
        self.use(["%s/sneaky" % self.root], mode="relaxed", roots=(str(self.root),))
        self.job("job-0002", "probe")
        self.run_all()
        self.assertIn("leads outside", self.result("job-0002")["note"])
        self.assertEqual(self.runner.calls, [])

    def test_the_published_registry_and_state_say_which_class_uses_which_folders_and_the_mode(self):
        self.w.publish_registry_info(self.w.load_registry())
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["classes"]["probe"]["tools"], ["%s/nmap/bin" % self.root, "%s/extra" % self.root])
        from ct import workerstate
        env, _ = workerstate.build(snapshot={}, registry=self.w.load_registry(), extra={"isolation_mode": "relaxed", "tool_roots": [str(self.root)]}, jail=jail_mod.NoJail(), locked=False, now_iso="x")
        self.assertEqual((env["isolation"]["mode"], env["isolation"]["tool_roots"]), ("relaxed", [str(self.root)]))


class SharedComputer(Base):
    """A computer other people use: no new job while a person is logged in (a running one finishes), or outside its working hours; the worker says why."""

    def setUp(self):
        super().setUp()
        from datetime import datetime, timezone
        self.noon = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    def hold(self, fixed_clock=False, **kw):
        for k, v in kw.items():
            setattr(self.settings, k, v)
        self.make_worker(**({"clock": lambda: self.noon} if fixed_clock else {}))                       # a fixed clock only where the hours matter: jobs are dated by the real one

    def test_nothing_changes_unless_the_owner_asks_for_it(self):
        self.sensors.people = ["alice"]
        self.assertIsNone(self.w.busy_reason())                                         # the default: a computer of its own

    def test_a_person_logged_in_holds_new_jobs_and_the_owners_own_account_does_not_count(self):
        self.hold(pause_when_logged_in=True, ignore_users=("agent",))
        self.sensors.people = ["agent"]
        self.assertIsNone(self.w.busy_reason())
        self.sensors.people = ["agent", "alice", "bob"]
        self.assertEqual(self.w.busy_reason(), "a person is using this computer (alice, bob)")
        self.assertEqual(self.w.snapshot()["held"], "a person is using this computer (alice, bob)")          # published, so the scheduler and the cards can say it
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.spawned, [])                                                # queued, not started
        self.sensors.people = []
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                      # started as soon as nobody is there

    def test_a_working_window_holds_jobs_outside_it_including_one_that_crosses_midnight(self):
        import re
        local = self.noon.astimezone().strftime("%H:%M")
        self.hold(True, window=f"{local}-23:59" if local < "23:59" else "00:00-00:01")
        self.assertIsNone(self.w.busy_reason())                                           # now is inside
        h = int(local[:2])
        outside_start = f"{(h + 2) % 24:02d}:00"
        outside_end = f"{(h + 4) % 24:02d}:00"
        self.hold(True, window=f"{outside_start}-{outside_end}")
        self.assertRegex(self.w.busy_reason(), r"outside its working hours")
        crossing = f"{(h + 2) % 24:02d}:00-{(h + 1) % 24:02d}:00"                       # all day except the hour from now to now+1: now is inside a window that crosses midnight
        self.hold(True, window=crossing)
        self.assertIsNone(self.w.busy_reason())


if __name__ == "__main__":
    unittest.main()

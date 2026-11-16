"""The whole lifecycle, end to end: a requester pushes a job file to a (bare) queue repo, the worker fetches, validates, holds for approval,
queues, runs, guards and publishes a result to a (bare) results repo. Only the command runner and the process spawner are faked."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from cw import gitio, guard, runner, util  # noqa: E402
from support import REGISTRY, IP, Base, sh  # noqa: E402
from cw import jail as jail_mod  # noqa: E402


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


class Rejections(Base):
    def rejected(self, jid, needle):
        self.run_all()
        res = self.result(jid)
        self.assertEqual(res["status"], "rejected", res)
        self.assertIn(needle, res["note"])
        self.assertEqual(self.runner.calls, [])                                            # nothing ran

    def test_unknown_class(self):
        self.job("job-0001", "nope")
        self.rejected("job-0001", "class not in installed registry")

    def test_bad_parameters_never_reach_a_command(self):
        self.job("job-0001", "echo", params={"word": "x; rm -rf /"})
        self.rejected("job-0001", "does not match the allowed pattern")

    def test_a_replayed_old_job_is_refused(self):
        self.job("job-0001", created=util.iso(util.now() - timedelta(days=3)))
        self.rejected("job-0001", "too old")

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
        self.assertIn("cw-admin approve job-0001", res["note"])
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
        self.assertEqual(self.result("job-0001")["note"], "cancelled on testcw before it started")
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
        self.assertEqual([r.get_header("Title") for r in self.pushes], ["testcw QUARANTINE", "testcw LOCKDOWN"])
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
        self.assertEqual([r.get_header("Title") for r in self.pushes][0], "testcw BREACH")

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
        self.assertIn("testcw now", res["glance"])
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


class RegistryInfo(Base):
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


if __name__ == "__main__":
    unittest.main()

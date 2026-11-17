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


if __name__ == "__main__":
    unittest.main()

"""Part 2 of 2 of the test_worker_approvals tests (ProfileWithoutApproval, Approval)."""
import unittest
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import jail as jail_mod  # noqa: E402
from support import Base, sh  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

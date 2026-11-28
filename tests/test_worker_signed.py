"""arm-worker: signed approvals, privileged jobs and signed orders."""
import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from armada import approvals  # noqa: E402
from support import REGISTRY, IP, Base, sh, ssh_key  # noqa: E402


class SignedApprovalFixture(Base):
    """Approving from the administrator's own computer: a signed approval is published next to the job, and the worker checks it against its approver key."""

    def setUp(self):
        super().setUp()
        self.keys = Path(tempfile.mkdtemp())
        self.pub = self.keygen("key")
        self.signers = self.keys / "approvers"
        self.signers.write_text(approvals.with_approver("", self.pub))
        self.make_worker(signed_approval=self.verifier, approvers_file=self.signers)

    def keygen(self, name):
        ssh_key(self.keys / name, name)
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


class SignedApprovalFixture(SignedApprovalFixture):
    pass


class SignedApproval(SignedApprovalFixture):
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


class PrivilegedJobs(SignedApprovalFixture):
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


class SignedOrders(SignedApprovalFixture):
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


if __name__ == "__main__":
    unittest.main()

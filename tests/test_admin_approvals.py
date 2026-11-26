"""ct-admin: approving with a signed key, signed orders, clean, and the signed registry."""
import re
import json
import subprocess

from admin_support import AdminBase, REGISTRY, ssh_key  # noqa: F401  (also points HOME at a throwaway folder)

from ct import approvals
from ct.admin import jobs


class SignedApprovalsFixture(AdminBase):
    """Approving here: signed with a key on this computer and published; nothing is run on the worker."""

    template_kind = "job-b"                # the registry and job-b are already in the queue (built once per process)

    def setUp(self):
        super().setUp()
        keydir = self.home / "approval"
        keydir.mkdir(parents=True)
        ssh_key(keydir / "key")
        self.pub = (keydir / "key.pub").read_text()
        self.signers = self.tmp / "approvers"
        self.signers.write_text(approvals.with_approver("", self.pub))
        self.digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-b"))
        self.tty = True

    def worker_publishes(self, **info):
        (self.repos.results / "registry.json").write_text(json.dumps(info))

    def published(self, jid):
        self.repos.fetch_queue()
        return self.repos.show_file(f"origin/{approvals.BRANCH}", approvals.path_in_repo(jid))


class SignedApprovals(SignedApprovalsFixture):
    def test_the_approval_is_signed_here_pinned_to_the_reviewed_file_and_nothing_runs_on_the_worker(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-b")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [])                                           # no login, no sudo
        text = self.published("job-b")
        self.assertTrue(approvals.check(text, "job-b", self.digest, self.signers)[0])
        self.assertFalse(approvals.check(text, "job-b", "0" * 64, self.signers)[0])
        self.assertIn("No login", out + err)

    def test_approve_all_signs_every_viewed_job(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.put_result("job-b", status="awaiting_approval")
        self.admin("review")
        self.answers.append("y")
        code, _, _ = self.admin("approve", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [])
        self.assertTrue(approvals.check(self.published("job-b"), "job-b", self.digest, self.signers)[0])

    def test_a_worker_that_trusts_a_different_key_is_an_error_not_a_silent_fallback(self):
        self.worker_publishes(approvers=["SHA256:somebodyelse"])
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-b")
        self.assertNotEqual(code, 0)
        self.assertIn("does not trust your approval key", out + err)
        self.assertIsNone(self.published("job-b"))

    def test_ssh_forces_the_old_way(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.answers.append("y")
        self.admin("approve", "job-b", "--ssh")
        self.assertEqual(self.ssh.calls, [f"ct-worker approve job-b --sha256 {self.digest}"])
        self.assertIsNone(self.published("job-b"))

    def test_a_worker_that_does_not_say_which_keys_it_trusts_gets_the_old_way(self):
        self.worker_publishes(capacity=4)                                               # an older release
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.assertEqual(len(self.ssh.calls), 1)

    def test_without_an_approval_key_nothing_changes(self):
        (self.home / "approval" / "key").unlink()
        self.worker_publishes(approvers=[])
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.assertEqual(len(self.ssh.calls), 1)


class SignedOrders(SignedApprovalsFixture):
    """ct-admin unlock, cancel and kill: signed here and published; the worker carries them out, no login."""

    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))

    def orders(self):
        self.repos.fetch_queue()
        out = subprocess.run(["git", "-C", str(self.repos.queue), "ls-tree", "--name-only", f"origin/{approvals.BRANCH}", "control/"], capture_output=True, text=True).stdout
        return [self.repos.show_file(f"origin/{approvals.BRANCH}", n) for n in out.split()]

    def test_cancel_and_kill_publish_signed_orders_naming_the_job_and_log_in_nowhere(self):
        self.assertEqual(self.admin("cancel", "job-b")[0], 0)
        self.assertEqual(self.admin("kill", "job-b", "--hard")[0], 0)
        self.assertEqual(self.ssh.calls, [])
        recs = [approvals.check_control(t, self.signers) for t in self.orders()]
        self.assertTrue(all(ok for ok, _, _ in recs))
        self.assertEqual(sorted((r["action"], r["job"], bool(r.get("hard"))) for _, _, r in recs), [("cancel", "job-b", False), ("kill", "job-b", True)])

    def test_unlock_shows_what_happened_asks_and_signs_for_that_exact_lockdown(self):
        self.repos.pull_results = lambda: {"kind": "quarantine", "job": "job-x", "since": "2026-11-16T10:00:00Z"}
        self.answers.append("y")
        code, out, err = self.admin("unlock")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [])
        (text,) = self.orders()
        ok, why, rec = approvals.check_control(text, self.signers)
        self.assertEqual((ok, rec["action"], rec["lock"]), (True, "unlock", "2026-11-16T10:00:00Z"))      # an old order cannot clear a later lockdown

    def test_answering_no_to_unlock_signs_nothing(self):
        self.repos.pull_results = lambda: {"kind": "quarantine", "job": "job-x", "since": "t"}
        self.answers.append("n")
        code, _, _ = self.admin("unlock")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.orders(), [])

    def test_unlock_when_the_worker_says_it_is_not_locked_signs_nothing(self):
        code, out, err = self.admin("unlock")
        self.assertEqual(code, 0)
        self.assertEqual(self.orders(), [])
        self.assertIn("Not locked", out + err)

    def test_ssh_forces_the_old_way_and_an_older_worker_gets_it_automatically(self):
        self.admin("cancel", "job-b", "--ssh")
        self.assertEqual(self.ssh.calls, ["ct-worker cancel job-b"])
        self.worker_publishes(capacity=4)
        self.admin("unlock")
        self.assertEqual(self.ssh.calls[-1], "ct-worker unlock")


class CleanCommand(SignedApprovalsFixture):
    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))

    def names(self):
        self.repos.fetch_queue()
        return subprocess.run(["git", "-C", str(self.repos.queue), "ls-tree", "-r", "--name-only", f"origin/{approvals.BRANCH}"], capture_output=True, text=True).stdout.split()

    def test_an_approval_for_a_finished_job_goes_and_one_for_a_waiting_job_stays(self):
        self.put_job("job-w")
        self.answers.extend(["y", "y"])
        self.admin("approve", "job-b")
        self.admin("approve", "job-w")
        self.put_result("job-b", status="ok")
        self.put_result("job-w", status="awaiting_approval")
        self.answers.append("y")
        code, out, err = self.admin("clean")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names(), ["approvals/job-w.json"])

    def test_the_signed_registry_is_never_removed(self):
        self.admin("proposals", "publish")
        self.admin("cancel", "job-b")                                                  # an order, fresh: it stays too
        self.answers.append("y")
        self.admin("clean")
        self.assertIn(approvals.REGISTRY_PATH, self.names())
        self.assertTrue(any(n.startswith("control/") for n in self.names()))

    def test_an_expired_order_is_removed(self):
        record = approvals.make_control_record("cancel", job="job-b", now=1000, nonce="aaaa0000")
        text = approvals.bundle(record, approvals.sign(record, self.home / "approval" / "key", namespace=approvals.CONTROL_NAMESPACE))
        self.repos.push_files(approvals.BRANCH, {"control/aaaa0000.json": text}, "an old order")
        self.answers.append("y")
        self.admin("clean")
        self.assertEqual(self.names(), [])

    def test_answering_no_removes_nothing_and_an_empty_branch_says_so(self):
        self.assertIn("Nothing to tidy", "".join(self.admin("clean")[1:]))
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.put_result("job-b", status="ok")
        self.answers.append("n")
        self.admin("clean")
        self.assertEqual(self.names(), ["approvals/job-b.json"])


class SignedRegistry(SignedApprovalsFixture):
    """Adding or removing a class from this computer: the registry on main is signed here and the worker installs it; no SSH, no sudo."""

    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None                      # `retire` in a terminal waits for the worker to show the class gone: never really wait in a test

    def trusted(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()), classes={"careful": {}})

    def signed_registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file(f"origin/{approvals.BRANCH}", approvals.REGISTRY_PATH)

    def test_retire_signs_the_registry_here_and_installs_nothing_over_ssh(self):
        self.trusted()
        code, out, err = self.admin("retire", "careful", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [])                                           # no sudo ct-update, no login
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertNotIn("  careful:", text)
        ok, why, rec = approvals.check_registry(self.signed_registry(), text, self.signers)
        self.assertTrue(ok, why)                                                       # what is signed is exactly what is now on main

    def test_the_signature_is_for_registry_changes_only_and_names_the_exact_text(self):
        self.trusted()
        self.admin("retire", "careful", "--yes")
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertFalse(approvals.check_registry(self.signed_registry(), text + "# tampered\n", self.signers)[0])
        doc = json.loads(self.signed_registry())
        self.assertFalse(approvals.verify(doc["record"], doc["signature"], self.signers))            # not accepted as the approval of a job

    def test_publish_signs_what_is_on_main_now(self):
        self.trusted()
        code, _, err = self.admin("proposals", "publish")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.signed_registry())
        self.assertEqual(self.ssh.calls, [])

    def test_merging_a_registry_proposal_publishes_it_when_the_worker_trusts_this_key(self):
        self.trusted()
        self.repos.push_files("registry/hello", {"registry.yaml": REGISTRY + "  hello:\n    tier: auto\n    cost: light\n    run: [/bin/echo, hi]\n"}, "registry: hello", base="main")
        self.answers.append("y")
        code, _, err = self.admin("proposals", "merge", "registry/hello")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.signed_registry())

    def test_a_worker_that_does_not_trust_this_key_is_told_so_and_nothing_is_signed(self):
        self.worker_publishes(approvers=["SHA256:somebodyelse"])
        code, out, err = self.admin("retire", "careful", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("does not trust your approval key", out + err)
        self.assertIsNone(self.signed_registry())

    def test_without_a_trusted_key_the_old_ssh_install_is_used(self):
        self.worker_publishes(capacity=4)
        self.health(self.rel[0])
        self.admin("retire", "careful", "--yes")
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))

    def health(self, rev):
        self.ssh.answers["systemctl"] = f"timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={rev}\n"


class BeforeApprovingAToolClass(AdminBase):
    """Which tool a class with tool folders would really run is said before the person signs, and in `class show`."""
    INFO = {"kind": "command", "cost": "light", "tier": "approve", "run": ["scan"], "tools": ["/opt/tools/a", "/opt/tools/b"]}
    CHECK = {"program": "scan", "found": "/opt/tools/a/scan", "where": "tool folder",
             "folders": [{"path": "/opt/tools/a", "exists": True, "allowed": True}, {"path": "/opt/tools/b", "exists": False, "allowed": True}]}

    def text_of(self, card):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(" ".join(str(x) for x in d) if isinstance(d, tuple) else str(d) for d in card["details"]))

    def test_the_review_card_names_the_folders_the_program_found_and_each_folders_state(self):
        from ct.admin import jobs
        card = jobs.review_card({"id": "scan-abc123", "class": "scan"}, None, "lab", info={**self.INFO, "tool_check": self.CHECK}, digest="a" * 64)
        text = self.text_of(card)
        for needle in ("Tool folders /opt/tools/a", "/opt/tools/b", "Found /opt/tools/a/scan  (in a tool folder)", "/opt/tools/b  missing on the worker"):
            self.assertIn(needle, text)

    def test_a_program_that_is_found_nowhere_is_said_loudly_and_an_unknown_state_is_not_invented(self):
        from ct.admin import jobs
        none = jobs.tool_check_rows({**self.CHECK, "found": None, "where": None}, ["/opt/tools/a"])
        self.assertTrue(any("NOT FOUND" in str(v) for _, v in none))
        unknown = jobs.tool_check_rows(None, ["/opt/tools/a"])
        self.assertTrue(any("not known yet" in str(v) for _, v in unknown))

    def test_class_show_reads_it_from_the_workers_published_state(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"updated": "2999-01-01T00:00:00Z", "isolation": {"mode": "relaxed", "tool_roots": ["/opt/tools"]}, "tool_check": {"careful": self.CHECK}}))
        self.admin("class", "set", "careful", "--tools", "/opt/tools/a", "/opt/tools/b", "--yes")
        self.repos.registry_info = lambda: {"classes": {"careful": self.INFO}}
        code, out, err = self.admin("class", "show", "careful")
        self.assertEqual(code, 0, err)
        for needle in ("Program", "scan", "/opt/tools/a/scan", "in a tool folder", "missing on the worker"):
            self.assertIn(needle, out + err)


if __name__ == "__main__":
    unittest.main()

"""F4: the worker's own record of every job the Direct mailbox key sent (src/ct/received.py, `ct-worker received`, `ct-admin received`)."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct import cli, received, render, worker  # noqa: E402
from support import Base, sh  # noqa: E402
from test_worker_approvals_b import ProfileWithoutApproval as PWA  # noqa: E402


class Ctx:
    def __init__(self, w):
        self.w = w


class DirectBase(Base):
    def setUp(self):
        super().setUp()
        self.settings.transport = "direct"
        self.make_worker()

    def entries(self):
        return self.w.received.read(100)


class RecordsEveryJobThatArrives(DirectBase):
    def test_a_queued_job_is_recorded_with_its_class_digest_and_size_and_no_contents(self):
        self.job("job-0001", "echo")
        self.run_all()
        [e] = self.entries()
        self.assertEqual((e["id"], e["class"], e["kind"], e["outcome"]), ("job-0001", "echo", "command", "queued"))
        raw = (self.tmp / "requester" / "jobs" / "job-0001.json").read_bytes()
        import hashlib
        self.assertEqual((e["sha256"], e["bytes"]), (hashlib.sha256(raw).hexdigest(), len(raw)))
        self.assertNotIn("hi", (self.state / received.NAME).read_text())              # the parameter value is not in the record

    def test_a_refused_job_is_recorded_with_its_refusal_code(self):
        self.job("job-0001", "nope")
        self.run_all()
        [e] = self.entries()
        self.assertEqual((e["outcome"], e["class"]), ("rejected", "nope"))
        self.assertTrue(e["refusal"], e)

    def test_a_job_waiting_for_approval_is_recorded_once_however_often_it_is_looked_at(self):
        self.job("job-0001", "echo")
        reg = self.registry_path
        reg.write_text(reg.read_text().replace("tier: auto", "tier: approve"))
        for _ in range(3):
            self.run_all()
        outcomes = [e["outcome"] for e in self.entries() if e["id"] == "job-0001"]
        self.assertEqual(outcomes, ["awaiting_approval"])

    def test_a_job_that_is_not_json_is_still_recorded(self):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "jobs", "origin/main", cwd=req)
        (req / "jobs").mkdir(exist_ok=True)
        (req / "jobs" / "job-0001.json").write_text("{ nope")
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "x", cwd=req)                              # (support.sh carries a committer identity: a CI machine has none)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.run_all()
        [e] = self.entries()
        self.assertEqual((e["id"], e["outcome"], e["class"]), ("job-0001", "rejected", None))

    def test_a_record_that_cannot_be_written_never_stops_the_job(self):
        (self.state / received.NAME).mkdir()                                            # a directory where the file should be: every write fails
        self.job("job-0001", "echo")
        self.run_all()
        self.assertNotEqual(self.result("job-0001")["status"], "error")
        self.assertTrue(any("could not write the record" in m for m in self.logged), self.logged)

    def test_a_worker_on_github_keeps_no_record(self):
        self.settings.transport = "github"
        self.make_worker()
        self.assertIsNone(self.w.received)
        self.job("job-0001", "echo")
        self.run_all()
        self.assertFalse((self.state / received.NAME).exists())


class TheRecordSaysWhenNoOneApproved(DirectBase):
    def settle(self, approval, jail, jid):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, profile_approval=approval, transport="direct")
        self.make_worker(jail=jail)
        self.job(jid, "profile-run", **PWA.JOB)
        self.run_all()
        return [e["outcome"] for e in self.entries() if e["id"] == jid]

    def test_a_profile_run_that_started_with_no_approval_is_marked_so(self):
        self.assertEqual(self.settle("none", PWA.Jail(ready=True, isolates=True), "profile-aaa111"), ["started_unapproved"])

    def test_a_profile_run_that_waits_for_approval_is_marked_so(self):
        self.assertEqual(self.settle("required", PWA.Jail(ready=True, isolates=True), "profile-bbb222"), ["awaiting_approval"])


class OneKeyPerWorker(unittest.TestCase):
    def test_each_worker_has_its_own_mailbox_key_path_so_a_stolen_key_reaches_one_worker(self):
        from ct import config, transport
        a, b = (config.validate({"transport": {"kind": "direct"}, "paths": {"client_home": f"/h/workers/{n}"}})[0] for n in ("w1", "w2"))
        ka, kb = (transport.Direct().client_key(config.Config(c) if hasattr(config, "Config") else _Cfg(c)) for c in (a, b))
        self.assertNotEqual(ka, kb)
        self.assertEqual((ka.name, ka.parent.name), ("key", "mailbox"))


class _Cfg:
    def __init__(self, clean):
        self.paths = type("P", (), clean["paths"])()


class TheRecordIsCappedAndReadable(unittest.TestCase):
    def test_the_file_rolls_over_and_both_halves_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            r = received.Received(d)
            received.CAP_BYTES, old = 300, received.CAP_BYTES
            try:
                for i in range(12):
                    r.record(f"job-{i:04d}", b"{}", "queued", cls="echo", kind="command")
            finally:
                received.CAP_BYTES = old
            self.assertTrue((Path(d) / (received.NAME + ".1")).exists())
            self.assertEqual(r.read(3)[-1]["id"], "job-0011")

    def test_a_restarted_worker_does_not_record_a_job_twice(self):
        with tempfile.TemporaryDirectory() as d:
            received.Received(d).record("job-0001", b"{}", "queued")
            received.Received(d).record("job-0001", b"{}", "queued")
            self.assertEqual(len(received.Received(d).read()), 1)


class TheCommands(DirectBase):
    def show(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.cmd_received(Ctx(self.w), **kw)
        return code, out.getvalue()

    def test_the_command_lists_what_the_mailbox_key_sent(self):
        self.job("job-0001", "echo")
        self.job("job-0002", "nope")
        self.run_all()
        code, text = self.show()
        self.assertEqual(code, 0)
        for word in ("job-0001", "job-0002", "queued", "refused", "rotate-key"):
            self.assertIn(word, text)
        print("\n" + text)

    def test_an_empty_record_says_so_and_a_github_worker_says_why(self):
        self.assertIn("No job has come through the mailbox", self.show()[1])
        self.settings.transport = "github"
        self.make_worker()
        self.assertIn("does not use the Direct mailbox", self.show()[1])

    def test_the_admin_may_read_it_without_a_password(self):
        self.assertIn("received", render.READ_ONLY)
        self.assertIn("received --last *", render.READ_ONLY)


if __name__ == "__main__":
    unittest.main()

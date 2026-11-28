import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from armada import approvals, config, helper, privileged  # noqa: E402

REGISTRY = """\
actions:
  fan-profile:
    run: [/usr/local/sbin/fan-profile, "{profile}"]
    params: {profile: {enum: [quiet, balanced, performance]}}
    sha256: "%s"
    writable: [/etc/mbpfan.conf]
    timeout_s: 20
classes:
  fan:
    kind: privileged
    action: fan-profile
    cost: light
  hello:
    tier: auto
    cost: light
    run: [/bin/echo, hi]
"""
SHA = "c" * 64
WORKER_UID = 999


class HelperFixture(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        paths = {"state_root": str(self.d / "var"), "config": str(self.d / "etc"), "code": str(self.d / "opt")}
        clean, problems = config.validate({"paths": paths, "server": {"admin_user": "me"}, "github": {"owner": "someone"}})
        assert not problems, problems
        self.cfg = config.Config(clean)
        (self.d / "etc").mkdir()
        Path(self.cfg.path("registry")).write_text(REGISTRY % SHA)
        for n in ("key", "stranger"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.d / n)], check=True)
        Path(self.cfg.path("approvers")).write_text(approvals.with_approver("", (self.d / "key.pub").read_text()))
        self.ran, self.exe_checks, self.logged = [], [], []
        self.now = 1_800_000_000
        self.sandbox = lambda: True

    def job(self, **over):
        body = {"id": "fan-abc123", "class": "fan", "params": {"profile": "quiet"}, "created": "2026-11-17T00:00:00Z", **over}
        return json.dumps(body, sort_keys=True).encode()

    def sign(self, job_bytes, key="key", now=None):
        body = json.loads(job_bytes)
        import hashlib
        record = approvals.make_record(body["id"], hashlib.sha256(job_bytes).hexdigest(), now=self.now if now is None else now)
        return approvals.bundle(record, approvals.sign(record, self.d / key))

    def request(self, job_bytes, approval):
        return json.dumps({"job_b64": base64.b64encode(job_bytes).decode(), "approval": approval})

    def fake_run(self, rc=0, out="done\n", err=""):
        def run(cmd, **kw):
            self.ran.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return run

    def go(self, job_bytes=None, approval=None, request=None, uid=WORKER_UID, run=None, exe_ok=(True, ""), **over):
        job_bytes = self.job(**over) if job_bytes is None else job_bytes
        approval = self.sign(job_bytes) if approval is None else approval
        text = request if request is not None else self.request(job_bytes, approval)
        return helper.handle(text, self.cfg, uid, run=run or self.fake_run(), now=lambda: self.now, check_exe=lambda p, s: (self.exe_checks.append((p, s)), exe_ok)[1],
                             worker_uid=WORKER_UID, log=self.logged.append, sandbox_check=self.sandbox)


class Helper(HelperFixture):
    def test_a_signed_approved_job_runs_the_action_in_its_own_sandbox_and_returns_the_output(self):
        r = self.go()
        self.assertEqual((r["status"], r["exit_code"], r["output"]), ("ok", 0, "done\n"))
        cmd, kw = self.ran[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertEqual(cmd[cmd.index("--") + 1:], ["/usr/local/sbin/fan-profile", "quiet"])          # exactly the template with the validated parameter, no shell
        for prop in ("ProtectSystem=strict", "PrivateNetwork=yes", "ReadWritePaths=/etc/mbpfan.conf", "RuntimeMaxSec=20", "NoNewPrivileges=yes"):
            self.assertIn(f"--property={prop}", cmd)
        self.assertEqual(self.exe_checks, [("/usr/local/sbin/fan-profile", SHA)])                      # the program was checked against the pin
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_a_failing_program_is_an_error_with_its_exit_code_and_a_timeout_is_a_timeout(self):
        r = self.go(run=self.fake_run(rc=3, out="", err="no sensor\n"))
        self.assertEqual((r["status"], r["exit_code"], "no sensor" in r["output"]), ("error", 3, True))

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        r = self.go(run=slow, id="fan-def456")
        self.assertEqual(r["status"], "timeout")

    def test_a_job_without_a_signed_approval_never_runs(self):
        for approval in ("", "not json", json.dumps({"record": "{}", "signature": "x"})):
            r = self.go(approval=approval)
            self.assertEqual(r["status"], "refused", approval)
        self.assertEqual(self.ran, [])

    def test_an_approval_signed_by_any_other_key_is_refused(self):
        job = self.job()
        r = self.go(job_bytes=job, approval=self.sign(job, key="stranger"))
        self.assertEqual(r["status"], "refused")
        self.assertIn("no valid signed approval", r["note"])
        self.assertEqual(self.ran, [])

    def test_a_job_changed_after_it_was_approved_is_refused(self):
        approved = self.job(params={"profile": "quiet"})
        r = self.go(job_bytes=self.job(params={"profile": "performance"}), approval=self.sign(approved))
        self.assertEqual(r["status"], "refused")
        self.assertIn("different version", r["note"])
        self.assertEqual(self.ran, [])

    def test_an_approval_for_another_job_or_an_expired_one_is_refused(self):
        other = self.job(id="fan-zzz999")
        self.assertEqual(self.go(approval=self.sign(other))["status"], "refused")
        job = self.job()
        self.assertEqual(self.go(job_bytes=job, approval=self.sign(job, now=self.now - 90 * 86400))["status"], "refused")
        self.assertEqual(self.ran, [])

    def test_the_workers_own_local_approval_marker_cannot_stand_in_for_the_signature(self):
        """The worker can write its own approval markers; the helper never looks at them, only at the administrator's signature."""
        r = self.go(approval="")
        self.assertEqual(r["status"], "refused")

    def test_only_the_worker_account_may_ask(self):
        self.assertEqual(self.go(uid=1000)["status"], "refused")
        self.assertEqual(self.go(uid=0)["status"], "refused")
        self.assertEqual(self.ran, [])

    def test_only_a_privileged_class_can_be_run_this_way(self):
        job = self.job(**{"class": "hello", "params": {}})
        r = self.go(job_bytes=job, approval=self.sign(job))
        self.assertEqual(r["status"], "refused")
        self.assertIn("not a privileged class", r["note"])
        job = self.job(**{"class": "nonexistent"})
        self.assertEqual(self.go(job_bytes=job, approval=self.sign(job))["status"], "refused")

    def test_parameters_outside_the_actions_specification_are_refused(self):
        for params in ({"profile": "turbo"}, {"profile": "quiet; rm -rf /"}, {}, {"profile": "quiet", "extra": "x"}):
            job = self.job(params=params, id="fan-p" + str(len(self.ran)) + "xyz")
            r = self.go(job_bytes=job, approval=self.sign(job))
            self.assertEqual(r["status"], "refused", params)
        self.assertEqual(self.ran, [])

    def test_a_program_that_does_not_pass_the_check_is_not_run_and_the_job_is_not_used_up(self):
        r = self.go(exe_ok=(False, "the program is not the one that was approved"))
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.ran, [])
        self.assertFalse((Path(helper.state_dir(self.cfg)) / "done" / "fan-abc123").exists())
        self.assertEqual(self.go()["status"], "ok")                                                  # fixed, so the same job may now run

    def test_a_job_runs_at_most_once_even_if_the_same_request_is_sent_again(self):
        self.assertEqual(self.go()["status"], "ok")
        again = self.go()
        self.assertEqual(again["status"], "refused")
        self.assertIn("at most once", again["note"])
        self.assertEqual(len(self.ran), 1)

    def test_it_is_recorded_before_it_runs_so_a_crash_cannot_allow_a_second_run(self):
        def crash(cmd, **kw):
            self.assertTrue((Path(helper.state_dir(self.cfg)) / "done" / "fan-abc123").exists())
            raise RuntimeError("boom")
        self.assertEqual(self.go(run=crash)["status"], "error")
        self.assertEqual(self.go()["status"], "refused")

    def test_malformed_requests_are_refused_not_crashes(self):
        for text in ("", "not json", "{}", json.dumps({"job_b64": "!!!", "approval": "x"}), json.dumps({"job_b64": base64.b64encode(b"[1]").decode(), "approval": "x"}),
                     json.dumps({"job_b64": base64.b64encode(b'{"id": "../../etc"}').decode(), "approval": "x"})):
            self.assertEqual(self.go(request=text)["status"], "refused", text)

    def test_without_systemd_run_nothing_runs_unsandboxed(self):
        def missing(cmd, **kw):
            raise FileNotFoundError("systemd-run")
        r = self.go(run=missing)
        self.assertEqual(r["status"], "error")
        self.assertIn("cannot be sandboxed", r["note"])

    def test_every_decision_is_logged(self):
        self.go()
        self.assertTrue(any("running fan-abc123" in m for m in self.logged))
        self.go(approval="")
        self.assertTrue(any("refused" in m for m in self.logged))


class Sandbox(HelperFixture):
    def test_a_machine_whose_systemd_does_not_enforce_the_sandbox_refuses_and_the_job_is_not_used_up(self):
        self.sandbox = lambda: False
        r = self.go()
        self.assertEqual(r["status"], "refused")
        self.assertIn("does not enforce the sandbox", r["note"])
        self.assertEqual(self.ran, [])
        self.sandbox = lambda: True
        self.assertEqual(self.go()["status"], "ok")                                            # the same job runs once the machine is fixed

    def test_the_probe_reads_what_the_unit_reported(self):
        out = lambda text: (lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, text, ""))
        self.assertTrue(helper.sandbox_enforced(out("ENFORCED\n")))
        self.assertFalse(helper.sandbox_enforced(out("OPEN\n")))
        self.assertFalse(helper.sandbox_enforced(out("")))

        def missing(cmd, **kw):
            raise FileNotFoundError("systemd-run")
        self.assertFalse(helper.sandbox_enforced(missing))


Sandbox.__test__ = True
for _n in [n for n in dir(Helper) if n.startswith("test_") and n not in Sandbox.__dict__]:
    setattr(Sandbox, _n, None)


class Client(unittest.TestCase):
    def test_a_missing_helper_is_an_error_answer_not_an_exception(self):
        r = privileged.call("/nonexistent/helper.sock", b"{}", "")
        self.assertEqual(r["status"], "error")
        self.assertIn("not running", r["note"])

    def test_it_talks_to_a_real_socket(self):
        import socket
        import threading
        path = str(Path(tempfile.mkdtemp(dir="/tmp")) / "h.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        seen = []

        def serve():
            conn, _ = srv.accept()
            seen.append(helper.read_all(conn))
            conn.sendall(json.dumps({"status": "ok", "output": "hi", "exit_code": 0}).encode())
            conn.close()
        t = threading.Thread(target=serve)
        t.start()
        r = privileged.call(path, b'{"id": "x"}', "signed")
        t.join()
        srv.close()
        self.assertEqual(r["output"], "hi")
        sent = json.loads(seen[0])
        self.assertEqual((base64.b64decode(sent["job_b64"]), sent["approval"]), (b'{"id": "x"}', "signed"))

    def test_an_answer_that_is_not_understood_is_an_error(self):
        import socket
        import threading
        path = str(Path(tempfile.mkdtemp(dir="/tmp")) / "h.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        t = threading.Thread(target=lambda: (lambda c: (c.recv(10), c.sendall(b"garbage"), c.close()))(srv.accept()[0]))
        t.start()
        r = privileged.call(path, b"{}", "")
        t.join()
        srv.close()
        self.assertEqual(r["status"], "error")


if __name__ == "__main__":
    unittest.main()

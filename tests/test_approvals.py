import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw import approvals  # noqa: E402

DIGEST = "ab" * 32


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)], check=True)
    return Path(str(path) + ".pub").read_text()


class Signing(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.pub = keygen(self.d / "key")
        self.signers = self.d / "approvers"
        self.signers.write_text(approvals.with_approver("", self.pub))

    def approval(self, jid="job-0001", digest=DIGEST, now=1000, ttl=approvals.TTL_S, key="key"):
        record = approvals.make_record(jid, digest, now=now, ttl=ttl)
        return approvals.bundle(record, approvals.sign(record, self.d / key))

    def test_a_good_approval_is_accepted(self):
        ok, why = approvals.check(self.approval(), "job-0001", DIGEST, self.signers, now=2000)
        self.assertEqual((ok, why), (True, ""))

    def test_it_is_for_one_job_and_one_exact_file(self):
        text = self.approval()
        self.assertFalse(approvals.check(text, "job-0002", DIGEST, self.signers, now=2000)[0])
        ok, why = approvals.check(text, "job-0001", "cd" * 32, self.signers, now=2000)
        self.assertFalse(ok)
        self.assertIn("different version", why)                     # an edited job file needs a new approval

    def test_an_approval_expires(self):
        text = self.approval(ttl=100)
        self.assertTrue(approvals.check(text, "job-0001", DIGEST, self.signers, now=1050)[0])
        self.assertIn("expired", approvals.check(text, "job-0001", DIGEST, self.signers, now=1200)[1])

    def test_an_approval_dated_far_in_the_future_is_refused(self):
        self.assertIn("future", approvals.check(self.approval(now=10_000_000), "job-0001", DIGEST, self.signers, now=2000)[1])

    def test_a_signature_by_any_other_key_is_refused(self):
        keygen(self.d / "stranger")
        ok, why = approvals.check(self.approval(key="stranger"), "job-0001", DIGEST, self.signers, now=2000)
        self.assertEqual(ok, False)
        self.assertIn("signature", why)

    def test_a_record_that_was_edited_after_signing_is_refused(self):
        doc = json.loads(self.approval())
        rec = json.loads(doc["record"])
        rec["job"] = "job-0009"
        doc["record"] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self.assertFalse(approvals.check(json.dumps(doc), "job-0009", DIGEST, self.signers, now=2000)[0])

    def test_the_key_is_good_for_approvals_only_not_for_other_signatures(self):
        """A key listed for approvals must not be accepted for anything else, and the reverse: the file names the namespace."""
        self.assertIn(f'namespaces="{approvals.NAMESPACE},{approvals.REGISTRY_NAMESPACE}"', self.signers.read_text())
        record = "x"
        sig = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.d / "key"), "-n", "git"], input=record, capture_output=True, text=True).stdout
        self.assertFalse(approvals.verify(record, sig, self.signers))

    def test_no_approver_installed_means_nothing_is_ever_approved(self):
        ok, why = approvals.check(self.approval(), "job-0001", DIGEST, self.d / "missing", now=2000)
        self.assertEqual((ok, "no approver" in why), (False, True))

    def test_garbage_is_refused_not_a_crash(self):
        for text in ("", "not json", "{}", '{"record": "x", "signature": "y"}', json.dumps({"record": "{}", "signature": "s"})):
            self.assertFalse(approvals.check(text, "job-0001", DIGEST, self.signers, now=2000)[0], text)


class Approvers(unittest.TestCase):
    def test_one_approver_and_a_new_key_replaces_the_old(self):
        d = Path(tempfile.mkdtemp())
        a, b = keygen(d / "a"), keygen(d / "b")
        text = approvals.with_approver(approvals.with_approver("", a), b)
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertIn(b.split()[1], lines[0])

    def test_a_comment_on_the_public_key_is_not_copied_and_a_non_key_is_refused(self):
        d = Path(tempfile.mkdtemp())
        pub = keygen(d / "a")
        line = approvals.signers_line(pub)
        self.assertEqual(len(line.split()), 4)                                  # principal, namespaces, type, key: no trailing comment
        self.assertFalse(line.endswith(" test"))
        with self.assertRaises(ValueError):
            approvals.signers_line("hello world")


if __name__ == "__main__":
    unittest.main()

"""Part 1 of 2 of the test_worker_approvals tests (Rejections)."""
import unittest
import json
import sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import util  # noqa: E402
from support import Base, sh  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
